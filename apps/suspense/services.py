from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.accounting.models import Journal
from apps.accounting.services import LineInput, create_journal
from apps.accounts.services import require_perm
from apps.approvals.models import ApprovalRecord
from apps.approvals.services import assert_independent, record_decision
from apps.core.activity import log_activity
from apps.core.exceptions import BusinessRuleError
from apps.core.sequences import next_number
from apps.notifications.services import notify, users_with_permission

from .models import AgingPolicy, EscalationRule, SuspenseItem, SuspenseNote, bucket_index, bucket_labels

ZERO = Decimal("0.00")
Decision = ApprovalRecord.Decision


def aging_bounds(institution):
    policy = AgingPolicy.objects.filter(institution=institution).first()
    return policy.bounds() if policy else [7, 30, 60, 90]


def set_aging_bounds(institution, user, bounds):
    require_perm(user, institution, "suspense.configure")
    try:
        cleaned = sorted({int(b) for b in bounds})
    except (TypeError, ValueError) as exc:
        raise BusinessRuleError("Bucket bounds must be whole numbers of days.") from exc
    if not cleaned or cleaned[0] <= 0:
        raise BusinessRuleError("Provide at least one positive bucket bound.")
    policy, _ = AgingPolicy.objects.update_or_create(institution=institution, defaults={"bucket_bounds": cleaned})
    log_activity("suspense.aging_configured", user=user, institution=institution, details={"bounds": cleaned})
    return policy


def create_item(
    institution,
    user,
    *,
    account,
    original_date,
    original_amount,
    side,
    reason,
    currency=None,
    transaction_reference="",
    source_system="",
    narration="",
    assigned_to=None,
    source_transaction=None,
    reconciliation=None,
) -> SuspenseItem:
    require_perm(user, institution, "suspense.manage")
    if account.institution_id != institution.pk:
        raise BusinessRuleError("Account belongs to a different institution.")
    if not account.is_suspense_account:
        raise BusinessRuleError(f"Account {account} is not designated as a suspense account.")
    if original_amount <= 0:
        raise BusinessRuleError("Suspense amount must be positive; use the side field for direction.")
    if not reason.strip():
        raise BusinessRuleError("A reason for suspense is required.")
    if source_transaction is not None and source_transaction.institution_id != institution.pk:
        raise BusinessRuleError("Source transaction belongs to a different institution.")
    with transaction.atomic():
        item = SuspenseItem.objects.create(
            institution=institution,
            reference=next_number(institution, "suspense", "SUS-"),
            account=account,
            original_date=original_date,
            original_amount=original_amount,
            side=side,
            reason=reason,
            currency=currency or institution.base_currency,
            transaction_reference=transaction_reference,
            source_system=source_system,
            narration=narration,
            assigned_to=assigned_to,
            created_by=user,
            source_transaction=source_transaction,
            reconciliation=reconciliation,
        )
    log_activity("suspense.created", user=user, obj=item)
    return item


def create_from_transaction(recon, txn, user, *, account, reason) -> SuspenseItem:
    """Raise a suspense item for an unmatched reconciliation exception."""
    if SuspenseItem.objects.filter(source_transaction=txn, status__in=SuspenseItem.OPEN_STATUSES).exists():
        raise BusinessRuleError("An open suspense item already exists for this transaction.")
    return create_item(
        recon.institution,
        user,
        account=account,
        original_date=txn.transaction_date,
        original_amount=abs(txn.signed_amount),
        side=SuspenseItem.Side.CREDIT if txn.signed_amount > 0 else SuspenseItem.Side.DEBIT,
        reason=reason,
        currency=txn.currency,
        transaction_reference=txn.transaction_id or txn.external_reference,
        source_system=txn.source_system,
        narration=txn.narration,
        source_transaction=txn,
        reconciliation=recon,
    )


def _open(item) -> SuspenseItem:
    item = SuspenseItem.objects.select_for_update().select_related("institution").get(pk=item.pk)
    if item.status == SuspenseItem.Status.RESOLVED:
        raise BusinessRuleError("Item is already resolved.")
    return item


def assign(item, user, assignee) -> SuspenseItem:
    require_perm(user, item.institution, "suspense.manage")
    with transaction.atomic():
        item = _open(item)
        if (
            assignee is not None
            and not assignee.memberships.filter(institution=item.institution, is_active=True).exists()
        ):
            raise BusinessRuleError("Assignee is not a member of this institution.")
        item.assigned_to = assignee
        if item.status == SuspenseItem.Status.OPEN:
            item.status = SuspenseItem.Status.INVESTIGATING
        item.save()
    log_activity("suspense.assigned", user=user, obj=item, details={"assignee": str(assignee)})
    if assignee is not None:
        notify(
            item.institution,
            [assignee],
            f"Suspense item {item.reference} assigned to you",
            item.reason,
            reverse("suspense:detail", args=[item.pk]),
        )
    return item


def add_note(item, user, text) -> SuspenseNote:
    require_perm(user, item.institution, "suspense.manage")
    if not text.strip():
        raise BusinessRuleError("Note text is required.")
    note = SuspenseNote.objects.create(item=item, user=user, text=text)
    if item.status == SuspenseItem.Status.OPEN:
        item.status = SuspenseItem.Status.INVESTIGATING
        item.save(update_fields=["status", "updated_at"])
    log_activity("suspense.note_added", user=user, obj=item)
    return note


def prepare_clearing_journal(item, user, *, contra_account, description="") -> Journal:
    """
    Prepare (never post) a draft journal clearing the suspense balance to the
    contra account. It follows the standard journal approval workflow.
    """
    require_perm(user, item.institution, "suspense.manage")
    if item.related_journal_id:
        raise BusinessRuleError("A clearing journal already exists for this item.")
    if item.status == SuspenseItem.Status.RESOLVED:
        raise BusinessRuleError("Item is already resolved.")
    amount = item.original_amount
    # A debit sitting in suspense is cleared by crediting suspense, and vice versa.
    if item.side == SuspenseItem.Side.DEBIT:
        lines = [LineInput(account=contra_account, debit=amount), LineInput(account=item.account, credit=amount)]
    else:
        lines = [LineInput(account=item.account, debit=amount), LineInput(account=contra_account, credit=amount)]
    journal = create_journal(
        item.institution,
        user,
        transaction_date=timezone.localdate(),
        description=description or f"Clearance of suspense item {item.reference}",
        lines=lines,
        currency=item.currency,
        source_reference=item.reference,
    )
    item.related_journal = journal
    item.save(update_fields=["related_journal", "updated_at"])
    log_activity("suspense.clearing_journal_prepared", user=user, obj=item, details={"journal": journal.pk})
    return journal


def propose_resolution(item, user, action, notes) -> SuspenseItem:
    require_perm(user, item.institution, "suspense.manage")
    if action not in SuspenseItem.Resolution.values:
        raise BusinessRuleError("Unknown resolution action.")
    if not notes.strip():
        raise BusinessRuleError("Resolution notes are required.")
    with transaction.atomic():
        item = _open(item)
        if item.status == SuspenseItem.Status.PENDING_APPROVAL:
            raise BusinessRuleError("A resolution is already pending approval.")
        item.resolution_action = action
        item.resolution_notes = notes
        item.proposed_by = user
        item.proposed_at = timezone.now()
        item.status = SuspenseItem.Status.PENDING_APPROVAL
        item.save()
        record_decision(item, Decision.SUBMITTED, user, notes)
    approvers = users_with_permission(item.institution, "suspense.approve")
    notify(
        item.institution,
        [u for u in approvers if u.pk != user.pk],
        f"Suspense resolution awaiting approval: {item.reference}",
        notes,
        reverse("suspense:detail", args=[item.pk]),
    )
    return item


def approve_resolution(item, user, comment="") -> SuspenseItem:
    require_perm(user, item.institution, "suspense.approve")
    with transaction.atomic():
        item = _open(item)
        if item.status != SuspenseItem.Status.PENDING_APPROVAL:
            raise BusinessRuleError("No resolution is pending approval.")
        assert_independent(item.institution, item.proposed_by, user, "this resolution")
        if item.related_journal_id:
            journal = Journal.objects.get(pk=item.related_journal_id)
            if journal.status != Journal.Status.POSTED:
                raise BusinessRuleError(
                    f"The clearing journal {journal} must be approved and posted before the item is resolved."
                )
        item.status = SuspenseItem.Status.RESOLVED
        item.approved_by = user
        item.date_resolved = timezone.localdate()
        item.save()
        record_decision(item, Decision.APPROVED, user, comment)
    return item


def reject_resolution(item, user, reason) -> SuspenseItem:
    require_perm(user, item.institution, "suspense.approve")
    if not reason.strip():
        raise BusinessRuleError("A reason is required.")
    with transaction.atomic():
        item = _open(item)
        if item.status != SuspenseItem.Status.PENDING_APPROVAL:
            raise BusinessRuleError("No resolution is pending approval.")
        assert_independent(item.institution, item.proposed_by, user, "this resolution")
        item.status = SuspenseItem.Status.INVESTIGATING
        item.save()
        record_decision(item, Decision.REJECTED, user, reason)
    return item


@dataclass
class AgingReport:
    as_of: date
    labels: list[str]
    bucket_totals: list[Decimal]
    bucket_counts: list[int]
    by_account: dict = field(default_factory=dict)
    items: list = field(default_factory=list)

    @property
    def total(self):
        return sum(self.bucket_totals, ZERO)

    @property
    def count(self):
        return sum(self.bucket_counts)


def aging_report(institution, as_of: date | None = None, account=None) -> AgingReport:
    as_of = as_of or timezone.localdate()
    bounds = aging_bounds(institution)
    labels = bucket_labels(bounds)
    totals = [ZERO] * len(labels)
    counts = [0] * len(labels)
    by_account: dict = defaultdict(lambda: [ZERO] * len(labels))
    qs = SuspenseItem.objects.filter(
        institution=institution, status__in=SuspenseItem.OPEN_STATUSES, original_date__lte=as_of
    ).select_related("account", "assigned_to")
    if account is not None:
        qs = qs.filter(account=account)
    items = []
    for item in qs:
        age = item.age_days(as_of)
        idx = bucket_index(age, bounds)
        totals[idx] += item.original_amount
        counts[idx] += 1
        by_account[item.account][idx] += item.original_amount
        items.append((item, age, labels[idx]))
    return AgingReport(as_of, labels, totals, counts, dict(by_account), items)


def run_escalations(institution, today: date | None = None) -> int:
    """Notify according to escalation rules; returns the number of item escalations sent."""
    today = today or timezone.localdate()
    rules = list(EscalationRule.objects.filter(institution=institution, is_active=True).order_by("min_age_days"))
    if not rules:
        return 0
    sent = 0
    for item in SuspenseItem.objects.filter(institution=institution, status__in=SuspenseItem.OPEN_STATUSES):
        age = item.age_days(today)
        applicable = [(level, r) for level, r in enumerate(rules, start=1) if age >= r.min_age_days]
        if not applicable:
            continue
        level, rule = applicable[-1]
        due = (
            item.escalation_level < level
            or item.last_escalated_at is None
            or item.last_escalated_at + timedelta(days=rule.repeat_every_days) <= today
        )
        if not due:
            continue
        recipients = users_with_permission(institution, rule.notify_permission)
        if rule.notify_assignee and item.assigned_to:
            recipients.append(item.assigned_to)
        notify(
            institution,
            recipients,
            f"Overdue suspense item {item.reference} ({age} days)",
            f"{item.original_amount} {item.currency} on {item.account}: {item.reason}",
            reverse("suspense:detail", args=[item.pk]),
            email=True,
        )
        item.escalation_level = level
        item.last_escalated_at = today
        item.save(update_fields=["escalation_level", "last_escalated_at", "updated_at"])
        log_activity("suspense.escalated", institution=institution, obj=item, details={"level": level, "age": age})
        sent += 1
    return sent
