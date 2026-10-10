"""
Deposit control totals: compare a core-banking deposit listing with the GL
deposit control accounts by product, and surface exceptions (unmapped
products, overdrawn deposit accounts, dormant balances).
"""

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounting import reports
from apps.accounts.services import require_perm
from apps.approvals.services import assert_independent
from apps.core.activity import log_activity
from apps.core.exceptions import BusinessRuleError
from apps.core.tabular import Column, parse_records
from apps.organizations.models import Branch

from .models import ZERO, DepositProduct, DepositRecord, DepositSnapshot

COLUMNS = [
    Column("account_number", ("ACCOUNT_NO", "ACCOUNT_NUMBER", "ACCT_NO", "NUBAN"), required=True),
    Column("customer_name", ("CUSTOMER", "CUSTOMER_NAME", "ACCOUNT_NAME", "NAME")),
    Column("product", ("PRODUCT", "PRODUCT_CODE", "SCHEME", "ACCOUNT_TYPE"), required=True),
    Column("branch_code", ("BRANCH", "BRANCH_CODE", "SOL_ID")),
    Column("balance", ("BALANCE", "CLEARED_BALANCE", "LEDGER_BALANCE", "BOOK_BALANCE"), "amount", True),
    Column("accrued_interest", ("ACCRUED_INTEREST", "INTEREST_PAYABLE", "INTEREST"), "amount"),
    Column("is_dormant", ("DORMANT", "IS_DORMANT", "DORMANCY", "STATUS"), "bool"),
]


def load_snapshot(institution, user, *, filename, data, as_of) -> DepositSnapshot:
    require_perm(user, institution, "portfolio.manage")
    extension = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    records = parse_records(data, extension, COLUMNS, settings.LEDGERGUARD["MAX_UPLOAD_BYTES"])
    branches = {b.code: b for b in Branch.objects.filter(institution=institution)}
    with transaction.atomic():
        current = (
            DepositSnapshot.objects.select_for_update()
            .filter(institution=institution, as_of=as_of)
            .exclude(status=DepositSnapshot.Status.SUPERSEDED)
            .first()
        )
        if current and current.status == DepositSnapshot.Status.REVIEWED:
            raise BusinessRuleError(f"The deposit listing as at {as_of} has been signed off and cannot be replaced.")
        if current:
            current.status = DepositSnapshot.Status.SUPERSEDED
            current.save(update_fields=["status", "updated_at"])
        snapshot = DepositSnapshot.objects.create(
            institution=institution,
            as_of=as_of,
            source_file=filename[:255],
            sha256=hashlib.sha256(data).hexdigest(),
            uploaded_by=user,
        )
        DepositRecord.objects.bulk_create(
            [
                DepositRecord(
                    snapshot=snapshot,
                    row_number=r["row_number"],
                    account_number=r["account_number"],
                    customer_name=r["customer_name"][:200],
                    product=r["product"].upper()[:40],
                    branch_code=r["branch_code"][:20],
                    branch=branches.get(r["branch_code"]),
                    balance=r["balance"],
                    accrued_interest=r["accrued_interest"] or ZERO,
                    is_dormant=bool(r["is_dormant"]),
                )
                for r in records
            ],
            batch_size=5000,
        )
    log_activity(
        "deposits.snapshot_loaded", user=user, obj=snapshot, details={"rows": len(records), "sha256": snapshot.sha256}
    )
    return snapshot


@dataclass
class ProductLine:
    code: str
    product: DepositProduct | None
    count: int = 0
    balance: Decimal = ZERO
    accrued_interest: Decimal = ZERO
    gl_balance: Decimal | None = None
    gl_interest: Decimal | None = None

    @property
    def difference(self):
        return None if self.gl_balance is None else self.balance - self.gl_balance

    @property
    def interest_difference(self):
        return None if self.gl_interest is None else self.accrued_interest - self.gl_interest


@dataclass
class DepositControl:
    snapshot: DepositSnapshot
    products: list = field(default_factory=list)
    by_branch: dict = field(default_factory=dict)
    overdrawn: list = field(default_factory=list)
    dormant_count: int = 0
    dormant_balance: Decimal = ZERO
    total_balance: Decimal = ZERO

    @property
    def unmapped(self):
        return [p for p in self.products if p.product is None]

    @property
    def overdrawn_total(self):
        return sum((r.balance for r in self.overdrawn), ZERO)

    @property
    def reconciled(self):
        return not self.unmapped and all(p.difference == 0 for p in self.products if p.difference is not None)


def control(snapshot: DepositSnapshot, branches=None) -> DepositControl:
    records = snapshot.records.all()
    if branches is not None:
        records = records.filter(branch_id__in=branches)
    mapping = {
        p.code.upper(): p
        for p in DepositProduct.objects.filter(institution=snapshot.institution).select_related(
            "gl_account", "interest_payable_account"
        )
    }
    lines: dict[str, ProductLine] = {}
    result = DepositControl(snapshot)
    for rec in records:
        line = lines.setdefault(rec.product, ProductLine(rec.product, mapping.get(rec.product)))
        line.count += 1
        line.balance += rec.balance
        line.accrued_interest += rec.accrued_interest
        result.total_balance += rec.balance
        result.by_branch[rec.branch_code or "(none)"] = (
            result.by_branch.get(rec.branch_code or "(none)", ZERO) + rec.balance
        )
        if rec.balance < 0:
            result.overdrawn.append(rec)
        if rec.is_dormant:
            result.dormant_count += 1
            result.dormant_balance += rec.balance
    if branches is None:
        # Several products may share one GL account: compare per account, attributing the GL balance once.
        by_account: dict = {}
        for line in lines.values():
            if line.product:
                by_account.setdefault(line.product.gl_account_id, []).append(line)
        for group in by_account.values():
            account = group[0].product.gl_account
            gl = reports.account_balance(account, snapshot.as_of)
            if len(group) == 1:
                group[0].gl_balance = gl
            else:
                for line in group:
                    line.gl_balance = None
                combined = ProductLine(
                    " + ".join(ln.code for ln in group),
                    group[0].product,
                    sum(ln.count for ln in group),
                    sum((ln.balance for ln in group), ZERO),
                )
                combined.gl_balance = gl
                lines[combined.code] = combined
        for line in lines.values():
            if line.product and line.product.interest_payable_account and " + " not in line.code:
                line.gl_interest = reports.account_balance(line.product.interest_payable_account, snapshot.as_of)
    result.products = sorted(lines.values(), key=lambda ln: ln.code)
    return result


def review(snapshot: DepositSnapshot, user, comment="") -> DepositSnapshot:
    require_perm(user, snapshot.institution, "portfolio.manage")
    if snapshot.status != DepositSnapshot.Status.LOADED:
        raise BusinessRuleError("Only loaded listings can be signed off.")
    assert_independent(snapshot.institution, snapshot.uploaded_by, user, "this deposit schedule")
    snapshot.status = DepositSnapshot.Status.REVIEWED
    snapshot.reviewed_by = user
    snapshot.reviewed_at = timezone.now()
    snapshot.review_comment = comment
    snapshot.save()
    log_activity("deposits.snapshot_reviewed", user=user, obj=snapshot, details={"comment": comment})
    return snapshot
