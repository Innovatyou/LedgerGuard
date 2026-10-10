from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q

from apps.core.models import InstitutionScopedModel

MONEY = {"max_digits": 20, "decimal_places": 2}
ZERO = Decimal("0.00")


class AccountCategory(models.TextChoices):
    ASSET = "ASSET", "Assets"
    LIABILITY = "LIABILITY", "Liabilities"
    EQUITY = "EQUITY", "Equity"
    INCOME = "INCOME", "Income"
    EXPENSE = "EXPENSE", "Expenses"


DEBIT_NORMAL_CATEGORIES = {AccountCategory.ASSET, AccountCategory.EXPENSE}


class NormalBalance(models.TextChoices):
    DEBIT = "DEBIT", "Debit"
    CREDIT = "CREDIT", "Credit"


class AccountSubcategory(models.TextChoices):
    CASH = "CASH", "Cash and cash equivalents"
    BALANCES_WITH_CBN = "BALANCES_WITH_CBN", "Balances with the Central Bank"
    DUE_FROM_BANKS = "DUE_FROM_BANKS", "Balances with other financial institutions"
    INVESTMENTS = "INVESTMENTS", "Investment securities / treasury bills"
    LOANS_ADVANCES = "LOANS_ADVANCES", "Loans and advances"
    LOAN_IMPAIRMENT = "LOAN_IMPAIRMENT", "Impairment allowance (contra-asset)"
    FIXED_ASSETS = "FIXED_ASSETS", "Property and equipment"
    ACCUMULATED_DEPRECIATION = "ACCUMULATED_DEPRECIATION", "Accumulated depreciation (contra-asset)"
    SETTLEMENT = "SETTLEMENT", "Settlement and clearing accounts"
    SUSPENSE = "SUSPENSE", "Suspense accounts"
    OTHER_ASSETS = "OTHER_ASSETS", "Other assets"
    CUSTOMER_DEPOSITS = "CUSTOMER_DEPOSITS", "Customer deposits"
    BORROWINGS = "BORROWINGS", "Borrowings"
    PROVISIONS = "PROVISIONS", "Provisions"
    TAX_PAYABLE = "TAX_PAYABLE", "Tax payable"
    OTHER_LIABILITIES = "OTHER_LIABILITIES", "Other liabilities"
    SHARE_CAPITAL = "SHARE_CAPITAL", "Share capital"
    RESERVES = "RESERVES", "Statutory and other reserves"
    RETAINED_EARNINGS = "RETAINED_EARNINGS", "Retained earnings"
    INTEREST_INCOME = "INTEREST_INCOME", "Interest income"
    FEE_COMMISSION_INCOME = "FEE_COMMISSION_INCOME", "Fees and commission income"
    OTHER_INCOME = "OTHER_INCOME", "Other income"
    INTEREST_EXPENSE = "INTEREST_EXPENSE", "Interest expense"
    FEE_COMMISSION_EXPENSE = "FEE_COMMISSION_EXPENSE", "Fees and commission expense"
    IMPAIRMENT_CHARGE = "IMPAIRMENT_CHARGE", "Impairment charges"
    PERSONNEL_EXPENSE = "PERSONNEL_EXPENSE", "Personnel expenses"
    DEPRECIATION_EXPENSE = "DEPRECIATION_EXPENSE", "Depreciation and amortisation"
    OPERATING_EXPENSE = "OPERATING_EXPENSE", "Other operating expenses"
    INCOME_TAX = "INCOME_TAX", "Income tax expense"
    OTHER = "OTHER", "Other"


class AccountType(models.TextChoices):
    HEADER = "HEADER", "Header (summary, non-posting)"
    POSTING = "POSTING", "Posting"


class CashFlowClass(models.TextChoices):
    NONE = "", "Not applicable"
    CASH = "CASH", "Cash and cash equivalents"
    OPERATING = "OPERATING", "Operating activities"
    INVESTING = "INVESTING", "Investing activities"
    FINANCING = "FINANCING", "Financing activities"


class Account(InstitutionScopedModel):
    PROTECTED_WHEN_USED = ("code", "category", "normal_balance", "currency", "account_type", "institution_id")

    code = models.CharField(max_length=30)
    name = models.CharField(max_length=200)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    category = models.CharField(max_length=10, choices=AccountCategory.choices)
    subcategory = models.CharField(max_length=40, choices=AccountSubcategory.choices, default=AccountSubcategory.OTHER)
    account_type = models.CharField(max_length=10, choices=AccountType.choices, default=AccountType.POSTING)
    normal_balance = models.CharField(max_length=6, choices=NormalBalance.choices)
    currency = models.CharField(max_length=3, blank=True, help_text="Blank: postings allowed in any currency.")
    branch = models.ForeignKey(
        "organizations.Branch",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        help_text="Restrict postings to one branch (blank: all branches).",
    )
    department = models.ForeignKey(
        "organizations.Department",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        help_text="Restrict postings to one department/cost centre (blank: any).",
    )
    is_active = models.BooleanField(default=True)
    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)
    reconciliation_required = models.BooleanField(default=False)
    is_control_account = models.BooleanField(default=False)
    is_suspense_account = models.BooleanField(default=False)
    cash_flow_class = models.CharField(max_length=10, choices=CashFlowClass.choices, blank=True, default="")
    fs_line = models.CharField("Financial-statement line", max_length=100, blank=True)
    regulatory_code = models.CharField(max_length=40, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["code"]
        constraints = [models.UniqueConstraint(fields=["institution", "code"], name="uniq_account_code")]

    def __str__(self):
        return f"{self.code} {self.name}"

    @property
    def is_debit_normal(self):
        return self.normal_balance == NormalBalance.DEBIT

    def has_postings(self):
        return self.pk is not None and LedgerEntry.objects.filter(account_id=self.pk).exists()

    def clean(self):
        errors = {}
        if self.parent_id:
            if self.parent.institution_id != self.institution_id:
                errors["parent"] = "Parent account belongs to a different institution."
            elif self.parent.account_type != AccountType.HEADER:
                errors["parent"] = "Parent account must be a header account."
            elif self.parent.category != self.category:
                errors["parent"] = "Parent account must have the same category."
            ancestor = self.parent
            while ancestor is not None:
                if ancestor.pk == self.pk:
                    errors["parent"] = "An account cannot be its own ancestor."
                    break
                ancestor = ancestor.parent
        for field in ("branch", "department"):
            related = getattr(self, field)
            if related is not None and related.institution_id != self.institution_id:
                errors[field] = "Belongs to a different institution."
        if self.effective_to and self.effective_from and self.effective_to < self.effective_from:
            errors["effective_to"] = "Effective-to date precedes effective-from date."
        if self.pk:
            original = Account.objects.filter(pk=self.pk).values(*self.PROTECTED_WHEN_USED).first()
            if original and self.has_postings():
                changed = [f for f in self.PROTECTED_WHEN_USED if original[f] != getattr(self, f)]
                if changed:
                    errors["__all__"] = (
                        "This account has posted transactions; the following fields cannot be changed: "
                        + ", ".join(changed)
                    )
            if self.account_type == AccountType.HEADER and self.has_postings():
                errors["account_type"] = "An account with postings cannot become a header account."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self.normal_balance:
            self.normal_balance = (
                NormalBalance.DEBIT if self.category in DEBIT_NORMAL_CATEGORIES else NormalBalance.CREDIT
            )
        self.currency = (self.currency or "").upper()
        self.full_clean(exclude=["institution", "created_by", "approved_by"])
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.has_postings() or JournalLine.objects.filter(account=self).exists():
            raise ValidationError("Accounts used in journals cannot be deleted; deactivate them instead.")
        return super().delete(*args, **kwargs)


class FinancialPeriod(InstitutionScopedModel):
    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        CLOSED = "CLOSED", "Closed"

    name = models.CharField(max_length=40)
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    is_year_end = models.BooleanField(default=False)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    reopened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reopened_at = models.DateTimeField(null=True, blank=True)
    reopen_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["start_date"]
        constraints = [
            models.UniqueConstraint(fields=["institution", "start_date"], name="uniq_period_start"),
            models.CheckConstraint(condition=Q(end_date__gte=models.F("start_date")), name="period_dates_valid"),
        ]

    def __str__(self):
        return self.name

    def contains(self, day):
        return self.start_date <= day <= self.end_date


class JournalType(models.TextChoices):
    MANUAL = "MANUAL", "Manual journal"
    ADJUSTMENT = "ADJUSTMENT", "Adjustment"
    ACCRUAL = "ACCRUAL", "Accrual"
    PREPAYMENT = "PREPAYMENT", "Prepayment"
    DEPRECIATION = "DEPRECIATION", "Depreciation"
    RECLASSIFICATION = "RECLASSIFICATION", "Reclassification"
    REVERSAL = "REVERSAL", "Reversal"
    IMPORTED = "IMPORTED", "Approved imported entries"
    RECURRING = "RECURRING", "Recurring template"
    YEAR_END = "YEAR_END", "Year-end closing"


class Journal(InstitutionScopedModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SUBMITTED = "SUBMITTED", "Submitted for approval"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        POSTED = "POSTED", "Posted"
        REVERSED = "REVERSED", "Posted and reversed"

    EDITABLE_STATUSES = (Status.DRAFT, Status.REJECTED)

    number = models.CharField(max_length=30, blank=True)
    journal_type = models.CharField(max_length=20, choices=JournalType.choices, default=JournalType.MANUAL)
    transaction_date = models.DateField()
    accounting_date = models.DateField()
    period = models.ForeignKey(
        FinancialPeriod, null=True, blank=True, on_delete=models.PROTECT, related_name="journals"
    )
    description = models.CharField(max_length=500)
    source_reference = models.CharField(max_length=120, blank=True)
    supporting_reference = models.CharField(max_length=255, blank=True, help_text="Evidence reference(s)")
    currency = models.CharField(max_length=3)
    exchange_rate = models.DecimalField(
        max_digits=18,
        decimal_places=8,
        default=Decimal("1"),
        validators=[MinValueValidator(Decimal("0.00000001"))],
        help_text="Units of base currency per one unit of the journal currency.",
    )
    branch = models.ForeignKey("organizations.Branch", null=True, blank=True, on_delete=models.PROTECT)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT, db_index=True)
    prepared_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    posted_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)
    reversal_of = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversed_by"
    )
    idempotency_key = models.CharField(max_length=80, null=True, blank=True)

    class Meta:
        ordering = ["-accounting_date", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["institution", "number"], condition=~Q(number=""), name="uniq_journal_number"
            ),
            models.UniqueConstraint(
                fields=["institution", "idempotency_key"],
                condition=Q(idempotency_key__isnull=False),
                name="uniq_journal_idempotency_key",
            ),
        ]

    def __str__(self):
        return self.number or f"Draft journal #{self.pk}"

    @property
    def is_editable(self):
        return self.status in self.EDITABLE_STATUSES

    def totals(self):
        agg = self.lines.aggregate(
            debit=models.Sum("debit"),
            credit=models.Sum("credit"),
            base_debit=models.Sum("base_debit"),
            base_credit=models.Sum("base_credit"),
        )
        return {k: (v or ZERO) for k, v in agg.items()}


class JournalLine(models.Model):
    journal = models.ForeignKey(Journal, on_delete=models.CASCADE, related_name="lines")
    line_no = models.PositiveIntegerField()
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="journal_lines")
    description = models.CharField(max_length=255, blank=True)
    debit = models.DecimalField(**MONEY, default=ZERO)
    credit = models.DecimalField(**MONEY, default=ZERO)
    base_debit = models.DecimalField(**MONEY, default=ZERO)
    base_credit = models.DecimalField(**MONEY, default=ZERO)
    branch = models.ForeignKey("organizations.Branch", null=True, blank=True, on_delete=models.PROTECT)
    department = models.ForeignKey("organizations.Department", null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        ordering = ["line_no"]
        constraints = [
            models.UniqueConstraint(fields=["journal", "line_no"], name="uniq_journal_line_no"),
            models.CheckConstraint(
                condition=(Q(debit__gt=0, credit=0) | Q(credit__gt=0, debit=0)),
                name="journal_line_one_sided_positive",
            ),
        ]

    def __str__(self):
        return f"{self.journal} line {self.line_no}"


class LedgerEntry(models.Model):
    """
    Posted general-ledger entry. Created only by the posting service; immutable.
    Amounts are in the institution's base currency; the transaction-currency
    amounts are retained for reference.
    """

    institution = models.ForeignKey("organizations.Institution", on_delete=models.PROTECT, related_name="+")
    journal = models.ForeignKey(Journal, on_delete=models.PROTECT, related_name="ledger_entries")
    line = models.OneToOneField(JournalLine, on_delete=models.PROTECT, related_name="ledger_entry")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="ledger_entries")
    period = models.ForeignKey(FinancialPeriod, on_delete=models.PROTECT, related_name="ledger_entries")
    accounting_date = models.DateField(db_index=True)
    debit = models.DecimalField(**MONEY, default=ZERO)
    credit = models.DecimalField(**MONEY, default=ZERO)
    txn_currency = models.CharField(max_length=3)
    txn_debit = models.DecimalField(**MONEY, default=ZERO)
    txn_credit = models.DecimalField(**MONEY, default=ZERO)
    branch = models.ForeignKey("organizations.Branch", null=True, blank=True, on_delete=models.PROTECT)
    department = models.ForeignKey("organizations.Department", null=True, blank=True, on_delete=models.PROTECT)
    posted_at = models.DateTimeField()

    class Meta:
        ordering = ["accounting_date", "id"]
        indexes = [models.Index(fields=["institution", "account", "accounting_date"])]
        verbose_name_plural = "ledger entries"

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Ledger entries are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Ledger entries cannot be deleted; post a reversal instead.")
