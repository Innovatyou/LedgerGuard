from django.conf import settings
from django.db import models

from apps.core.models import InstitutionScopedModel

DISCLAIMER = (
    "Workpaper template configured by the institution. LedgerGuard prepares supporting workpapers only; it does not "
    "submit returns. Validate line definitions against the regulator's current return format before relying on them."
)


class RegulatoryTemplate(InstitutionScopedModel):
    class Regulator(models.TextChoices):
        CBN = "CBN", "Central Bank of Nigeria"
        NDIC = "NDIC", "Nigeria Deposit Insurance Corporation"
        FIRS = "FIRS", "Federal Inland Revenue Service"
        INTERNAL = "INTERNAL", "Internal / management"
        OTHER = "OTHER", "Other"

    class Frequency(models.TextChoices):
        MONTHLY = "MONTHLY", "Monthly"
        QUARTERLY = "QUARTERLY", "Quarterly"
        ANNUAL = "ANNUAL", "Annual"
        AD_HOC = "AD_HOC", "Ad hoc"

    code = models.CharField(max_length=40)
    name = models.CharField(max_length=200)
    regulator = models.CharField(max_length=10, choices=Regulator.choices)
    frequency = models.CharField(max_length=10, choices=Frequency.choices, default=Frequency.MONTHLY)
    description = models.TextField(blank=True, default=DISCLAIMER)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]
        constraints = [models.UniqueConstraint(fields=["institution", "code"], name="uniq_regulatory_template")]

    def __str__(self):
        return f"{self.code} {self.name}"


class TemplateLine(models.Model):
    class Kind(models.TextChoices):
        ACCOUNTS = "ACCOUNTS", "Sum of ledger balances"
        FORMULA = "FORMULA", "Formula of other lines"
        MANUAL = "MANUAL", "Manual input"
        HEADING = "HEADING", "Heading (no value)"

    class Basis(models.TextChoices):
        CLOSING = "CLOSING", "Closing balance at period end"
        MOVEMENT = "MOVEMENT", "Movement during the period"

    class Sign(models.TextChoices):
        DEBIT_POSITIVE = "DEBIT_POSITIVE", "Debit positive (assets, expenses)"
        CREDIT_POSITIVE = "CREDIT_POSITIVE", "Credit positive (liabilities, equity, income)"

    template = models.ForeignKey(RegulatoryTemplate, on_delete=models.CASCADE, related_name="lines")
    code = models.CharField(max_length=20, help_text="Line code used in formulas, e.g. A100")
    label = models.CharField(max_length=200)
    order = models.PositiveIntegerField(default=0)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.ACCOUNTS)
    regulatory_codes = models.JSONField(
        default=list, blank=True, help_text="Account.regulatory_code prefixes to include"
    )
    subcategories = models.JSONField(default=list, blank=True, help_text="Account subcategories to include")
    account_codes = models.JSONField(default=list, blank=True, help_text="Account code prefixes to include")
    categories = models.JSONField(
        default=list,
        blank=True,
        help_text="Restrict to these account categories (alone: include every account in them)",
    )
    basis = models.CharField(max_length=10, choices=Basis.choices, default=Basis.CLOSING)
    sign = models.CharField(max_length=16, choices=Sign.choices, default=Sign.DEBIT_POSITIVE)
    formula = models.CharField(max_length=500, blank=True, help_text="e.g. A100 + A200 - A300")
    is_total = models.BooleanField(default=False)

    class Meta:
        ordering = ["order", "id"]
        constraints = [models.UniqueConstraint(fields=["template", "code"], name="uniq_template_line_code")]

    def __str__(self):
        return f"{self.code} {self.label}"


class ValidationRule(models.Model):
    template = models.ForeignKey(RegulatoryTemplate, on_delete=models.CASCADE, related_name="rules")
    name = models.CharField(max_length=200)
    expression = models.CharField(max_length=500, help_text="e.g. A900 = L900 + E900, or R10 >= 10")
    tolerance = models.DecimalField(max_digits=20, decimal_places=2, default=0)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.name


class RegulatoryReturn(InstitutionScopedModel):
    """A prepared workpaper for one template and reporting period."""

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PREPARED = "PREPARED", "Prepared – awaiting review"
        REVIEWED = "REVIEWED", "Reviewed and signed off"

    template = models.ForeignKey(RegulatoryTemplate, on_delete=models.PROTECT, related_name="returns")
    period_start = models.DateField()
    period_end = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    values = models.JSONField(default=dict, blank=True)
    manual_values = models.JSONField(default=dict, blank=True)
    validation_results = models.JSONField(default=list, blank=True)
    generated_at = models.DateTimeField(null=True, blank=True)
    prepared_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-period_end", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["institution", "template", "period_end"], name="uniq_return_period")
        ]

    def __str__(self):
        return f"{self.template.code} {self.period_end}"

    @property
    def passed(self):
        return all(r.get("passed") for r in self.validation_results)
