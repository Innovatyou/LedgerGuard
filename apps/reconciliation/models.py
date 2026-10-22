import re
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.core.models import InstitutionScopedModel
from apps.imports.models import Side, SourceType

MONEY = {"max_digits": 20, "decimal_places": 2}
ZERO = Decimal("0.00")


class Strategy(models.TextChoices):
    EXACT_REFERENCE = "EXACT_REFERENCE", "Exact reference and exact amount"
    EXTERNAL_ID = "EXTERNAL_ID", "External transaction ID and amount"
    AMOUNT_DATE = "AMOUNT_DATE", "Exact amount within date tolerance"
    NARRATION_AMOUNT = "NARRATION_AMOUNT", "Normalised narration and exact amount"
    COMPOSITE = "COMPOSITE", "Configurable composite key"
    ONE_TO_MANY = "ONE_TO_MANY", "One-to-many (split / partial settlement)"
    MANY_TO_ONE = "MANY_TO_ONE", "Many-to-one (aggregated posting)"
    FEE_NET = "FEE_NET", "Gross / fee / net settlement"
    REVERSAL_PAIR = "REVERSAL_PAIR", "Reversal and counter-entry (same side)"
    NARRATION_SIMILARITY = "NARRATION_SIMILARITY", "Similar narration (fuzzy, always manual review)"
    SESSION_BULK = "SESSION_BULK", "Session bulk settlement (items per settlement session vs one bulk line)"
    BULK_SUBSET = "BULK_SUBSET", "Bulk amount finder (items adding up to a bulk line; always reviewed)"


# Strategies whose unique, zero-difference results may be auto-accepted when the rule allows it.
AUTO_ELIGIBLE = {
    Strategy.EXACT_REFERENCE,
    Strategy.EXTERNAL_ID,
    Strategy.NARRATION_AMOUNT,
    Strategy.COMPOSITE,
    Strategy.ONE_TO_MANY,
    Strategy.MANY_TO_ONE,
    Strategy.FEE_NET,
    Strategy.REVERSAL_PAIR,
    Strategy.SESSION_BULK,
}

REFERENCE_FIELDS = {
    "transaction_id": "norm_transaction_id",
    "external_reference": "norm_external_reference",
    "settlement_batch_ref": "settlement_batch_ref",
    "counterparty_reference": "counterparty_reference",
}

COMPOSITE_FIELDS = {
    "transaction_date",
    "value_date",
    "norm_transaction_id",
    "norm_external_reference",
    "norm_narration",
    "counterparty_reference",
    "settlement_batch_ref",
    "channel",
    "currency",
}


class ReconType(models.TextChoices):
    BANK = "BANK", "Bank statement vs general ledger"
    CONTROL = "CONTROL", "Core banking control totals vs accounting"
    SETTLEMENT = "SETTLEMENT", "Settlement / payment channel"
    SUSPENSE = "SUSPENSE", "Suspense / control account"
    OTHER = "OTHER", "Other dataset"


class MatchingRule(InstitutionScopedModel):
    name = models.CharField(max_length=120)
    strategy = models.CharField(max_length=25, choices=Strategy.choices)
    priority = models.PositiveIntegerField(default=100, help_text="Lower numbers run first.")
    is_active = models.BooleanField(default=True)
    recon_type = models.CharField(
        max_length=12, choices=ReconType.choices, blank=True, help_text="Blank: applies to all reconciliation types."
    )
    date_tolerance_days = models.PositiveIntegerField(default=0)
    amount_tolerance = models.DecimalField(
        **MONEY, default=ZERO, help_text="Matches with a non-zero difference always need review."
    )
    auto_accept = models.BooleanField(
        default=False, help_text="Auto-accept unique, zero-difference results (eligible strategies only)."
    )
    left_reference_field = models.CharField(max_length=30, default="transaction_id")
    right_reference_field = models.CharField(max_length=30, default="transaction_id")
    composite_fields = models.JSONField(default=list, blank=True)
    max_group_size = models.PositiveIntegerField(default=50)
    similarity_threshold = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal("0.85"))
    bulk_side = models.CharField(
        max_length=5,
        choices=[("LEFT", "Left (e.g. settlement account statement)"), ("RIGHT", "Right")],
        default="LEFT",
        help_text="Session bulk / amount finder: the side that shows bulk lines.",
    )
    session_cutoffs = models.JSONField(
        default=list,
        blank=True,
        help_text='Session bulk: settlement session cut-off times, e.g. ["02:00", "05:00", "08:00", ...]. '
        "A transaction belongs to the first session whose cut-off is at or after its time; "
        "items after the last cut-off belong to the first session of the next day.",
    )
    session_boundary_minutes = models.PositiveIntegerField(
        default=0,
        help_text="Session bulk: items this close to a cut-off may have settled in the neighbouring session.",
    )
    session_label_format = models.CharField(
        max_length=8,
        choices=[
            ("NONE", "Not used"),
            ("NIBSS", 'NIBSS narration ("31.08.2026 11th SESSION")'),
            ("PATTERN", "Custom pattern"),
        ],
        default="NONE",
        help_text="Session bulk: read the session date and number from the bulk line's narration, so each "
        "line settles exactly the session it names.",
    )
    session_label_pattern = models.CharField(
        max_length=200,
        blank=True,
        help_text="Custom session label: a regular expression on the normalised narration (upper case, "
        "punctuation as spaces) with named groups DD, MO, YYYY (or YY) and N.",
    )
    fee_keywords = models.JSONField(
        default=list,
        blank=True,
        help_text='Session bulk: narration words identifying charge lines, e.g. ["CHARGE", "FEE", "VAT"]. '
        "Used when charges settle as a separate bulk line.",
    )
    description = models.TextField(blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["priority", "id"]

    def __str__(self):
        return f"{self.priority}: {self.name}"

    def clean(self):
        errors = {}
        cutoffs = self.session_cutoffs or []
        if not isinstance(cutoffs, list) or not all(
            isinstance(c, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d|24:00", c) for c in cutoffs
        ):
            errors["session_cutoffs"] = 'Enter cut-off times as a list of "HH:MM" values (24:00 = end of day).'
        elif cutoffs != sorted(set(cutoffs)):
            errors["session_cutoffs"] = "Cut-off times must be in increasing order without repeats."
        elif self.strategy == Strategy.SESSION_BULK and not cutoffs:
            errors["session_cutoffs"] = "Session bulk matching needs the settlement session cut-off times."
        if self.session_label_format == "PATTERN":
            try:
                groups = set(re.compile(self.session_label_pattern or "").groupindex)
            except re.error as exc:
                errors["session_label_pattern"] = f"Invalid regular expression: {exc}"
            else:
                if not ({"DD", "MO", "N"} <= groups and groups & {"YYYY", "YY"}):
                    errors["session_label_pattern"] = "The pattern needs named groups DD, MO, YYYY (or YY) and N."
        keywords = self.fee_keywords or []
        if not isinstance(keywords, list) or not all(isinstance(k, str) and k.strip() for k in keywords):
            errors["fee_keywords"] = "Enter fee keywords as a list of words."
        if errors:
            raise ValidationError(errors)

    def snapshot(self):
        return {
            "id": self.pk,
            "name": self.name,
            "strategy": self.strategy,
            "priority": self.priority,
            "date_tolerance_days": self.date_tolerance_days,
            "amount_tolerance": str(self.amount_tolerance),
            "auto_accept": self.auto_accept,
            "left_reference_field": self.left_reference_field,
            "right_reference_field": self.right_reference_field,
            "composite_fields": self.composite_fields,
            "similarity_threshold": str(self.similarity_threshold),
            "max_group_size": self.max_group_size,
            "bulk_side": self.bulk_side,
            "session_cutoffs": self.session_cutoffs,
            "session_boundary_minutes": self.session_boundary_minutes,
            "session_label_format": self.session_label_format,
            "session_label_pattern": self.session_label_pattern,
            "fee_keywords": self.fee_keywords,
        }


class Reconciliation(InstitutionScopedModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PROCESSING = "PROCESSING", "Processing"
        IN_REVIEW = "IN_REVIEW", "Exceptions under review"
        SUBMITTED = "SUBMITTED", "Submitted for approval"
        APPROVED = "APPROVED", "Approved"
        REOPENED = "REOPENED", "Reopened"
        CLOSED = "CLOSED", "Closed"

    WORKABLE = (Status.DRAFT, Status.IN_REVIEW, Status.REOPENED)

    reference = models.CharField(max_length=30, blank=True)
    name = models.CharField(max_length=200)
    recon_type = models.CharField(max_length=12, choices=ReconType.choices, default=ReconType.BANK)
    left_label = models.CharField(max_length=60, default="Bank statement")
    left_source_type = models.CharField(max_length=20, choices=SourceType.choices, default=SourceType.BANK_STATEMENT)
    left_source_system = models.CharField(max_length=60, blank=True)
    left_account_reference = models.CharField(max_length=60)
    left_inflow_side = models.CharField(
        max_length=6,
        choices=Side.choices,
        default=Side.CREDIT,
        help_text="Which side represents money coming in, in the left source.",
    )
    right_label = models.CharField(max_length=60, default="General ledger")
    right_source_type = models.CharField(max_length=20, choices=SourceType.choices, default=SourceType.GL_EXPORT)
    right_source_system = models.CharField(max_length=60, blank=True)
    right_account_reference = models.CharField(max_length=60)
    right_inflow_side = models.CharField(max_length=6, choices=Side.choices, default=Side.DEBIT)
    gl_account = models.ForeignKey(
        "accounting.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    currency = models.CharField(max_length=3, default="NGN")
    period_start = models.DateField()
    period_end = models.DateField()
    opening_left_balance = models.DecimalField(**MONEY, default=ZERO)
    closing_left_balance = models.DecimalField(
        **MONEY, null=True, blank=True, help_text="Closing balance reported by the source (inflow-positive)."
    )
    opening_right_balance = models.DecimalField(**MONEY, default=ZERO)
    closing_right_balance = models.DecimalField(**MONEY, null=True, blank=True)
    rules = models.ManyToManyField(
        MatchingRule,
        blank=True,
        related_name="reconciliations",
        help_text="Leave empty to use all active rules for this type.",
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT, db_index=True)
    prepared_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    run_summary = models.JSONField(default=dict, blank=True)
    previous = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-period_end", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["institution", "reference"], condition=~Q(reference=""), name="uniq_recon_reference"
            ),
            models.CheckConstraint(condition=Q(period_end__gte=models.F("period_start")), name="recon_period_valid"),
        ]

    def __str__(self):
        return f"{self.reference or 'REC'} {self.name}"

    @property
    def is_workable(self):
        return self.status in self.WORKABLE


class MatchGroup(models.Model):
    class Category(models.TextChoices):
        EXACT = "EXACT", "Exact match"
        HIGH_CONFIDENCE = "HIGH_CONFIDENCE", "High-confidence candidate"
        MANUAL_REVIEW = "MANUAL_REVIEW", "Manual review required"
        MANUAL = "MANUAL", "Manual match"

    class Status(models.TextChoices):
        PROPOSED = "PROPOSED", "Proposed (awaiting review)"
        ACCEPTED = "ACCEPTED", "Accepted"
        REJECTED = "REJECTED", "Rejected / unmatched"

    class Kind(models.TextChoices):
        MATCH = "MATCH", "Cross-source match"
        REVERSAL = "REVERSAL", "Reversal pair (same source)"

    reconciliation = models.ForeignKey(Reconciliation, on_delete=models.CASCADE, related_name="match_groups")
    rule = models.ForeignKey(MatchingRule, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    rule_snapshot = models.JSONField(default=dict, blank=True)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.MATCH)
    category = models.CharField(max_length=16, choices=Category.choices)
    status = models.CharField(max_length=10, choices=Status.choices, db_index=True)
    left_total = models.DecimalField(**MONEY, default=ZERO)
    right_total = models.DecimalField(**MONEY, default=ZERO)
    difference = models.DecimalField(**MONEY, default=ZERO)
    signature = models.CharField(max_length=2000, db_index=True)
    reason = models.CharField(max_length=255, blank=True, help_text="Why the engine proposed this match")
    note = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"Match #{self.pk} ({self.get_category_display()}, {self.get_status_display()})"


class MatchGroupItem(models.Model):
    class Side(models.TextChoices):
        LEFT = "LEFT", "Left"
        RIGHT = "RIGHT", "Right"

    group = models.ForeignKey(MatchGroup, on_delete=models.CASCADE, related_name="items")
    transaction = models.ForeignKey("imports.SourceTransaction", on_delete=models.PROTECT, related_name="match_items")
    side = models.CharField(max_length=5, choices=Side.choices)
    flow_amount = models.DecimalField(**MONEY)
    active = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["transaction"], condition=Q(active=True), name="uniq_active_match_per_transaction"
            ),
        ]


class ReconExclusion(models.Model):
    reconciliation = models.ForeignKey(Reconciliation, on_delete=models.CASCADE, related_name="exclusions")
    transaction = models.ForeignKey("imports.SourceTransaction", on_delete=models.PROTECT, related_name="+")
    side = models.CharField(max_length=5, choices=MatchGroupItem.Side.choices)
    reason = models.TextField()
    excluded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["reconciliation", "transaction"], name="uniq_recon_exclusion")]


class ReconAdjustment(models.Model):
    reconciliation = models.ForeignKey(Reconciliation, on_delete=models.CASCADE, related_name="adjustments")
    side = models.CharField(max_length=5, choices=MatchGroupItem.Side.choices)
    description = models.CharField(max_length=255)
    amount = models.DecimalField(**MONEY, help_text="Inflow-positive adjustment to that side's balance")
    reference = models.CharField(max_length=120, blank=True)
    journal = models.ForeignKey("accounting.Journal", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
