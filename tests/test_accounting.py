from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction

from apps.accounting import reports, services
from apps.accounting.models import Account, AccountCategory, AccountType, FinancialPeriod, Journal, LedgerEntry
from apps.core.exceptions import (
    BusinessRuleError,
    PermissionDeniedError,
    SegregationOfDutiesError,
    TenantViolationError,
)

from .conftest import lines, make_user, post

pytestmark = pytest.mark.django_db
D = Decimal


def test_balanced_journal_full_workflow(institution, users, coa, periods, today):
    acct, mgr = users["accountant"], users["manager"]
    journal = services.create_journal(
        institution,
        acct,
        transaction_date=today,
        description="Accrue rent",
        lines=lines((coa["5601"], "D", "150000.00"), (coa["2301"], "C", "150000.00")),
    )
    assert journal.status == Journal.Status.DRAFT
    services.submit_journal(journal, acct)
    journal.refresh_from_db()
    assert journal.status == Journal.Status.SUBMITTED and journal.number.startswith(f"JV-{today.year}-")
    services.approve_journal(journal, mgr)
    services.post_journal(journal, mgr)
    journal.refresh_from_db()
    assert journal.status == Journal.Status.POSTED
    entries = LedgerEntry.objects.filter(journal=journal)
    assert entries.count() == 2
    assert sum(e.debit for e in entries) == sum(e.credit for e in entries) == D("150000.00")
    assert reports.account_balance(coa["5601"]) == D("150000.00")
    assert reports.account_balance(coa["2301"]) == D("150000.00")


def test_unbalanced_journal_cannot_be_submitted_or_posted(institution, users, coa, periods, today):
    journal = services.create_journal(
        institution,
        users["accountant"],
        transaction_date=today,
        description="Bad",
        lines=lines((coa["5601"], "D", "100.00"), (coa["2301"], "C", "99.99")),
    )
    with pytest.raises(BusinessRuleError, match="not balanced"):
        services.submit_journal(journal, users["accountant"])
    # Even if the status were forced, posting re-validates.
    Journal.objects.filter(pk=journal.pk).update(status=Journal.Status.APPROVED)
    with pytest.raises(BusinessRuleError, match="not balanced"):
        services.post_journal(journal, users["manager"])
    assert not LedgerEntry.objects.filter(journal=journal).exists()


def test_line_must_be_one_sided_and_positive(institution, users, coa, periods, today):
    with pytest.raises(BusinessRuleError):
        services.create_journal(
            institution,
            users["accountant"],
            transaction_date=today,
            description="x",
            lines=[services.LineInput(account=coa["5601"], debit=D("10"), credit=D("10"))],
        )
    with pytest.raises(BusinessRuleError):
        services.create_journal(
            institution,
            users["accountant"],
            transaction_date=today,
            description="x",
            lines=[services.LineInput(account=coa["5601"], debit=D("-10"))],
        )


def test_maker_checker_prevents_self_approval_and_self_posting(institution, users, coa, periods, today):
    mgr = users["manager"]  # holds create, approve and post permissions
    journal = services.create_journal(
        institution,
        mgr,
        transaction_date=today,
        description="Self",
        lines=lines((coa["5601"], "D", "10"), (coa["2301"], "C", "10")),
    )
    services.submit_journal(journal, mgr)
    with pytest.raises(SegregationOfDutiesError):
        services.approve_journal(journal, mgr)
    services.approve_journal(journal, users["cfo"])
    with pytest.raises(SegregationOfDutiesError):
        services.post_journal(journal, mgr)
    services.post_journal(journal, users["cfo"])


def test_permissions_enforced(institution, users, coa, periods, today):
    with pytest.raises(PermissionDeniedError):
        services.create_journal(
            institution,
            users["viewer"],
            transaction_date=today,
            description="x",
            lines=lines((coa["5601"], "D", "10"), (coa["2301"], "C", "10")),
        )
    journal = services.create_journal(
        institution,
        users["accountant"],
        transaction_date=today,
        description="x",
        lines=lines((coa["5601"], "D", "10"), (coa["2301"], "C", "10")),
    )
    services.submit_journal(journal, users["accountant"])
    with pytest.raises(PermissionDeniedError):
        services.approve_journal(journal, users["recon"])


def test_posted_journal_cannot_be_posted_twice(institution, users, coa, periods):
    journal = post(
        institution, users["accountant"], users["manager"], lines((coa["5601"], "D", "10"), (coa["2301"], "C", "10"))
    )
    with pytest.raises(BusinessRuleError, match="already been posted"):
        services.post_journal(journal, users["manager"])
    assert LedgerEntry.objects.filter(journal=journal).count() == 2


def test_double_posting_blocked_at_database_level(institution, users, coa, periods):
    journal = post(
        institution, users["accountant"], users["manager"], lines((coa["5601"], "D", "10"), (coa["2301"], "C", "10"))
    )
    line = journal.lines.first()
    entry = LedgerEntry.objects.filter(line=line).first()
    with pytest.raises(Exception), transaction.atomic():  # noqa: B017 - IntegrityError on one-to-one
        LedgerEntry.objects.create(
            institution=institution,
            journal=journal,
            line=line,
            account=line.account,
            period=entry.period,
            accounting_date=entry.accounting_date,
            debit=line.base_debit,
            credit=line.base_credit,
            txn_currency="NGN",
            posted_at=entry.posted_at,
        )


def test_posted_journal_editing_blocked(institution, users, coa, periods):
    journal = post(
        institution, users["accountant"], users["manager"], lines((coa["5601"], "D", "10"), (coa["2301"], "C", "10"))
    )
    with pytest.raises(BusinessRuleError, match="cannot be edited"):
        services.update_journal(journal, users["accountant"], description="changed")


def test_ledger_entries_are_immutable(institution, users, coa, periods):
    journal = post(
        institution, users["accountant"], users["manager"], lines((coa["5601"], "D", "10"), (coa["2301"], "C", "10"))
    )
    entry = LedgerEntry.objects.filter(journal=journal).first()
    entry.debit = D("999")
    with pytest.raises(ValidationError):
        entry.save()
    with pytest.raises(ValidationError):
        entry.delete()
    # The database itself rejects updates and deletes (PostgreSQL trigger).
    if connection.vendor == "postgresql":
        with pytest.raises(DatabaseError), transaction.atomic():
            LedgerEntry.objects.filter(pk=entry.pk).update(debit=D("999"))
        with pytest.raises(DatabaseError), transaction.atomic():
            LedgerEntry.objects.filter(pk=entry.pk).delete()


def test_closed_period_rejects_postings(institution, users, coa, periods, today):
    period = services.period_for(institution, today)
    journal = services.create_journal(
        institution,
        users["accountant"],
        transaction_date=today,
        description="x",
        lines=lines((coa["5601"], "D", "10"), (coa["2301"], "C", "10")),
    )
    services.submit_journal(journal, users["accountant"])
    services.approve_journal(journal, users["manager"])
    # Earlier open periods must be closed first.
    with pytest.raises(BusinessRuleError, match="earlier periods"):
        services.close_period(period, users["cfo"])
    for p in FinancialPeriod.objects.filter(institution=institution, end_date__lt=period.start_date):
        services.close_period(p, users["cfo"])
    # Pending approved journals block the close.
    with pytest.raises(BusinessRuleError, match="not yet posted"):
        services.close_period(period, users["cfo"])
    services.reject_journal(journal, users["manager"], "Wrong period")
    services.close_period(period, users["cfo"])
    journal.refresh_from_db()
    with pytest.raises(BusinessRuleError, match="closed"):
        services.submit_journal(journal, users["accountant"])
    Journal.objects.filter(pk=journal.pk).update(status=Journal.Status.APPROVED)
    with pytest.raises(BusinessRuleError, match="closed"):
        services.post_journal(journal, users["manager"])
    # Reopening needs a reason and an independent user.
    with pytest.raises(BusinessRuleError):
        services.reopen_period(period, users["cfo"], "")
    with pytest.raises(SegregationOfDutiesError):
        services.reopen_period(period, users["cfo"], "Audit adjustment")
    second_cfo = make_user("cfo2", institution, "cfo")
    services.reopen_period(period, second_cfo, "Audit adjustment")
    period.refresh_from_db()
    assert period.status == FinancialPeriod.Status.OPEN


def test_backdated_posting_requires_permission(institution, users, coa, periods, today):
    old = today - timedelta(days=institution.backdate_limit_days + 3)
    if services.period_for(institution, old) is None:
        pytest.skip("no period for backdated date")
    journal = services.create_journal(
        institution,
        users["accountant"],
        transaction_date=old,
        description="old",
        lines=lines((coa["5601"], "D", "10"), (coa["2301"], "C", "10")),
    )
    services.submit_journal(journal, users["accountant"])
    services.approve_journal(journal, users["manager"])
    with pytest.raises(BusinessRuleError, match="backdated"):
        services.post_journal(journal, users["manager"])  # finance manager lacks post_backdated
    services.post_journal(journal, users["cfo"])


def test_no_period_rejects_submission(institution, users, coa, today):
    journal = services.create_journal(
        institution,
        users["accountant"],
        transaction_date=today,
        description="x",
        lines=lines((coa["5601"], "D", "10"), (coa["2301"], "C", "10")),
    )
    with pytest.raises(BusinessRuleError, match="No financial period"):
        services.submit_journal(journal, users["accountant"])


def test_inactive_and_header_accounts_rejected(institution, users, coa, periods, today):
    header = coa["5000"]
    assert header.account_type == AccountType.HEADER
    journal = services.create_journal(
        institution,
        users["accountant"],
        transaction_date=today,
        description="x",
        lines=lines((header, "D", "10"), (coa["2301"], "C", "10")),
    )
    with pytest.raises(BusinessRuleError, match="header"):
        services.submit_journal(journal, users["accountant"])
    acct = coa["5602"]
    acct.is_active = False
    acct.save()
    journal2 = services.create_journal(
        institution,
        users["accountant"],
        transaction_date=today,
        description="x",
        lines=lines((acct, "D", "10"), (coa["2301"], "C", "10")),
    )
    with pytest.raises(BusinessRuleError, match="inactive"):
        services.submit_journal(journal2, users["accountant"])


def test_cross_institution_account_rejected(institution, other_institution, users, coa, periods, today):
    foreign = Account.objects.create(
        institution=other_institution, code="9999", name="Foreign", category=AccountCategory.ASSET
    )
    with pytest.raises(TenantViolationError):
        services.create_journal(
            institution,
            users["accountant"],
            transaction_date=today,
            description="x",
            lines=lines((foreign, "D", "10"), (coa["2301"], "C", "10")),
        )


def test_used_account_protected_fields(institution, users, coa, periods):
    post(institution, users["accountant"], users["manager"], lines((coa["5601"], "D", "10"), (coa["2301"], "C", "10")))
    account = Account.objects.get(pk=coa["5601"].pk)
    account.code = "5699"
    with pytest.raises(ValidationError):
        account.save()
    account = Account.objects.get(pk=coa["5601"].pk)
    account.category = AccountCategory.ASSET
    with pytest.raises(ValidationError):
        account.save()
    account = Account.objects.get(pk=coa["5601"].pk)
    account.name = "Rent, rates and premises"
    account.is_active = False
    account.save()  # descriptive changes and deactivation are allowed
    with pytest.raises(ValidationError):
        account.delete()


def test_reversal_workflow(institution, users, coa, periods):
    original = post(
        institution,
        users["accountant"],
        users["manager"],
        lines((coa["5601"], "D", "500.00"), (coa["2301"], "C", "500.00")),
    )
    with pytest.raises(BusinessRuleError):
        services.request_reversal(original, users["manager"], "")
    reversal = services.request_reversal(original, users["manager"], "Posted in error")
    assert reversal.status == Journal.Status.SUBMITTED and reversal.reversal_of_id == original.pk
    with pytest.raises(BusinessRuleError, match="already exists"):
        services.request_reversal(original, users["manager"], "again")
    with pytest.raises(SegregationOfDutiesError):
        services.approve_journal(reversal, users["manager"])
    services.approve_journal(reversal, users["cfo"])
    services.post_journal(reversal, users["cfo"])
    original.refresh_from_db()
    assert original.status == Journal.Status.REVERSED
    assert reports.account_balance(coa["5601"]) == D("0")
    assert LedgerEntry.objects.filter(journal=original).count() == 2  # history retained


def test_idempotency_key_prevents_duplicate_journals(institution, users, coa, periods, today):
    kwargs = dict(
        transaction_date=today,
        description="API",
        idempotency_key="req-123",
        lines=lines((coa["5601"], "D", "10"), (coa["2301"], "C", "10")),
    )
    first = services.create_journal(institution, users["accountant"], **kwargs)
    second = services.create_journal(institution, users["accountant"], **kwargs)
    assert first.pk == second.pk
    assert Journal.objects.filter(institution=institution, idempotency_key="req-123").count() == 1


def test_foreign_currency_journal_balances_in_base(institution, users, coa, periods, today):
    usd_account = coa["1201"]
    journal = services.create_journal(
        institution,
        users["accountant"],
        transaction_date=today,
        description="USD receipt",
        currency="USD",
        exchange_rate=D("1532.45"),
        lines=lines((usd_account, "D", "100.00"), (coa["4901"], "C", "100.00")),
    )
    totals = journal.totals()
    assert totals["base_debit"] == totals["base_credit"] == D("153245.00")
    with pytest.raises(BusinessRuleError):
        services.create_journal(
            institution,
            users["accountant"],
            transaction_date=today,
            description="x",
            currency="NGN",
            exchange_rate=D("2"),
            lines=lines((coa["5601"], "D", "10"), (coa["2301"], "C", "10")),
        )


def test_rounding_difference_in_base_currency_is_rejected(institution, users, coa, periods, today):
    journal = services.create_journal(
        institution,
        users["accountant"],
        transaction_date=today,
        description="rounding",
        currency="USD",
        exchange_rate=D("1.5"),
        lines=lines((coa["1201"], "D", "0.01"), (coa["1201"], "D", "0.01"), (coa["4901"], "C", "0.02")),
    )
    with pytest.raises(BusinessRuleError, match="base currency"):
        services.submit_journal(journal, users["accountant"])


def test_financial_statements(institution, users, coa, periods, today):
    a, m = users["accountant"], users["manager"]
    post(institution, a, m, lines((coa["1201"], "D", "1000000.00"), (coa["3101"], "C", "1000000.00")))
    post(institution, a, m, lines((coa["1301"], "D", "400000.00"), (coa["1201"], "C", "400000.00")))
    post(institution, a, m, lines((coa["1201"], "D", "60000.00"), (coa["4101"], "C", "60000.00")))
    post(institution, a, m, lines((coa["5401"], "D", "25000.00"), (coa["1201"], "C", "25000.00")))
    post(institution, a, m, lines((coa["1201"], "D", "300000.00"), (coa["2102"], "C", "300000.00")))

    tb = reports.trial_balance(institution, today)
    assert tb.is_balanced and tb.total_debit == D("1360000.00")  # 935k bank + 400k loans + 25k salaries

    is_ = reports.income_statement(institution, today.replace(day=1), today)
    assert is_.income.total == D("60000.00") and is_.expenses.total == D("25000.00")
    assert is_.net_profit == D("35000.00")

    fp = reports.statement_of_financial_position(institution, today)
    assert fp.assets.total == D("1335000.00")
    assert fp.liabilities.total == D("300000.00")
    assert fp.equity.total == D("1000000.00")
    assert fp.unclosed_earnings == D("35000.00")
    assert fp.is_balanced

    gl = reports.general_ledger(institution, coa["1201"], None, today)
    assert gl.closing_balance == D("935000.00")
    assert [ln.running_balance for ln in gl.lines] == [
        D("1000000.00"),
        D("600000.00"),
        D("660000.00"),
        D("635000.00"),
        D("935000.00"),
    ]


def test_year_end_closing_journal(institution, users, coa, periods, today):
    a, m = users["accountant"], users["manager"]
    post(institution, a, m, lines((coa["1201"], "D", "60000.00"), (coa["4101"], "C", "60000.00")))
    post(institution, a, m, lines((coa["5401"], "D", "25000.00"), (coa["1201"], "C", "25000.00")))
    closing = services.generate_year_end_closing_journal(
        institution, m, year_start=today.replace(month=1, day=1), year_end=today, retained_earnings=coa["3301"]
    )
    assert closing.status == Journal.Status.DRAFT
    services.submit_journal(closing, m)
    services.approve_journal(closing, users["cfo"])
    services.post_journal(closing, users["cfo"])
    fp = reports.statement_of_financial_position(institution, today)
    assert fp.unclosed_earnings == D("0.00")
    assert reports.account_balance(coa["3301"]) == D("35000.00")
    assert fp.is_balanced


def test_period_generation_and_overlap(institution, users):
    periods = services.generate_monthly_periods(institution, users["manager"], 2030)
    assert len(periods) == 12 and periods[-1].is_year_end
    assert periods[1].start_date.month == 2 and periods[1].end_date.day in (28, 29)
    with pytest.raises(BusinessRuleError, match="overlap"):
        services.create_period(
            institution, users["manager"], name="x", start_date=periods[0].start_date, end_date=periods[0].end_date
        )


def test_template_creates_hierarchy(institution, users, coa):
    assert coa["1301"].parent == coa["1300"]
    assert coa["1309"].normal_balance == "CREDIT"  # contra-asset
    assert coa["1590"].is_suspense_account
    # Applying again is idempotent.
    from apps.accounting.coa_templates import apply_template

    assert apply_template(institution, users["manager"], "MFB") == 0
