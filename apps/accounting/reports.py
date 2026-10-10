"""
Financial reports computed exclusively from posted ledger entries.
"""

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db.models import Q, Sum

from .models import ZERO, Account, AccountCategory, AccountSubcategory, LedgerEntry


@dataclass
class TBRow:
    account: Account
    debit_total: Decimal
    credit_total: Decimal

    @property
    def net(self) -> Decimal:  # debit-positive
        return self.debit_total - self.credit_total

    @property
    def balance_debit(self) -> Decimal:
        return self.net if self.net > 0 else ZERO

    @property
    def balance_credit(self) -> Decimal:
        return -self.net if self.net < 0 else ZERO


@dataclass
class TrialBalance:
    as_of: date
    rows: list[TBRow]
    total_debit: Decimal = ZERO
    total_credit: Decimal = ZERO

    @property
    def is_balanced(self) -> bool:
        return self.total_debit == self.total_credit


def _entries(institution, date_from=None, date_to=None, branch=None):
    qs = LedgerEntry.objects.filter(institution=institution)
    if date_from:
        qs = qs.filter(accounting_date__gte=date_from)
    if date_to:
        qs = qs.filter(accounting_date__lte=date_to)
    if branch is not None:
        qs = qs.filter(branch=branch)
    return qs


def account_movements(institution, date_from=None, date_to=None, branch=None) -> dict[int, tuple[Decimal, Decimal]]:
    rows = (
        _entries(institution, date_from, date_to, branch).values("account").annotate(dr=Sum("debit"), cr=Sum("credit"))
    )
    return {r["account"]: (r["dr"] or ZERO, r["cr"] or ZERO) for r in rows}


def trial_balance(institution, as_of: date, branch=None) -> TrialBalance:
    movements = account_movements(institution, date_to=as_of, branch=branch)
    accounts = Account.objects.filter(institution=institution, pk__in=movements.keys()).order_by("code")
    rows = []
    for account in accounts:
        dr, cr = movements[account.pk]
        rows.append(TBRow(account, dr, cr))
    tb = TrialBalance(as_of=as_of, rows=rows)
    tb.total_debit = sum((r.balance_debit for r in rows), ZERO)
    tb.total_credit = sum((r.balance_credit for r in rows), ZERO)
    return tb


def natural_balance(account: Account, debit: Decimal, credit: Decimal) -> Decimal:
    """Balance expressed in the account's normal-balance direction."""
    return debit - credit if account.is_debit_normal else credit - debit


@dataclass
class LedgerLine:
    entry: LedgerEntry
    running_balance: Decimal


@dataclass
class GeneralLedger:
    account: Account
    date_from: date | None
    date_to: date | None
    opening_balance: Decimal
    lines: list[LedgerLine]
    closing_balance: Decimal
    total_debit: Decimal
    total_credit: Decimal


def general_ledger(institution, account: Account, date_from: date | None, date_to: date | None) -> GeneralLedger:
    if account.institution_id != institution.pk:
        raise ValueError("Account belongs to a different institution.")
    opening = ZERO
    if date_from:
        agg = LedgerEntry.objects.filter(account=account, accounting_date__lt=date_from).aggregate(
            dr=Sum("debit"), cr=Sum("credit")
        )
        opening = natural_balance(account, agg["dr"] or ZERO, agg["cr"] or ZERO)
    qs = LedgerEntry.objects.filter(account=account).select_related("journal")
    if date_from:
        qs = qs.filter(accounting_date__gte=date_from)
    if date_to:
        qs = qs.filter(accounting_date__lte=date_to)
    running = opening
    lines = []
    total_dr = total_cr = ZERO
    for entry in qs.order_by("accounting_date", "id"):
        running += natural_balance(account, entry.debit, entry.credit)
        total_dr += entry.debit
        total_cr += entry.credit
        lines.append(LedgerLine(entry, running))
    return GeneralLedger(account, date_from, date_to, opening, lines, running, total_dr, total_cr)


@dataclass
class StatementSection:
    title: str
    lines: "OrderedDict[str, list[tuple[Account, Decimal]]]" = field(default_factory=OrderedDict)
    total: Decimal = ZERO

    def add(self, group: str, account: Account, amount: Decimal):
        self.lines.setdefault(group, []).append((account, amount))
        self.total += amount

    def group_totals(self):
        return [(g, sum((a for _, a in items), ZERO), items) for g, items in self.lines.items()]


def _group_label(account: Account) -> str:
    return account.fs_line or AccountSubcategory(account.subcategory).label


@dataclass
class IncomeStatement:
    date_from: date
    date_to: date
    income: StatementSection
    expenses: StatementSection

    @property
    def net_profit(self) -> Decimal:
        return self.income.total - self.expenses.total


def income_statement(institution, date_from: date, date_to: date, branch=None) -> IncomeStatement:
    movements = account_movements(institution, date_from, date_to, branch)
    income = StatementSection("Income")
    expenses = StatementSection("Expenses")
    accounts = Account.objects.filter(
        institution=institution,
        pk__in=movements.keys(),
        category__in=[AccountCategory.INCOME, AccountCategory.EXPENSE],
    ).order_by("code")
    for account in accounts:
        dr, cr = movements[account.pk]
        if account.category == AccountCategory.INCOME:
            income.add(_group_label(account), account, cr - dr)
        else:
            expenses.add(_group_label(account), account, dr - cr)
    return IncomeStatement(date_from, date_to, income, expenses)


@dataclass
class FinancialPosition:
    as_of: date
    assets: StatementSection
    liabilities: StatementSection
    equity: StatementSection
    unclosed_earnings: Decimal

    @property
    def total_equity(self) -> Decimal:
        return self.equity.total + self.unclosed_earnings

    @property
    def total_liabilities_and_equity(self) -> Decimal:
        return self.liabilities.total + self.total_equity

    @property
    def difference(self) -> Decimal:
        return self.assets.total - self.total_liabilities_and_equity

    @property
    def is_balanced(self) -> bool:
        return self.difference == ZERO


def statement_of_financial_position(institution, as_of: date, branch=None) -> FinancialPosition:
    movements = account_movements(institution, date_to=as_of, branch=branch)
    assets = StatementSection("Assets")
    liabilities = StatementSection("Liabilities")
    equity = StatementSection("Equity")
    earnings = ZERO
    for account in Account.objects.filter(institution=institution, pk__in=movements.keys()).order_by("code"):
        dr, cr = movements[account.pk]
        if account.category == AccountCategory.ASSET:
            assets.add(_group_label(account), account, dr - cr)
        elif account.category == AccountCategory.LIABILITY:
            liabilities.add(_group_label(account), account, cr - dr)
        elif account.category == AccountCategory.EQUITY:
            equity.add(_group_label(account), account, cr - dr)
        else:
            # Income/expense balances not yet closed to retained earnings.
            earnings += cr - dr
    return FinancialPosition(as_of, assets, liabilities, equity, earnings)


def account_balance(account: Account, as_of: date | None = None) -> Decimal:
    q = Q(account=account)
    if as_of:
        q &= Q(accounting_date__lte=as_of)
    agg = LedgerEntry.objects.filter(q).aggregate(dr=Sum("debit"), cr=Sum("credit"))
    return natural_balance(account, agg["dr"] or ZERO, agg["cr"] or ZERO)
