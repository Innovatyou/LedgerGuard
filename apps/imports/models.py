from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import InstitutionScopedModel

MONEY = {"max_digits": 20, "decimal_places": 2}


class SourceType(models.TextChoices):
    BANK_STATEMENT = "BANK_STATEMENT", "Bank statement"
    GL_EXPORT = "GL_EXPORT", "Core banking GL export"
    SETTLEMENT_REPORT = "SETTLEMENT_REPORT", "Settlement report"
    CONTROL_TOTALS = "CONTROL_TOTALS", "Control totals"
    OTHER = "OTHER", "Other approved dataset"


class Channel(models.TextChoices):
    NONE = "", "Not applicable"
    NIP = "NIP", "NIP (NIBSS Instant Payment)"
    NIBSS = "NIBSS", "Other NIBSS settlement"
    POS = "POS", "POS"
    ATM = "ATM", "ATM"
    CARD = "CARD", "Card scheme"
    INTERBANK = "INTERBANK", "Interbank clearing"
    PROCESSOR = "PROCESSOR", "Payment processor"
    OTHER = "OTHER", "Other channel"


class AmountMode(models.TextChoices):
    SEPARATE = "SEPARATE", "Separate debit and credit columns"
    SIGNED = "SIGNED", "Single signed amount column"
    INDICATOR = "INDICATOR", "Amount column plus DR/CR indicator column"


class Side(models.TextChoices):
    DEBIT = "DEBIT", "Debit"
    CREDIT = "CREDIT", "Credit"


STANDARD_FIELDS = {
    "account_reference": "Account reference",
    "transaction_id": "Transaction identifier",
    "external_reference": "External reference",
    "transaction_date": "Transaction date",
    "value_date": "Value date",
    "posting_date": "Posting date",
    "debit_amount": "Debit amount",
    "credit_amount": "Credit amount",
    "amount": "Signed / unsigned amount",
    "dr_cr_indicator": "DR/CR indicator",
    "currency": "Currency",
    "narration": "Narration",
    "counterparty_reference": "Counterparty reference",
    "channel": "Channel",
    "settlement_batch_ref": "Settlement batch reference",
    "gross_amount": "Gross amount",
    "fee_amount": "Fee amount",
    "tax_amount": "Tax amount",
    "net_amount": "Net settlement amount",
    "settlement_date": "Settlement date",
    "is_reversal": "Reversal flag",
}


class ImportProfile(InstitutionScopedModel):
    name = models.CharField(max_length=120)
    source_type = models.CharField(max_length=20, choices=SourceType.choices)
    source_system = models.CharField(max_length=60, help_text="e.g. 'Core banking GL', 'Correspondent bank statement'")
    channel = models.CharField(max_length=12, choices=Channel.choices, blank=True, default="")
    column_mapping = models.JSONField(default=dict, help_text="Standard field → source column header")
    header_row = models.PositiveIntegerField(default=1)
    sheet_name = models.CharField(max_length=100, blank=True)
    date_formats = models.JSONField(default=list, blank=True, help_text='e.g. ["%d/%m/%Y", "%d-%b-%Y"]')
    amount_mode = models.CharField(max_length=10, choices=AmountMode.choices, default=AmountMode.SEPARATE)
    signed_positive_is = models.CharField(
        max_length=6,
        choices=Side.choices,
        default=Side.CREDIT,
        help_text="For signed amounts: which side a positive value represents in this source.",
    )
    indicator_debit_values = models.JSONField(default=list, blank=True)
    indicator_credit_values = models.JSONField(default=list, blank=True)
    default_currency = models.CharField(max_length=3, default="NGN")
    default_account_reference = models.CharField(max_length=60, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["institution", "name"], name="uniq_import_profile_name")]

    def __str__(self):
        return self.name


def import_upload_path(instance, filename):
    return f"imports/{instance.institution_id}/{instance.sha256[:2]}/{instance.sha256}{instance.extension}"


class ImportBatch(InstitutionScopedModel):
    class Status(models.TextChoices):
        UPLOADED = "UPLOADED", "Uploaded"
        VALIDATED = "VALIDATED", "Validated"
        COMMITTED = "COMMITTED", "Committed"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"

    profile = models.ForeignKey(ImportProfile, on_delete=models.PROTECT, related_name="batches")
    file = models.FileField(upload_to=import_upload_path, null=True, blank=True)
    original_filename = models.CharField(max_length=255)
    extension = models.CharField(max_length=8)
    sha256 = models.CharField(max_length=64, db_index=True)
    size = models.PositiveBigIntegerField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.UPLOADED)
    total_rows = models.PositiveIntegerField(default=0)
    valid_rows = models.PositiveIntegerField(default=0)
    invalid_rows = models.PositiveIntegerField(default=0)
    duplicate_rows = models.PositiveIntegerField(default=0)
    warning_rows = models.PositiveIntegerField(default=0)
    committed_rows = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)
    summary = models.JSONField(default=dict, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    validated_at = models.DateTimeField(null=True, blank=True)
    committed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    committed_at = models.DateTimeField(null=True, blank=True)
    file_purged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "import batches"
        constraints = [
            models.UniqueConstraint(
                fields=["institution", "sha256"],
                condition=Q(status="COMMITTED"),
                name="uniq_committed_file_per_institution",
            )
        ]

    def __str__(self):
        return f"Batch #{self.pk} {self.original_filename}"


class ImportRow(models.Model):
    class Status(models.TextChoices):
        VALID = "VALID", "Valid"
        WARNING = "WARNING", "Valid with warnings (suspected duplicate)"
        INVALID = "INVALID", "Invalid"
        DUPLICATE = "DUPLICATE", "Duplicate of a previously imported transaction"
        EXCLUDED = "EXCLUDED", "Excluded by user"
        COMMITTED = "COMMITTED", "Committed"

    batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="rows")
    row_number = models.PositiveIntegerField()
    raw = models.JSONField(default=dict)
    normalized = models.JSONField(default=dict)
    status = models.CharField(max_length=10, choices=Status.choices)
    errors = models.JSONField(default=list)
    warnings = models.JSONField(default=list)
    fingerprint = models.CharField(max_length=64, blank=True)
    corrected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    corrected_at = models.DateTimeField(null=True, blank=True)
    correction_note = models.TextField(blank=True)

    class Meta:
        ordering = ["row_number"]
        constraints = [models.UniqueConstraint(fields=["batch", "row_number"], name="uniq_batch_row")]
        indexes = [models.Index(fields=["batch", "status"])]


class SourceTransaction(InstitutionScopedModel):
    """
    A validated, normalised transaction imported from an external source.

    Imported records are *source data* for reconciliation: they are never
    treated as LedgerGuard's own general ledger.
    """

    batch = models.ForeignKey(ImportBatch, on_delete=models.PROTECT, related_name="transactions")
    import_row = models.OneToOneField(ImportRow, null=True, on_delete=models.PROTECT, related_name="transaction")
    source_type = models.CharField(max_length=20, choices=SourceType.choices)
    source_system = models.CharField(max_length=60)
    source_file = models.CharField(max_length=255)
    source_row_number = models.PositiveIntegerField()
    account_reference = models.CharField(max_length=60, db_index=True)
    transaction_id = models.CharField(max_length=120, blank=True)
    external_reference = models.CharField(max_length=120, blank=True)
    norm_transaction_id = models.CharField(max_length=120, blank=True, db_index=True)
    norm_external_reference = models.CharField(max_length=120, blank=True, db_index=True)
    transaction_date = models.DateField(db_index=True)
    value_date = models.DateField(null=True, blank=True)
    posting_date = models.DateField(null=True, blank=True)
    debit_amount = models.DecimalField(**MONEY, default=0)
    credit_amount = models.DecimalField(**MONEY, default=0)
    signed_amount = models.DecimalField(**MONEY, help_text="credit − debit, in the source's own convention")
    currency = models.CharField(max_length=3)
    narration = models.TextField(blank=True)
    norm_narration = models.TextField(blank=True)
    counterparty_reference = models.CharField(max_length=120, blank=True)
    channel = models.CharField(max_length=12, choices=Channel.choices, blank=True, default="")
    settlement_batch_ref = models.CharField(max_length=120, blank=True, db_index=True)
    gross_amount = models.DecimalField(**MONEY, null=True, blank=True)
    fee_amount = models.DecimalField(**MONEY, null=True, blank=True)
    tax_amount = models.DecimalField(**MONEY, null=True, blank=True)
    net_amount = models.DecimalField(**MONEY, null=True, blank=True)
    settlement_date = models.DateField(null=True, blank=True)
    is_reversal = models.BooleanField(default=False)
    source_values = models.JSONField(default=dict, help_text="Original source text for amounts and dates")
    fingerprint = models.CharField(max_length=64)
    is_suspected_duplicate = models.BooleanField(default=False)

    class Meta:
        ordering = ["transaction_date", "id"]
        constraints = [
            models.UniqueConstraint(fields=["institution", "fingerprint"], name="uniq_source_txn_fingerprint"),
            models.CheckConstraint(
                condition=Q(debit_amount__gte=0, credit_amount__gte=0), name="source_txn_amounts_non_negative"
            ),
        ]
        indexes = [models.Index(fields=["institution", "source_type", "account_reference", "transaction_date"])]

    def __str__(self):
        ref = self.transaction_id or self.external_reference or f"row {self.source_row_number}"
        return f"{self.transaction_date} {ref} {self.signed_amount}"
