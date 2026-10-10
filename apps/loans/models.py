from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import InstitutionScopedModel

MONEY = {"max_digits": 20, "decimal_places": 2}
ZERO = Decimal("0.00")


class ClassificationBand(InstitutionScopedModel):
    """
    Days-past-due band with its provisioning rate. Defaults are seeded from the
    institution type and MUST be validated against the current CBN prudential
    guidelines and the institution's IFRS 9 policy before use.
    """

    name = models.CharField(max_length=60)
    min_days = models.PositiveIntegerField()
    max_days = models.PositiveIntegerField(null=True, blank=True, help_text="Blank: no upper limit")
    provision_rate = models.DecimalField(max_digits=6, decimal_places=2, help_text="Percent of outstanding exposure")
    is_performing = models.BooleanField(default=False)

    class Meta:
        ordering = ["min_days"]
        constraints = [models.UniqueConstraint(fields=["institution", "min_days"], name="uniq_band_start")]

    def __str__(self):
        return self.name


class LoanSnapshot(InstitutionScopedModel):
    """A loan portfolio listing from the core banking system as at a date."""

    class Status(models.TextChoices):
        LOADED = "LOADED", "Loaded"
        REVIEWED = "REVIEWED", "Reviewed and signed off"
        SUPERSEDED = "SUPERSEDED", "Superseded"

    as_of = models.DateField()
    source_file = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64)
    principal_account = models.ForeignKey("accounting.Account", on_delete=models.PROTECT, related_name="+")
    interest_account = models.ForeignKey(
        "accounting.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    provision_account = models.ForeignKey(
        "accounting.Account",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="Impairment allowance (contra-asset) account",
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.LOADED)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_comment = models.TextField(blank=True)
    provision_journal = models.ForeignKey(
        "accounting.Journal", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-as_of", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["institution", "as_of"], condition=~Q(status="SUPERSEDED"), name="uniq_current_loan_snapshot"
            )
        ]

    def __str__(self):
        return f"Loan portfolio {self.as_of}"


class LoanRecord(models.Model):
    snapshot = models.ForeignKey(LoanSnapshot, on_delete=models.CASCADE, related_name="records")
    row_number = models.PositiveIntegerField()
    loan_account = models.CharField(max_length=40)
    customer_name = models.CharField(max_length=200, blank=True)
    product = models.CharField(max_length=80, blank=True)
    branch = models.ForeignKey("organizations.Branch", null=True, blank=True, on_delete=models.PROTECT)
    branch_code = models.CharField(max_length=20, blank=True)
    principal = models.DecimalField(**MONEY)
    interest_receivable = models.DecimalField(**MONEY, default=ZERO)
    days_past_due = models.PositiveIntegerField(default=0)
    classification = models.CharField(max_length=60)
    provision_rate = models.DecimalField(max_digits=6, decimal_places=2)
    provision_required = models.DecimalField(**MONEY)

    class Meta:
        ordering = ["row_number"]
        indexes = [models.Index(fields=["snapshot", "classification"])]
