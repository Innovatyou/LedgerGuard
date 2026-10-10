from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import InstitutionScopedModel

MONEY = {"max_digits": 20, "decimal_places": 2}
ZERO = Decimal("0.00")


class DepreciationMethod(models.TextChoices):
    STRAIGHT_LINE = "STRAIGHT_LINE", "Straight line"
    REDUCING_BALANCE = "REDUCING_BALANCE", "Reducing balance"


class AssetCategory(InstitutionScopedModel):
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=120)
    cost_account = models.ForeignKey("accounting.Account", on_delete=models.PROTECT, related_name="+")
    accumulated_depreciation_account = models.ForeignKey(
        "accounting.Account", on_delete=models.PROTECT, related_name="+"
    )
    depreciation_expense_account = models.ForeignKey("accounting.Account", on_delete=models.PROTECT, related_name="+")
    disposal_account = models.ForeignKey(
        "accounting.Account", on_delete=models.PROTECT, related_name="+", help_text="Gain / loss on disposal"
    )
    method = models.CharField(
        max_length=20, choices=DepreciationMethod.choices, default=DepreciationMethod.STRAIGHT_LINE
    )
    useful_life_months = models.PositiveIntegerField(default=60)
    annual_rate = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal("0"), help_text="Percent per year (reducing balance only)"
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]
        constraints = [models.UniqueConstraint(fields=["institution", "code"], name="uniq_asset_category_code")]
        verbose_name_plural = "asset categories"

    def __str__(self):
        return f"{self.code} {self.name}"


class FixedAsset(InstitutionScopedModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "In use"
        DISPOSAL_PENDING = "DISPOSAL_PENDING", "Disposal awaiting approval"
        DISPOSED = "DISPOSED", "Disposed"

    tag = models.CharField(max_length=40, help_text="Asset tag / register number")
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    category = models.ForeignKey(AssetCategory, on_delete=models.PROTECT, related_name="assets")
    branch = models.ForeignKey("organizations.Branch", null=True, blank=True, on_delete=models.PROTECT)
    department = models.ForeignKey("organizations.Department", null=True, blank=True, on_delete=models.PROTECT)
    location = models.CharField(max_length=120, blank=True)
    supplier = models.CharField(max_length=150, blank=True)
    invoice_reference = models.CharField(max_length=80, blank=True)
    acquisition_date = models.DateField()
    in_service_date = models.DateField()
    cost = models.DecimalField(**MONEY)
    residual_value = models.DecimalField(**MONEY, default=ZERO)
    useful_life_months = models.PositiveIntegerField()
    method = models.CharField(max_length=20, choices=DepreciationMethod.choices)
    annual_rate = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0"))
    opening_accumulated_depreciation = models.DecimalField(
        **MONEY, default=ZERO, help_text="Depreciation charged before the asset was brought into LedgerGuard"
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    capitalisation_journal = models.ForeignKey(
        "accounting.Journal", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    disposal_date = models.DateField(null=True, blank=True)
    disposal_proceeds = models.DecimalField(**MONEY, null=True, blank=True)
    disposal_journal = models.ForeignKey(
        "accounting.Journal", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["tag"]
        constraints = [
            models.UniqueConstraint(fields=["institution", "tag"], name="uniq_asset_tag"),
            models.CheckConstraint(condition=Q(cost__gt=0), name="asset_cost_positive"),
            models.CheckConstraint(condition=Q(residual_value__gte=0), name="asset_residual_non_negative"),
        ]

    def __str__(self):
        return f"{self.tag} {self.name}"


class DepreciationRun(InstitutionScopedModel):
    class Status(models.TextChoices):
        PREPARED = "PREPARED", "Journal awaiting approval/posting"
        POSTED = "POSTED", "Posted"
        CANCELLED = "CANCELLED", "Cancelled"

    period = models.ForeignKey("accounting.FinancialPeriod", on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PREPARED)
    journal = models.ForeignKey("accounting.Journal", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    total = models.DecimalField(**MONEY, default=ZERO)
    prepared_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-period__start_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["institution", "period"], condition=~Q(status="CANCELLED"), name="uniq_active_depreciation_run"
            )
        ]

    def __str__(self):
        return f"Depreciation {self.period}"


class DepreciationLine(models.Model):
    run = models.ForeignKey(DepreciationRun, on_delete=models.CASCADE, related_name="lines")
    asset = models.ForeignKey(FixedAsset, on_delete=models.PROTECT, related_name="depreciation_lines")
    amount = models.DecimalField(**MONEY)
    nbv_before = models.DecimalField(**MONEY)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "asset"], name="uniq_depreciation_line")]
