from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.core.models import InstitutionScopedModel, TimeStampedModel


class InstitutionType(models.TextChoices):
    COMMERCIAL_BANK = "COMMERCIAL_BANK", "Commercial bank"
    MFB = "MFB", "Microfinance bank"
    OFI = "OFI", "Other financial institution"
    FINANCE_COMPANY = "FINANCE_COMPANY", "Finance company"
    OTHER = "OTHER", "Other approved category"


class RoundingMode(models.TextChoices):
    HALF_UP = "HALF_UP", "Round half up"
    HALF_EVEN = "HALF_EVEN", "Round half to even (banker's rounding)"


class Institution(TimeStampedModel):
    name = models.CharField(max_length=200)
    code = models.SlugField(max_length=30, unique=True, help_text="Short unique code, e.g. 'acme-mfb'.")
    institution_type = models.CharField(max_length=20, choices=InstitutionType.choices)
    licence_number = models.CharField(max_length=60, blank=True)
    rc_number = models.CharField("RC number", max_length=30, blank=True)
    address = models.TextField(blank=True)
    base_currency = models.CharField(max_length=3, default="NGN")
    amount_decimal_places = models.PositiveSmallIntegerField(default=2, validators=[MaxValueValidator(4)])
    rounding_mode = models.CharField(max_length=10, choices=RoundingMode.choices, default=RoundingMode.HALF_UP)
    financial_year_start_month = models.PositiveSmallIntegerField(
        default=1, validators=[MinValueValidator(1), MaxValueValidator(12)]
    )
    backdate_limit_days = models.PositiveIntegerField(
        default=5,
        help_text="Journals with an accounting date older than this many days need 'post backdated' permission.",
    )
    reconciliation_tolerance = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Maximum unexplained difference permitted when approving a reconciliation.",
    )
    require_independent_approval = models.BooleanField(
        default=True, help_text="Maker-checker: preparers may not approve their own work."
    )
    require_config_approval = models.BooleanField(
        default=True,
        help_text="Chart-of-accounts and matching-rule changes take effect only after independent approval.",
    )
    require_mfa = models.BooleanField(default=False)
    document_retention_years = models.PositiveSmallIntegerField(default=7)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Branch(InstitutionScopedModel):
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=150)
    state = models.CharField(max_length=60, blank=True)
    address = models.TextField(blank=True)
    is_head_office = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]
        verbose_name_plural = "branches"
        constraints = [models.UniqueConstraint(fields=["institution", "code"], name="uniq_branch_code")]

    def __str__(self):
        return f"{self.code} – {self.name}"


class Department(InstitutionScopedModel):
    class Kind(models.TextChoices):
        DEPARTMENT = "DEPARTMENT", "Department"
        COST_CENTRE = "COST_CENTRE", "Cost centre"

    code = models.CharField(max_length=20)
    name = models.CharField(max_length=150)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.DEPARTMENT)
    branch = models.ForeignKey(Branch, null=True, blank=True, on_delete=models.PROTECT, related_name="departments")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]
        constraints = [models.UniqueConstraint(fields=["institution", "code"], name="uniq_department_code")]

    def __str__(self):
        return f"{self.code} – {self.name}"
