"""
Reconciliation workflow: scoping, running the matching engine, exception
review, the reconciliation statement and approval.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from apps.accounts.services import require_perm
from apps.approvals.models import ApprovalRecord
from apps.approvals.services import assert_independent, record_decision
from apps.core.activity import log_activity
from apps.core.exceptions import BusinessRuleError
from apps.core.sequences import next_number
from apps.imports.models import Side, SourceTransaction

from . import engine
from .models import (
    AUTO_ELIGIBLE,
    ZERO,
    MatchGroup,
    MatchGroupItem,
    MatchingRule,
    ReconAdjustment,
    Reconciliation,
    ReconExclusion,
    Strategy,
)

LEFT, RIGHT = MatchGroupItem.Side.LEFT, MatchGroupItem.Side.RIGHT
Decision = ApprovalRecord.Decision


# --------------------------------------------------------------------------
# Scope
# --------------------------------------------------------------------------


def create_reconciliation(institution, user, **fields) -> Reconciliation:
    require_perm(user, institution, "recon.run")
    rules = fields.pop("rules", None)
    if fields["period_end"] < fields["period_start"]:
        raise BusinessRuleError("Period end precedes period start.")
    gl_account = fields.get("gl_account")
    if gl_account is not None and gl_account.institution_id != institution.pk:
        raise BusinessRuleError("GL account belongs to a different institution.")
    with transaction.atomic():
        recon = Reconciliation.objects.create(
            institution=institution,
            prepared_by=user,
            reference=next_number(institution, "reconciliation", "REC-"),
            **fields,
        )
        if rules:
            for rule in rules:
                if rule.institution_id != institution.pk:
                    raise BusinessRuleError("Matching rule belongs to a different institution.")
            recon.rules.set(rules)
    log_activity("reconciliation.created", user=user, obj=recon)
    return recon


def side_queryset(recon: Reconciliation, side: str):
    prefix = "left" if side == LEFT else "right"
    qs = SourceTransaction.objects.filter(
        institution=recon.institution,
        source_type=getattr(recon, f"{prefix}_source_type"),
        account_reference=getattr(recon, f"{prefix}_account_reference"),
        currency=recon.currency,
        transaction_date__lte=recon.period_end,
    )
    system = getattr(recon, f"{prefix}_source_system")
    if system:
        qs = qs.filter(source_system=system)
    # Items matched in another reconciliation are settled and out of scope.
    matched_elsewhere = MatchGroupItem.objects.filter(transaction=OuterRef("pk"), active=True).exclude(
        group__reconciliation=recon
    )
    qs = qs.annotate(_elsewhere=Exists(matched_elsewhere)).filter(_elsewhere=False)
    # Items before the period that were already matched in this reconciliation's
    # predecessors are excluded by the clause above; items excluded in a previous
    # reconciliation stay in scope as outstanding until resolved.
    return qs


def flow_of(recon: Reconciliation, side: str, txn: SourceTransaction) -> Decimal:
    inflow = recon.left_inflow_side if side == LEFT else recon.right_inflow_side
    return txn.signed_amount if inflow == Side.CREDIT else -txn.signed_amount


def to_engine(recon, side, txn) -> engine.Txn:
    return engine.Txn(
        id=txn.pk,
        side=side,
        flow=flow_of(recon, side, txn),
        date=txn.transaction_date,
        value_date=txn.value_date,
        norm_transaction_id=txn.norm_transaction_id,
        norm_external_reference=txn.norm_external_reference,
        norm_narration=txn.norm_narration,
        counterparty_reference=txn.counterparty_reference.upper(),
        settlement_batch_ref=txn.settlement_batch_ref,
        channel=txn.channel,
        currency=txn.currency,
        gross_amount=txn.gross_amount,
        fee_amount=txn.fee_amount,
        tax_amount=txn.tax_amount,
        net_amount=txn.net_amount,
        is_reversal=txn.is_reversal,
        suspected_duplicate=txn.is_suspected_duplicate,
    )


def _active_ids(recon) -> set[int]:
    return set(
        MatchGroupItem.objects.filter(group__reconciliation=recon, active=True).values_list("transaction_id", flat=True)
    )


def _excluded_ids(recon) -> set[int]:
    return set(recon.exclusions.values_list("transaction_id", flat=True))


def rules_for(recon: Reconciliation):
    chosen = list(recon.rules.filter(is_active=True))
    if chosen:
        return sorted(chosen, key=lambda r: (r.priority, r.pk))
    return list(
        MatchingRule.objects.filter(institution=recon.institution, is_active=True)
        .filter(Q(recon_type="") | Q(recon_type=recon.recon_type))
        .order_by("priority", "pk")
    )


# --------------------------------------------------------------------------
# Running the engine
# --------------------------------------------------------------------------


def classify(rule: MatchingRule, proposal: engine.Proposal) -> str:
    if proposal.difference != ZERO or not proposal.unique or proposal.flags:
        return MatchGroup.Category.MANUAL_REVIEW
    if rule.strategy == Strategy.NARRATION_SIMILARITY:
        return MatchGroup.Category.MANUAL_REVIEW
    if any(t.suspected_duplicate for t in proposal.left + proposal.right):
        return MatchGroup.Category.MANUAL_REVIEW
    if rule.auto_accept and rule.strategy in AUTO_ELIGIBLE and rule.amount_tolerance == ZERO:
        return MatchGroup.Category.EXACT
    return MatchGroup.Category.HIGH_CONFIDENCE


def run_reconciliation(recon: Reconciliation, user) -> dict:
    require_perm(user, recon.institution, "recon.run")
    with transaction.atomic():
        recon = Reconciliation.objects.select_for_update().get(pk=recon.pk)
        if not recon.is_workable:
            raise BusinessRuleError(f"Reconciliation in status {recon.get_status_display()} cannot be run.")
        recon.status = Reconciliation.Status.PROCESSING
        recon.save(update_fields=["status", "updated_at"])

        # Undecided proposals from earlier runs are discarded and recomputed.
        recon.match_groups.filter(status=MatchGroup.Status.PROPOSED).delete()
        rejected = set(recon.match_groups.filter(status=MatchGroup.Status.REJECTED).values_list("signature", flat=True))
        taken = _active_ids(recon) | _excluded_ids(recon)
        left = [to_engine(recon, LEFT, t) for t in side_queryset(recon, LEFT) if t.pk not in taken]
        right = [to_engine(recon, RIGHT, t) for t in side_queryset(recon, RIGHT) if t.pk not in taken]

        counts = {c: 0 for c in MatchGroup.Category.values}
        per_rule = []
        for rule in rules_for(recon):
            func = engine.STRATEGIES[rule.strategy]
            proposals = [p for p in func(rule, left, right) if p.signature not in rejected]
            used: set[int] = set()
            created = 0
            for proposal in proposals:
                ids = set(proposal.ids)
                if ids & used:
                    continue
                used |= ids
                category = classify(rule, proposal)
                _persist(recon, rule, proposal, category)
                counts[category] += 1
                created += 1
            left = [t for t in left if t.id not in used]
            right = [t for t in right if t.id not in used]
            per_rule.append({"rule": rule.name, "strategy": rule.strategy, "groups": created})

        duplicates = engine.find_duplicates(left) + engine.find_duplicates(right)
        summary = {
            "run_at": timezone.now().isoformat(),
            "auto_accepted": counts[MatchGroup.Category.EXACT],
            "high_confidence": counts[MatchGroup.Category.HIGH_CONFIDENCE],
            "manual_review": counts[MatchGroup.Category.MANUAL_REVIEW],
            "unmatched_left": len(left),
            "unmatched_right": len(right),
            "suspected_duplicate_groups": len(duplicates),
            "rules": per_rule,
        }
        recon.status = Reconciliation.Status.IN_REVIEW
        recon.last_run_at = timezone.now()
        recon.run_summary = summary
        recon.save()
    log_activity("reconciliation.run", user=user, obj=recon, details=summary)
    return summary


def _persist(recon, rule, proposal: engine.Proposal, category: str, user=None) -> MatchGroup:
    accepted = category == MatchGroup.Category.EXACT
    group = MatchGroup.objects.create(
        reconciliation=recon,
        rule=rule,
        rule_snapshot=rule.snapshot() if rule else {},
        kind=proposal.kind,
        category=category,
        status=MatchGroup.Status.ACCEPTED if accepted else MatchGroup.Status.PROPOSED,
        left_total=proposal.left_total,
        right_total=proposal.right_total,
        difference=proposal.difference,
        signature=proposal.signature,
        reason=("; ".join([proposal.reason] + proposal.flags))[:255],
        created_by=user,
        decided_at=timezone.now() if accepted else None,
    )
    MatchGroupItem.objects.bulk_create(
        [
            MatchGroupItem(group=group, transaction_id=t.id, side=LEFT, flow_amount=t.flow, active=accepted)
            for t in proposal.left
        ]
        + [
            MatchGroupItem(group=group, transaction_id=t.id, side=RIGHT, flow_amount=t.flow, active=accepted)
            for t in proposal.right
        ]
    )
    return group


# --------------------------------------------------------------------------
# Exception review
# --------------------------------------------------------------------------


def _workable(recon_id) -> Reconciliation:
    recon = Reconciliation.objects.select_for_update().get(pk=recon_id)
    if not recon.is_workable:
        raise BusinessRuleError(f"Reconciliation in status {recon.get_status_display()} cannot be changed.")
    return recon


def accept_match(group: MatchGroup, user, note="") -> MatchGroup:
    recon = group.reconciliation
    require_perm(user, recon.institution, "recon.investigate")
    with transaction.atomic():
        _workable(recon.pk)
        group = MatchGroup.objects.select_for_update().get(pk=group.pk)
        if group.status != MatchGroup.Status.PROPOSED:
            raise BusinessRuleError("Only proposed matches can be accepted.")
        if group.difference != ZERO and not note.strip():
            raise BusinessRuleError("A note is required to accept a match with a difference.")
        try:
            with transaction.atomic():
                group.items.update(active=True)
        except IntegrityError as exc:
            raise BusinessRuleError("One of these transactions is already matched elsewhere.") from exc
        group.status = MatchGroup.Status.ACCEPTED
        group.decided_by = user
        group.decided_at = timezone.now()
        group.note = note
        group.save()
    log_activity(
        "reconciliation.match_accepted",
        user=user,
        obj=recon,
        details={"group": group.pk, "difference": group.difference, "note": note},
    )
    return group


def reject_match(group: MatchGroup, user, note="") -> MatchGroup:
    """Reject a proposal, or unmatch an accepted group. The pairing will not be proposed again."""
    recon = group.reconciliation
    require_perm(user, recon.institution, "recon.investigate")
    with transaction.atomic():
        _workable(recon.pk)
        group = MatchGroup.objects.select_for_update().get(pk=group.pk)
        if group.status == MatchGroup.Status.REJECTED:
            raise BusinessRuleError("Match is already rejected.")
        if group.status == MatchGroup.Status.ACCEPTED and not note.strip():
            raise BusinessRuleError("A note is required to unmatch an accepted match.")
        group.items.update(active=False)
        group.status = MatchGroup.Status.REJECTED
        group.decided_by = user
        group.decided_at = timezone.now()
        group.note = note
        group.save()
    log_activity("reconciliation.match_rejected", user=user, obj=recon, details={"group": group.pk, "note": note})
    return group


def manual_match(recon: Reconciliation, user, left_ids, right_ids, note: str) -> MatchGroup:
    require_perm(user, recon.institution, "recon.investigate")
    left_ids, right_ids = set(map(int, left_ids)), set(map(int, right_ids))
    if not note or not note.strip():
        raise BusinessRuleError("A note explaining the manual match is required.")
    reversal = not left_ids or not right_ids
    if len(left_ids | right_ids) < 2:
        raise BusinessRuleError("Select at least two transactions.")
    with transaction.atomic():
        recon = _workable(recon.pk)
        taken = _active_ids(recon) | _excluded_ids(recon)
        if (left_ids | right_ids) & taken:
            raise BusinessRuleError("Some selected transactions are already matched or excluded.")
        lefts = list(side_queryset(recon, LEFT).filter(pk__in=left_ids))
        rights = list(side_queryset(recon, RIGHT).filter(pk__in=right_ids))
        if len(lefts) != len(left_ids) or len(rights) != len(right_ids):
            raise BusinessRuleError("Some selected transactions are not in scope for this reconciliation.")
        proposal = engine.Proposal(
            [to_engine(recon, LEFT, t) for t in lefts],
            [to_engine(recon, RIGHT, t) for t in rights],
            unique=True,
            reason="Manual match",
            kind="REVERSAL" if reversal else "MATCH",
        )
        group = MatchGroup.objects.create(
            reconciliation=recon,
            kind=proposal.kind,
            category=MatchGroup.Category.MANUAL,
            status=MatchGroup.Status.ACCEPTED,
            left_total=proposal.left_total,
            right_total=proposal.right_total,
            difference=proposal.difference,
            signature=proposal.signature,
            reason="Manual match",
            note=note,
            created_by=user,
            decided_by=user,
            decided_at=timezone.now(),
        )
        try:
            with transaction.atomic():
                MatchGroupItem.objects.bulk_create(
                    [
                        MatchGroupItem(group=group, transaction_id=t.id, side=LEFT, flow_amount=t.flow, active=True)
                        for t in proposal.left
                    ]
                    + [
                        MatchGroupItem(group=group, transaction_id=t.id, side=RIGHT, flow_amount=t.flow, active=True)
                        for t in proposal.right
                    ]
                )
        except IntegrityError as exc:
            raise BusinessRuleError("One of these transactions is already matched elsewhere.") from exc
    log_activity(
        "reconciliation.manual_match",
        user=user,
        obj=recon,
        details={"group": group.pk, "difference": group.difference, "note": note},
    )
    return group


def exclude_transaction(recon: Reconciliation, user, txn_id: int, reason: str) -> ReconExclusion:
    require_perm(user, recon.institution, "recon.investigate")
    if not reason or not reason.strip():
        raise BusinessRuleError("An exclusion reason is required.")
    with transaction.atomic():
        recon = _workable(recon.pk)
        if txn_id in _active_ids(recon):
            raise BusinessRuleError("Unmatch the transaction before excluding it.")
        side = None
        for candidate in (LEFT, RIGHT):
            if side_queryset(recon, candidate).filter(pk=txn_id).exists():
                side = candidate
                break
        if side is None:
            raise BusinessRuleError("Transaction is not in scope for this reconciliation.")
        exclusion, created = ReconExclusion.objects.get_or_create(
            reconciliation=recon,
            transaction_id=txn_id,
            defaults={"side": side, "reason": reason, "excluded_by": user},
        )
        if not created:
            raise BusinessRuleError("Transaction is already excluded.")
        recon.match_groups.filter(status=MatchGroup.Status.PROPOSED, items__transaction_id=txn_id).delete()
    log_activity("reconciliation.excluded", user=user, obj=recon, details={"transaction": txn_id, "reason": reason})
    return exclusion


def remove_exclusion(exclusion: ReconExclusion, user):
    recon = exclusion.reconciliation
    require_perm(user, recon.institution, "recon.investigate")
    with transaction.atomic():
        _workable(recon.pk)
        exclusion.delete()
    log_activity(
        "reconciliation.exclusion_removed", user=user, obj=recon, details={"transaction": exclusion.transaction_id}
    )


def add_adjustment(recon: Reconciliation, user, *, side, description, amount, reference="", journal=None):
    require_perm(user, recon.institution, "recon.investigate")
    if not description.strip():
        raise BusinessRuleError("An adjustment description is required.")
    if amount == ZERO:
        raise BusinessRuleError("Adjustment amount must not be zero.")
    if journal is not None and journal.institution_id != recon.institution_id:
        raise BusinessRuleError("Journal belongs to a different institution.")
    with transaction.atomic():
        recon = _workable(recon.pk)
        adjustment = ReconAdjustment.objects.create(
            reconciliation=recon,
            side=side,
            description=description,
            amount=amount,
            reference=reference,
            journal=journal,
            created_by=user,
        )
    log_activity(
        "reconciliation.adjustment_added",
        user=user,
        obj=recon,
        details={"side": side, "amount": amount, "description": description},
    )
    return adjustment


# --------------------------------------------------------------------------
# Statement
# --------------------------------------------------------------------------


@dataclass
class SideFigures:
    label: str
    opening: Decimal
    inflows: Decimal
    outflows: Decimal
    reported_closing: Decimal | None

    @property
    def computed_closing(self) -> Decimal:
        return self.opening + self.inflows - self.outflows

    @property
    def closing(self) -> Decimal:
        return self.reported_closing if self.reported_closing is not None else self.computed_closing

    @property
    def balance_check_difference(self) -> Decimal:
        if self.reported_closing is None:
            return ZERO
        return self.reported_closing - self.computed_closing


@dataclass
class Statement:
    recon: Reconciliation
    left: SideFigures
    right: SideFigures
    matched_groups: int
    matched_left_total: Decimal
    matched_right_total: Decimal
    match_differences: Decimal
    outstanding_deposits: list  # right inflows not in left
    outstanding_payments: list  # right outflows not in left
    unrecorded_credits: list  # left inflows not in right
    unrecorded_charges: list  # left outflows not in right
    excluded_left: list
    excluded_right: list
    duplicates: list
    adjustments: list
    pending_review: int
    flows: dict = field(default_factory=dict)

    @staticmethod
    def _total(items):
        return sum((flow for _, flow in items), ZERO)

    @property
    def unmatched_count(self):
        return (
            len(self.outstanding_deposits)
            + len(self.outstanding_payments)
            + len(self.unrecorded_credits)
            + len(self.unrecorded_charges)
        )

    @property
    def total_outstanding_deposits(self):
        return self._total(self.outstanding_deposits)

    @property
    def total_outstanding_payments(self):
        return -self._total(self.outstanding_payments)

    @property
    def total_unrecorded_credits(self):
        return self._total(self.unrecorded_credits)

    @property
    def total_unrecorded_charges(self):
        return -self._total(self.unrecorded_charges)

    @property
    def total_excluded_left(self):
        return self._total(self.excluded_left)

    @property
    def total_excluded_right(self):
        return self._total(self.excluded_right)

    @property
    def left_adjustments(self):
        return sum((a.amount for a in self.adjustments if a.side == LEFT), ZERO)

    @property
    def right_adjustments(self):
        return sum((a.amount for a in self.adjustments if a.side == RIGHT), ZERO)

    @property
    def adjusted_left_balance(self):
        """Left closing balance + right-only items (items recorded internally but not yet in the source)."""
        return (
            self.left.closing
            + self.total_outstanding_deposits
            - self.total_outstanding_payments
            + self.total_excluded_right
            + self.left_adjustments
        )

    @property
    def adjusted_right_balance(self):
        """Right closing balance + left-only items (items in the source not yet recorded internally)."""
        return (
            self.right.closing
            + self.total_unrecorded_credits
            - self.total_unrecorded_charges
            + self.total_excluded_left
            + self.right_adjustments
        )

    @property
    def final_difference(self):
        return self.adjusted_left_balance - self.adjusted_right_balance

    def validation_errors(self, tolerance: Decimal) -> list[str]:
        errors = []
        if self.left.balance_check_difference != ZERO:
            errors.append(
                f"{self.left.label}: opening + inflows − outflows differs from the reported closing balance by "
                f"{self.left.balance_check_difference}. The imported data may be incomplete."
            )
        if self.right.balance_check_difference != ZERO:
            errors.append(
                f"{self.right.label}: opening + inflows − outflows differs from the reported closing balance by "
                f"{self.right.balance_check_difference}. The imported data may be incomplete."
            )
        if self.pending_review:
            errors.append(f"{self.pending_review} proposed match(es) still await review.")
        if abs(self.final_difference) > tolerance:
            errors.append(f"Unexplained difference {self.final_difference} exceeds the approved tolerance {tolerance}.")
        return errors


def build_statement(recon: Reconciliation) -> Statement:
    active = set(
        MatchGroupItem.objects.filter(group__reconciliation=recon, active=True).values_list("transaction_id", flat=True)
    )
    exclusions = {e.transaction_id: e for e in recon.exclusions.all()}
    figures, buckets = {}, {}
    for side in (LEFT, RIGHT):
        prefix = "left" if side == LEFT else "right"
        inflows = outflows = ZERO
        unmatched, excluded = [], []
        for txn in side_queryset(recon, side).order_by("transaction_date", "pk"):
            flow = flow_of(recon, side, txn)
            if txn.transaction_date >= recon.period_start:
                if flow > 0:
                    inflows += flow
                else:
                    outflows -= flow
            if txn.pk in active:
                continue
            if txn.pk in exclusions:
                excluded.append((txn, flow))
            else:
                unmatched.append((txn, flow))
        figures[side] = SideFigures(
            label=getattr(recon, f"{prefix}_label"),
            opening=getattr(recon, f"opening_{prefix}_balance"),
            inflows=inflows,
            outflows=outflows,
            reported_closing=getattr(recon, f"closing_{prefix}_balance"),
        )
        buckets[side] = (unmatched, excluded)

    accepted = recon.match_groups.filter(status=MatchGroup.Status.ACCEPTED)
    left_unmatched, left_excluded = buckets[LEFT]
    right_unmatched, right_excluded = buckets[RIGHT]
    duplicates = [(t, f) for t, f in left_unmatched + right_unmatched if t.is_suspected_duplicate]
    return Statement(
        recon=recon,
        left=figures[LEFT],
        right=figures[RIGHT],
        matched_groups=accepted.count(),
        matched_left_total=sum((g.left_total for g in accepted), ZERO),
        matched_right_total=sum((g.right_total for g in accepted), ZERO),
        match_differences=sum((g.difference for g in accepted if g.kind == MatchGroup.Kind.MATCH), ZERO),
        outstanding_deposits=[(t, f) for t, f in right_unmatched if f > 0],
        outstanding_payments=[(t, f) for t, f in right_unmatched if f < 0],
        unrecorded_credits=[(t, f) for t, f in left_unmatched if f > 0],
        unrecorded_charges=[(t, f) for t, f in left_unmatched if f < 0],
        excluded_left=left_excluded,
        excluded_right=right_excluded,
        duplicates=duplicates,
        adjustments=list(recon.adjustments.all()),
        pending_review=recon.match_groups.filter(status=MatchGroup.Status.PROPOSED).count(),
    )


# --------------------------------------------------------------------------
# Approval workflow
# --------------------------------------------------------------------------


def submit_reconciliation(recon: Reconciliation, user, comment="") -> Reconciliation:
    require_perm(user, recon.institution, "recon.run")
    with transaction.atomic():
        recon = Reconciliation.objects.select_for_update().select_related("institution").get(pk=recon.pk)
        if recon.status not in (Reconciliation.Status.IN_REVIEW, Reconciliation.Status.REOPENED):
            raise BusinessRuleError("Run the reconciliation and review exceptions before submitting.")
        errors = build_statement(recon).validation_errors(recon.institution.reconciliation_tolerance)
        if errors:
            raise BusinessRuleError("Cannot submit: " + " ".join(errors))
        recon.status = Reconciliation.Status.SUBMITTED
        recon.submitted_at = timezone.now()
        recon.save()
        record_decision(recon, Decision.SUBMITTED, user, comment)
    return recon


def approve_reconciliation(recon: Reconciliation, user, comment="") -> Reconciliation:
    require_perm(user, recon.institution, "recon.approve")
    with transaction.atomic():
        recon = Reconciliation.objects.select_for_update().select_related("institution").get(pk=recon.pk)
        if recon.status != Reconciliation.Status.SUBMITTED:
            raise BusinessRuleError("Only submitted reconciliations can be approved.")
        assert_independent(recon.institution, recon.prepared_by, user, "this reconciliation")
        errors = build_statement(recon).validation_errors(recon.institution.reconciliation_tolerance)
        if errors:
            raise BusinessRuleError("Cannot approve: " + " ".join(errors))
        recon.status = Reconciliation.Status.APPROVED
        recon.approved_by = user
        recon.approved_at = timezone.now()
        recon.save()
        record_decision(recon, Decision.APPROVED, user, comment)
    return recon


def return_reconciliation(recon: Reconciliation, user, reason: str) -> Reconciliation:
    require_perm(user, recon.institution, "recon.approve")
    if not reason.strip():
        raise BusinessRuleError("A reason is required.")
    with transaction.atomic():
        recon = Reconciliation.objects.select_for_update().select_related("institution").get(pk=recon.pk)
        if recon.status != Reconciliation.Status.SUBMITTED:
            raise BusinessRuleError("Only submitted reconciliations can be returned.")
        assert_independent(recon.institution, recon.prepared_by, user, "this reconciliation")
        recon.status = Reconciliation.Status.IN_REVIEW
        recon.save()
        record_decision(recon, Decision.REJECTED, user, reason)
    return recon


def reopen_reconciliation(recon: Reconciliation, user, reason: str) -> Reconciliation:
    require_perm(user, recon.institution, "recon.reopen")
    if not reason.strip():
        raise BusinessRuleError("A reason is required to reopen a reconciliation.")
    with transaction.atomic():
        recon = Reconciliation.objects.select_for_update().get(pk=recon.pk)
        if recon.status not in (Reconciliation.Status.APPROVED, Reconciliation.Status.CLOSED):
            raise BusinessRuleError("Only approved or closed reconciliations can be reopened.")
        recon.status = Reconciliation.Status.REOPENED
        recon.approved_by = None
        recon.approved_at = None
        recon.save()
        record_decision(recon, Decision.REOPENED, user, reason)
    return recon


def close_reconciliation(recon: Reconciliation, user, comment="") -> Reconciliation:
    require_perm(user, recon.institution, "recon.approve")
    with transaction.atomic():
        recon = Reconciliation.objects.select_for_update().get(pk=recon.pk)
        if recon.status != Reconciliation.Status.APPROVED:
            raise BusinessRuleError("Only approved reconciliations can be closed.")
        recon.status = Reconciliation.Status.CLOSED
        recon.save()
        record_decision(recon, Decision.CLOSED, user, comment)
    return recon
