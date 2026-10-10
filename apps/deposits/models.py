from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import InstitutionScopedModel

MONEY = {"max_digits": 20, "decimal_places": 2}
ZERO = Decimal("0.00")


class DepositProduct(InstitutionScopedModel):
    """Maps a core-banking deposit product code to its GL control account."""

    code = models.CharField(max_length=40)
    name = models.CharField(max_length=120)
    gl_account = models.ForeignKey("accounting.Account", on_delete=models.PROTECT, related_name="+")
    interest_payable_account = models.ForeignKey(
        "accounting.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["code"]
        constraints = [models.UniqueConstraint(fields=["institution", "code"], name="uniq_deposit_product")]

    def __str__(self):
        return f"{self.code} {self.name}"


class DepositSnapshot(InstitutionScopedModel):
    class Status(models.TextChoices):
        LOADED = "LOADED", "Loaded"
        REVIEWED = "REVIEWED", "Reviewed and signed off"
        SUPERSEDED = "SUPERSEDED", "Superseded"

    as_of = models.DateField()
    source_file = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.LOADED)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_comment = models.TextField(blank=True)

    class Meta:
        ordering = ["-as_of", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["institution", "as_of"],
                condition=~Q(status="SUPERSEDED"),
                name="uniq_current_deposit_snapshot",
            )
        ]

    def __str__(self):
        return f"Deposit listing {self.as_of}"


class DepositRecord(models.Model):
    snapshot = models.ForeignKey(DepositSnapshot, on_delete=models.CASCADE, related_name="records")
    row_number = models.PositiveIntegerField()
    account_number = models.CharField(max_length=40)
    customer_name = models.CharField(max_length=200, blank=True)
    product = models.CharField(max_length=40)
    branch = models.ForeignKey("organizations.Branch", null=True, blank=True, on_delete=models.PROTECT)
    branch_code = models.CharField(max_length=20, blank=True)
    balance = models.DecimalField(**MONEY, help_text="Credit (customer funds) positive; overdrawn negative")
    accrued_interest = models.DecimalField(**MONEY, default=ZERO)
    is_dormant = models.BooleanField(default=False)

    class Meta:
        ordering = ["row_number"]
        indexes = [models.Index(fields=["snapshot", "product"])]
