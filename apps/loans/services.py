"""
Loan portfolio control schedule: classify a core-banking loan listing by days
past due, compute required provisions, and compare the listing with the GL
control accounts. Provision adjustments are prepared as journals for approval.
"""

import hashlib
from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounting import reports
from apps.accounting.services import LineInput, create_journal, submit_journal
from apps.accounts.services import require_perm
from apps.approvals.services import assert_independent
from apps.core.activity import log_activity
from apps.core.exceptions import BusinessRuleError
from apps.core.money import quantize
from apps.core.tabular import Column, parse_records
from apps.organizations.models import Branch

from .models import ZERO, ClassificationBand, LoanRecord, LoanSnapshot

# Starting points only – verify against current CBN prudential guidelines and IFRS 9 policy.
DEFAULT_BANDS = {
    "MFB": [
        ("Performing", 0, 30, "1.00", True),
        ("Pass and watch", 31, 60, "5.00", False),
        ("Substandard", 61, 90, "20.00", False),
        ("Doubtful", 91, 180, "50.00", False),
        ("Lost", 181, None, "100.00", False),
    ],
    "DEFAULT": [
        ("Performing", 0, 89, "1.00", True),
        ("Substandard", 90, 179, "10.00", False),
        ("Doubtful", 180, 359, "50.00", False),
        ("Lost", 360, None, "100.00", False),
    ],
}

COLUMNS = [
    Column("loan_account", ("ACCOUNT_NO", "ACCOUNT_NUMBER", "LOAN_ACCOUNT_NO", "LOAN_ID"), required=True),
    Column("customer_name", ("CUSTOMER", "CUSTOMER_NAME", "BORROWER", "NAME")),
    Column("product", ("PRODUCT", "PRODUCT_CODE", "SCHEME")),
    Column("branch_code", ("BRANCH", "BRANCH_CODE", "SOL_ID")),
    Column("principal", ("PRINCIPAL", "PRINCIPAL_OUTSTANDING", "OUTSTANDING_PRINCIPAL", "BALANCE"), "amount", True),
    Column("interest_receivable", ("INTEREST", "ACCRUED_INTEREST", "INTEREST_RECEIVABLE"), "amount"),
    Column("days_past_due", ("DPD", "DAYS_PAST_DUE", "DAYS_OVERDUE", "ARREARS_DAYS"), "int", True),
]


def bands(institution):
    existing = list(ClassificationBand.objects.filter(institution=institution))
    if existing:
        return existing
    spec = DEFAULT_BANDS.get(institution.institution_type, DEFAULT_BANDS["DEFAULT"])
    return [
        ClassificationBand.objects.create(
            institution=institution, name=n, min_days=lo, max_days=hi, provision_rate=Decimal(rate), is_performing=perf
        )
        for n, lo, hi, rate, perf in spec
    ]


def classify(days: int, band_list) -> ClassificationBand:
    for band in band_list:
        if days >= band.min_days and (band.max_days is None or days <= band.max_days):
            return band
    raise BusinessRuleError(f"No classification band covers {days} days past due. Review the band configuration.")


def load_snapshot(
    institution, user, *, filename, data, as_of, principal_account, interest_account=None, provision_account=None
) -> LoanSnapshot:
    require_perm(user, institution, "portfolio.manage")
    for account in (principal_account, interest_account, provision_account):
        if account is not None and account.institution_id != institution.pk:
            raise BusinessRuleError("Control account belongs to a different institution.")
    extension = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    records = parse_records(data, extension, COLUMNS, settings.LEDGERGUARD["MAX_UPLOAD_BYTES"])
    band_list = bands(institution)
    branches = {b.code: b for b in Branch.objects.filter(institution=institution)}
    places = institution.amount_decimal_places
    with transaction.atomic():
        current = (
            LoanSnapshot.objects.select_for_update()
            .filter(institution=institution, as_of=as_of)
            .exclude(status=LoanSnapshot.Status.SUPERSEDED)
            .first()
        )
        if current and current.status == LoanSnapshot.Status.REVIEWED:
            raise BusinessRuleError(f"The portfolio as at {as_of} has been signed off and cannot be replaced.")
        if current:
            current.status = LoanSnapshot.Status.SUPERSEDED
            current.save(update_fields=["status", "updated_at"])
        snapshot = LoanSnapshot.objects.create(
            institution=institution,
            as_of=as_of,
            source_file=filename[:255],
            sha256=hashlib.sha256(data).hexdigest(),
            principal_account=principal_account,
            interest_account=interest_account,
            provision_account=provision_account,
            uploaded_by=user,
        )
        objs = []
        for r in records:
            if r["principal"] < 0 or (r["interest_receivable"] or ZERO) < 0 or r["days_past_due"] < 0:
                raise BusinessRuleError(f"Row {r['row_number']}: negative principal, interest or days past due.")
            band = classify(r["days_past_due"], band_list)
            exposure = r["principal"] + (r["interest_receivable"] or ZERO)
            objs.append(
                LoanRecord(
                    snapshot=snapshot,
                    row_number=r["row_number"],
                    loan_account=r["loan_account"],
                    customer_name=r["customer_name"][:200],
                    product=r["product"][:80],
                    branch_code=r["branch_code"][:20],
                    branch=branches.get(r["branch_code"]),
                    principal=r["principal"],
                    interest_receivable=r["interest_receivable"] or ZERO,
                    days_past_due=r["days_past_due"],
                    classification=band.name,
                    provision_rate=band.provision_rate,
                    provision_required=quantize(exposure * band.provision_rate / Decimal(100), places),
                )
            )
        LoanRecord.objects.bulk_create(objs, batch_size=5000)
    log_activity(
        "loans.snapshot_loaded", user=user, obj=snapshot, details={"rows": len(objs), "sha256": snapshot.sha256}
    )
    return snapshot


@dataclass
class Bucket:
    count: int = 0
    principal: Decimal = ZERO
    interest: Decimal = ZERO
    provision: Decimal = ZERO

    def add(self, rec):
        self.count += 1
        self.principal += rec.principal
        self.interest += rec.interest_receivable
        self.provision += rec.provision_required


@dataclass
class LoanControl:
    snapshot: LoanSnapshot
    total: Bucket
    by_classification: "OrderedDict[str, Bucket]" = field(default_factory=OrderedDict)
    by_product: dict = field(default_factory=dict)
    by_branch: dict = field(default_factory=dict)
    gl_principal: Decimal | None = None
    gl_interest: Decimal | None = None
    gl_provision: Decimal | None = None

    @property
    def principal_difference(self):
        return None if self.gl_principal is None else self.total.principal - self.gl_principal

    @property
    def interest_difference(self):
        return None if self.gl_interest is None else self.total.interest - self.gl_interest

    @property
    def provision_shortfall(self):
        return None if self.gl_provision is None else self.total.provision - self.gl_provision

    @property
    def npl_ratio(self):
        if not self.total.principal:
            return Decimal("0")
        npl = sum((b.principal for name, b in self.by_classification.items() if name not in self._performing), ZERO)
        return quantize(npl * 100 / self.total.principal, 2)

    _performing: tuple = ()


def control(snapshot: LoanSnapshot, branches=None) -> LoanControl:
    records = snapshot.records.all()
    if branches is not None:
        records = records.filter(branch_id__in=branches)
    band_list = bands(snapshot.institution)
    result = LoanControl(snapshot, Bucket())
    result._performing = tuple(b.name for b in band_list if b.is_performing)
    for band in band_list:
        result.by_classification[band.name] = Bucket()
    for rec in records:
        result.total.add(rec)
        result.by_classification.setdefault(rec.classification, Bucket()).add(rec)
        result.by_product.setdefault(rec.product or "(none)", Bucket()).add(rec)
        result.by_branch.setdefault(rec.branch_code or "(none)", Bucket()).add(rec)
    if branches is None:
        result.gl_principal = reports.account_balance(snapshot.principal_account, snapshot.as_of)
        if snapshot.interest_account:
            result.gl_interest = reports.account_balance(snapshot.interest_account, snapshot.as_of)
        if snapshot.provision_account:
            balance = reports.account_balance(snapshot.provision_account, snapshot.as_of)
            result.gl_provision = -balance if snapshot.provision_account.is_debit_normal else balance
    return result


def review(snapshot: LoanSnapshot, user, comment="") -> LoanSnapshot:
    require_perm(user, snapshot.institution, "portfolio.manage")
    if snapshot.status != LoanSnapshot.Status.LOADED:
        raise BusinessRuleError("Only loaded snapshots can be signed off.")
    assert_independent(snapshot.institution, snapshot.uploaded_by, user, "this portfolio schedule")
    snapshot.status = LoanSnapshot.Status.REVIEWED
    snapshot.reviewed_by = user
    snapshot.reviewed_at = timezone.now()
    snapshot.review_comment = comment
    snapshot.save()
    log_activity("loans.snapshot_reviewed", user=user, obj=snapshot, details={"comment": comment})
    return snapshot


def prepare_provision_journal(snapshot: LoanSnapshot, user, charge_account):
    """Prepare (not post) the journal that brings the GL allowance to the required provision."""
    institution = snapshot.institution
    if snapshot.provision_account is None:
        raise BusinessRuleError("Set the provision (impairment allowance) account on the snapshot first.")
    if charge_account.institution_id != institution.pk:
        raise BusinessRuleError("Charge account belongs to a different institution.")
    if snapshot.provision_journal_id:
        raise BusinessRuleError("A provision journal has already been prepared for this snapshot.")
    shortfall = control(snapshot).provision_shortfall
    if not shortfall:
        raise BusinessRuleError("The GL allowance already equals the required provision.")
    if shortfall > 0:
        lines = [
            LineInput(account=charge_account, debit=shortfall, description="Impairment charge"),
            LineInput(account=snapshot.provision_account, credit=shortfall, description="Increase in allowance"),
        ]
    else:
        lines = [
            LineInput(account=snapshot.provision_account, debit=-shortfall, description="Release of allowance"),
            LineInput(account=charge_account, credit=-shortfall, description="Impairment write-back"),
        ]
    with transaction.atomic():
        journal = create_journal(
            institution,
            user,
            transaction_date=snapshot.as_of,
            accounting_date=snapshot.as_of,
            description=f"Loan impairment provision adjustment as at {snapshot.as_of}",
            journal_type="ADJUSTMENT",
            lines=lines,
            source_reference=f"LOANS-{snapshot.pk}",
        )
        journal = submit_journal(journal, user, "Provision per loan portfolio schedule")
        snapshot.provision_journal = journal
        snapshot.save(update_fields=["provision_journal", "updated_at"])
    log_activity("loans.provision_journal_prepared", user=user, obj=snapshot, details={"amount": shortfall})
    return journal
