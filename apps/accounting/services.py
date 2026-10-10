"""
Double-entry accounting engine: journal workflow, posting, reversal and
period control. All state changes go through these functions.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.accounts.services import has_perm, require_perm
from apps.approvals.models import ApprovalRecord
from apps.approvals.services import assert_independent, record_decision
from apps.core.activity import log_activity
from apps.core.exceptions import BusinessRuleError, TenantViolationError
from apps.core.money import quantize
from apps.core.sequences import next_number

from .models import (
    ZERO,
    Account,
    AccountCategory,
    AccountType,
    FinancialPeriod,
    Journal,
    JournalLine,
    JournalType,
    LedgerEntry,
)

Decision = ApprovalRecord.Decision


@dataclass
class LineInput:
    account: Account
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    description: str = ""
    branch: object = None
    department: object = None


# --------------------------------------------------------------------------
# Journal preparation
# --------------------------------------------------------------------------


def _money(institution, value) -> Decimal:
    if value in (None, ""):
        return ZERO
    return quantize(
        Decimal(str(value)) if not isinstance(value, Decimal) else value,
        institution.amount_decimal_places,
        institution.rounding_mode,
    )


def create_journal(
    institution,
    user,
    *,
    transaction_date,
    accounting_date=None,
    description,
    lines,
    currency=None,
    exchange_rate=Decimal("1"),
    journal_type=JournalType.MANUAL,
    source_reference="",
    supporting_reference="",
    branch=None,
    idempotency_key=None,
) -> Journal:
    require_perm(user, institution, "journal.create")
    currency = (currency or institution.base_currency).upper()
    if idempotency_key:
        existing = Journal.objects.filter(institution=institution, idempotency_key=idempotency_key).first()
        if existing:
            return existing
    try:
        with transaction.atomic():
            journal = Journal.objects.create(
                institution=institution,
                journal_type=journal_type,
                transaction_date=transaction_date,
                accounting_date=accounting_date or transaction_date,
                description=description,
                source_reference=source_reference,
                supporting_reference=supporting_reference,
                currency=currency,
                exchange_rate=exchange_rate,
                branch=branch,
                prepared_by=user,
                idempotency_key=idempotency_key,
            )
            _validate_currency(journal)
            _replace_lines(journal, lines)
    except IntegrityError:
        if idempotency_key:
            return Journal.objects.get(institution=institution, idempotency_key=idempotency_key)
        raise
    log_activity("journal.created", user=user, obj=journal)
    return journal


def update_journal(journal: Journal, user, *, lines=None, **fields) -> Journal:
    with transaction.atomic():
        journal = Journal.objects.select_for_update().get(pk=journal.pk)
        if not journal.is_editable:
            raise BusinessRuleError(f"Journal in status {journal.get_status_display()} cannot be edited.")
        if journal.prepared_by_id != user.pk:
            raise BusinessRuleError("Only the preparer can edit a draft journal.")
        require_perm(user, journal.institution, "journal.create")
        allowed = {
            "transaction_date",
            "accounting_date",
            "description",
            "source_reference",
            "supporting_reference",
            "currency",
            "exchange_rate",
            "branch",
            "journal_type",
        }
        for key, value in fields.items():
            if key not in allowed:
                raise BusinessRuleError(f"Field {key} cannot be edited.")
            setattr(journal, key, value)
        _validate_currency(journal)
        journal.save()
        if lines is not None:
            _replace_lines(journal, lines)
    log_activity("journal.updated", user=user, obj=journal)
    return journal


def _validate_currency(journal):
    if journal.currency == journal.institution.base_currency and journal.exchange_rate != Decimal("1"):
        raise BusinessRuleError("Exchange rate must be 1 for base-currency journals.")


def _replace_lines(journal: Journal, lines):
    institution = journal.institution
    journal.lines.all().delete()
    objs = []
    for idx, line in enumerate(lines, start=1):
        if isinstance(line, dict):
            line = LineInput(**line)
        if line.account.institution_id != institution.pk:
            raise TenantViolationError("Journal line account belongs to a different institution.")
        for related in (line.branch, line.department):
            if related is not None and related.institution_id != institution.pk:
                raise TenantViolationError("Journal line branch/department belongs to a different institution.")
        debit = _money(institution, line.debit)
        credit = _money(institution, line.credit)
        if debit < 0 or credit < 0:
            raise BusinessRuleError(f"Line {idx}: amounts must not be negative.")
        if (debit > 0) == (credit > 0):
            raise BusinessRuleError(f"Line {idx}: enter either a debit or a credit amount.")
        rate = journal.exchange_rate
        objs.append(
            JournalLine(
                journal=journal,
                line_no=idx,
                account=line.account,
                description=line.description or "",
                debit=debit,
                credit=credit,
                base_debit=_money(institution, debit * rate),
                base_credit=_money(institution, credit * rate),
                branch=line.branch,
                department=line.department,
            )
        )
    JournalLine.objects.bulk_create(objs)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def validate_journal(journal: Journal):
    """Raise BusinessRuleError if the journal cannot be submitted/approved/posted."""
    lines = list(journal.lines.select_related("account"))
    if len(lines) < 2:
        raise BusinessRuleError("A journal needs at least two lines.")
    totals = journal.totals()
    if totals["debit"] != totals["credit"]:
        raise BusinessRuleError(
            f"Journal is not balanced: debits {totals['debit']} ≠ credits {totals['credit']} ({journal.currency})."
        )
    if totals["base_debit"] != totals["base_credit"]:
        raise BusinessRuleError(
            "Journal is not balanced in base currency after conversion "
            f"({totals['base_debit']} ≠ {totals['base_credit']}). Adjust a line to absorb the rounding difference."
        )
    if totals["debit"] == ZERO:
        raise BusinessRuleError("Journal total must be greater than zero.")
    for line in lines:
        account = line.account
        if account.institution_id != journal.institution_id:
            raise TenantViolationError()
        if not account.is_active:
            raise BusinessRuleError(f"Account {account} is inactive.")
        if account.account_type != AccountType.POSTING:
            raise BusinessRuleError(f"Account {account} is a header account and cannot be posted to.")
        if account.currency and account.currency != journal.currency:
            raise BusinessRuleError(f"Account {account} only accepts {account.currency} postings.")
        if account.effective_from and journal.accounting_date < account.effective_from:
            raise BusinessRuleError(f"Account {account} is not effective until {account.effective_from}.")
        if account.effective_to and journal.accounting_date > account.effective_to:
            raise BusinessRuleError(f"Account {account} ceased to be effective on {account.effective_to}.")
        line_branch = line.branch_id or journal.branch_id
        if account.branch_id and line_branch != account.branch_id:
            raise BusinessRuleError(f"Account {account} is restricted to branch {account.branch}.")
        if account.department_id and line.department_id != account.department_id:
            raise BusinessRuleError(f"Account {account} is restricted to {account.department}.")


def period_for(institution, day: date, *, lock=False) -> FinancialPeriod | None:
    qs = FinancialPeriod.objects.filter(institution=institution, start_date__lte=day, end_date__gte=day)
    if lock:
        qs = qs.select_for_update()
    return qs.first()


# --------------------------------------------------------------------------
# Workflow
# --------------------------------------------------------------------------


def _locked(journal) -> Journal:
    return Journal.objects.select_for_update().select_related("institution").get(pk=journal.pk)


def submit_journal(journal: Journal, user, comment="") -> Journal:
    with transaction.atomic():
        journal = _locked(journal)
        require_perm(user, journal.institution, "journal.create")
        if journal.status not in Journal.EDITABLE_STATUSES:
            raise BusinessRuleError("Only draft or rejected journals can be submitted.")
        if journal.prepared_by_id != user.pk:
            raise BusinessRuleError("Only the preparer can submit this journal.")
        validate_journal(journal)
        period = period_for(journal.institution, journal.accounting_date)
        if period is None:
            raise BusinessRuleError(f"No financial period covers {journal.accounting_date}.")
        if period.status != FinancialPeriod.Status.OPEN:
            raise BusinessRuleError(f"Financial period {period} is closed.")
        if not journal.number:
            journal.number = next_number(
                journal.institution, f"journal-{journal.accounting_date.year}", f"JV-{journal.accounting_date.year}-"
            )
        journal.period = period
        journal.status = Journal.Status.SUBMITTED
        journal.submitted_at = timezone.now()
        journal.rejection_reason = ""
        journal.save()
        record_decision(journal, Decision.SUBMITTED, user, comment)
    return journal


def approve_journal(journal: Journal, user, comment="") -> Journal:
    with transaction.atomic():
        journal = _locked(journal)
        require_perm(user, journal.institution, "journal.approve")
        if journal.status != Journal.Status.SUBMITTED:
            raise BusinessRuleError("Only submitted journals can be approved.")
        assert_independent(journal.institution, journal.prepared_by, user, "this journal")
        validate_journal(journal)
        journal.status = Journal.Status.APPROVED
        journal.approved_by = user
        journal.approved_at = timezone.now()
        journal.save()
        record_decision(journal, Decision.APPROVED, user, comment)
    return journal


def reject_journal(journal: Journal, user, reason: str) -> Journal:
    if not reason or not reason.strip():
        raise BusinessRuleError("A rejection reason is required.")
    with transaction.atomic():
        journal = _locked(journal)
        require_perm(user, journal.institution, "journal.approve")
        if journal.status not in (Journal.Status.SUBMITTED, Journal.Status.APPROVED):
            raise BusinessRuleError("Only submitted or approved (unposted) journals can be rejected.")
        assert_independent(journal.institution, journal.prepared_by, user, "this journal")
        journal.status = Journal.Status.REJECTED
        journal.rejection_reason = reason
        journal.approved_by = None
        journal.approved_at = None
        journal.save()
        record_decision(journal, Decision.REJECTED, user, reason)
    return journal


def post_journal(journal: Journal, user, comment="") -> Journal:
    """
    Atomically post an approved journal to the ledger.

    The journal row and its financial period are locked for the duration of the
    transaction, so concurrent posting attempts are serialised and a period
    cannot be closed while a posting into it is in progress. The one-to-one
    link between ledger entries and journal lines makes double posting
    impossible at database level as well.
    """
    with transaction.atomic():
        journal = _locked(journal)
        institution = journal.institution
        require_perm(user, institution, "journal.post")
        if journal.status in (Journal.Status.POSTED, Journal.Status.REVERSED):
            raise BusinessRuleError(f"Journal {journal} has already been posted.", code="already_posted")
        if journal.status != Journal.Status.APPROVED:
            raise BusinessRuleError("Only approved journals can be posted.")
        assert_independent(institution, journal.prepared_by, user, "this journal")
        validate_journal(journal)

        period = period_for(institution, journal.accounting_date, lock=True)
        if period is None:
            raise BusinessRuleError(f"No financial period covers {journal.accounting_date}.")
        if period.status != FinancialPeriod.Status.OPEN:
            raise BusinessRuleError(f"Financial period {period} is closed; ordinary postings are rejected.")
        limit = timezone.localdate() - timedelta(days=institution.backdate_limit_days)
        if journal.accounting_date < limit and not has_perm(user, institution, "journal.post_backdated"):
            raise BusinessRuleError(
                f"Accounting date {journal.accounting_date} is more than {institution.backdate_limit_days} days in "
                "the past; posting requires backdated-posting permission."
            )

        now = timezone.now()
        entries = [
            LedgerEntry(
                institution=institution,
                journal=journal,
                line=line,
                account=line.account,
                period=period,
                accounting_date=journal.accounting_date,
                debit=line.base_debit,
                credit=line.base_credit,
                txn_currency=journal.currency,
                txn_debit=line.debit,
                txn_credit=line.credit,
                branch=line.branch or journal.branch,
                department=line.department,
                posted_at=now,
            )
            for line in journal.lines.select_related("account", "branch")
        ]
        try:
            LedgerEntry.objects.bulk_create(entries)
        except IntegrityError as exc:
            raise BusinessRuleError("Journal lines are already posted.", code="already_posted") from exc

        journal.period = period
        journal.status = Journal.Status.POSTED
        journal.posted_by = user
        journal.posted_at = now
        journal.save()

        if journal.reversal_of_id:
            original = Journal.objects.select_for_update().get(pk=journal.reversal_of_id)
            if original.status != Journal.Status.POSTED:
                raise BusinessRuleError("The original journal is no longer in posted status.")
            original.status = Journal.Status.REVERSED
            original.save(update_fields=["status", "updated_at"])
            log_activity("journal.reversed", user=user, obj=original, details={"reversal": journal.number})
        record_decision(journal, Decision.POSTED, user, comment)
    return journal


def request_reversal(journal: Journal, user, reason: str, reversal_date: date | None = None) -> Journal:
    """Create a reversing journal (submitted for independent approval)."""
    if not reason or not reason.strip():
        raise BusinessRuleError("A reversal reason is required.")
    with transaction.atomic():
        original = _locked(journal)
        institution = original.institution
        require_perm(user, institution, "journal.reverse")
        if original.status != Journal.Status.POSTED:
            raise BusinessRuleError("Only posted journals can be reversed.")
        if Journal.objects.filter(reversal_of=original).exists():
            raise BusinessRuleError("A reversal already exists for this journal.")
        reversal_date = reversal_date or timezone.localdate()
        reversal = Journal.objects.create(
            institution=institution,
            journal_type=JournalType.REVERSAL,
            transaction_date=reversal_date,
            accounting_date=reversal_date,
            description=f"Reversal of {original.number}: {reason}"[:500],
            source_reference=original.number,
            currency=original.currency,
            exchange_rate=original.exchange_rate,
            branch=original.branch,
            prepared_by=user,
            reversal_of=original,
        )
        JournalLine.objects.bulk_create(
            JournalLine(
                journal=reversal,
                line_no=line.line_no,
                account=line.account,
                description=f"Reversal: {line.description}"[:255],
                debit=line.credit,
                credit=line.debit,
                base_debit=line.base_credit,
                base_credit=line.base_debit,
                branch=line.branch,
                department=line.department,
            )
            for line in original.lines.all()
        )
        record_decision(original, Decision.REVERSAL_REQUESTED, user, reason)
        # The reversal enters the approval queue directly; its preparer is the requester.
        period = period_for(institution, reversal_date)
        if period is None or period.status != FinancialPeriod.Status.OPEN:
            raise BusinessRuleError(f"No open financial period covers the reversal date {reversal_date}.")
        reversal.number = next_number(institution, f"journal-{reversal_date.year}", f"JV-{reversal_date.year}-")
        reversal.period = period
        reversal.status = Journal.Status.SUBMITTED
        reversal.submitted_at = timezone.now()
        reversal.save()
        record_decision(reversal, Decision.SUBMITTED, user, reason)
    return reversal


# --------------------------------------------------------------------------
# Periods
# --------------------------------------------------------------------------


def create_period(institution, user, *, name, start_date, end_date, is_year_end=False) -> FinancialPeriod:
    require_perm(user, institution, "period.manage")
    if end_date < start_date:
        raise BusinessRuleError("Period end date precedes start date.")
    overlap = FinancialPeriod.objects.filter(
        institution=institution, start_date__lte=end_date, end_date__gte=start_date
    ).exists()
    if overlap:
        raise BusinessRuleError("Financial periods must not overlap.")
    period = FinancialPeriod.objects.create(
        institution=institution, name=name, start_date=start_date, end_date=end_date, is_year_end=is_year_end
    )
    log_activity("period.created", user=user, obj=period)
    return period


def generate_monthly_periods(institution, user, year: int) -> list[FinancialPeriod]:
    start_month = institution.financial_year_start_month
    periods = []
    for i in range(12):
        month = (start_month - 1 + i) % 12 + 1
        yr = year + ((start_month - 1 + i) // 12)
        start = date(yr, month, 1)
        end = (date(yr + (month == 12), month % 12 + 1, 1)) - timedelta(days=1)
        if FinancialPeriod.objects.filter(institution=institution, start_date=start).exists():
            continue
        periods.append(
            create_period(
                institution, user, name=start.strftime("%b %Y"), start_date=start, end_date=end, is_year_end=(i == 11)
            )
        )
    return periods


def close_period(period: FinancialPeriod, user, comment="") -> FinancialPeriod:
    with transaction.atomic():
        period = FinancialPeriod.objects.select_for_update().select_related("institution").get(pk=period.pk)
        require_perm(user, period.institution, "period.close")
        if period.status == FinancialPeriod.Status.CLOSED:
            raise BusinessRuleError("Period is already closed.")
        earlier_open = FinancialPeriod.objects.filter(
            institution=period.institution, end_date__lt=period.start_date, status=FinancialPeriod.Status.OPEN
        ).exists()
        if earlier_open:
            raise BusinessRuleError("Close earlier periods first.")
        pending = Journal.objects.filter(
            institution=period.institution,
            accounting_date__gte=period.start_date,
            accounting_date__lte=period.end_date,
            status__in=[Journal.Status.SUBMITTED, Journal.Status.APPROVED],
        ).count()
        if pending:
            raise BusinessRuleError(
                f"{pending} submitted/approved journal(s) in this period are not yet posted. "
                "Post or reject them before closing."
            )
        period.status = FinancialPeriod.Status.CLOSED
        period.closed_by = user
        period.closed_at = timezone.now()
        period.save()
        record_decision(period, Decision.CLOSED, user, comment)
    return period


def reopen_period(period: FinancialPeriod, user, reason: str) -> FinancialPeriod:
    if not reason or not reason.strip():
        raise BusinessRuleError("A reason is required to reopen a period.")
    with transaction.atomic():
        period = FinancialPeriod.objects.select_for_update().select_related("institution").get(pk=period.pk)
        require_perm(user, period.institution, "period.reopen")
        if period.status != FinancialPeriod.Status.CLOSED:
            raise BusinessRuleError("Only closed periods can be reopened.")
        later_closed = FinancialPeriod.objects.filter(
            institution=period.institution, start_date__gt=period.end_date, status=FinancialPeriod.Status.CLOSED
        ).exists()
        if later_closed:
            raise BusinessRuleError("Reopen later periods first.")
        assert_independent(period.institution, period.closed_by, user, "the period close")
        period.status = FinancialPeriod.Status.OPEN
        period.reopened_by = user
        period.reopened_at = timezone.now()
        period.reopen_reason = reason
        period.save()
        record_decision(period, Decision.REOPENED, user, reason)
    return period


# --------------------------------------------------------------------------
# Year-end
# --------------------------------------------------------------------------


def generate_year_end_closing_journal(
    institution, user, *, year_start: date, year_end: date, retained_earnings: Account
) -> Journal:
    """
    Prepare (not post) a closing journal that transfers income and expense
    balances for the year to retained earnings. It follows the normal approval
    and posting workflow.
    """
    if retained_earnings.institution_id != institution.pk or retained_earnings.category != AccountCategory.EQUITY:
        raise BusinessRuleError("Retained earnings must be an equity account of this institution.")
    balances = (
        LedgerEntry.objects.filter(
            institution=institution,
            accounting_date__gte=year_start,
            accounting_date__lte=year_end,
            account__category__in=[AccountCategory.INCOME, AccountCategory.EXPENSE],
        )
        .values("account")
        .annotate(dr=Sum("debit"), cr=Sum("credit"))
    )
    accounts = {a.pk: a for a in Account.objects.filter(pk__in=[b["account"] for b in balances])}
    lines, net = [], ZERO
    for row in balances:
        balance = (row["dr"] or ZERO) - (row["cr"] or ZERO)  # debit-positive
        if balance == ZERO:
            continue
        account = accounts[row["account"]]
        if balance > 0:
            lines.append(LineInput(account=account, credit=balance, description="Year-end close"))
        else:
            lines.append(LineInput(account=account, debit=-balance, description="Year-end close"))
        net += balance
    if not lines:
        raise BusinessRuleError("There are no income or expense balances to close.")
    if net > 0:  # net loss
        lines.append(LineInput(account=retained_earnings, debit=net, description="Net loss for the year"))
    elif net < 0:
        lines.append(LineInput(account=retained_earnings, credit=-net, description="Net profit for the year"))
    return create_journal(
        institution,
        user,
        transaction_date=year_end,
        accounting_date=year_end,
        description=f"Year-end closing entries {year_start:%Y-%m-%d} to {year_end:%Y-%m-%d}",
        lines=lines,
        journal_type=JournalType.YEAR_END,
    )
