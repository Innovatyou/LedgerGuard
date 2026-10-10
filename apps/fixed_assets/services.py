"""
Fixed-asset register, depreciation runs and disposals.

Depreciation convention: a full month's charge is taken in the month the asset
is placed in service and none in the month of disposal. Charges never take the
net book value below the residual value. Depreciation and disposal entries are
prepared as journals that follow the normal approval and posting workflow; the
register only counts depreciation whose journal has been posted.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from apps.accounting import reports
from apps.accounting.models import FinancialPeriod, Journal
from apps.accounting.services import LineInput, create_journal, submit_journal
from apps.accounts.services import require_perm
from apps.core.activity import log_activity
from apps.core.exceptions import BusinessRuleError
from apps.core.money import quantize

from .models import ZERO, AssetCategory, DepreciationLine, DepreciationMethod, DepreciationRun, FixedAsset


def months_in(period: FinancialPeriod) -> int:
    return (period.end_date.year - period.start_date.year) * 12 + period.end_date.month - period.start_date.month + 1


def accumulated_depreciation(asset: FixedAsset, as_of: date | None = None) -> Decimal:
    """Opening balance plus depreciation from runs whose journals have been posted."""
    lines = DepreciationLine.objects.filter(asset=asset, run__status=DepreciationRun.Status.POSTED)
    if as_of:
        lines = lines.filter(run__period__end_date__lte=as_of)
    return asset.opening_accumulated_depreciation + (lines.aggregate(t=Sum("amount"))["t"] or ZERO)


def net_book_value(asset: FixedAsset, as_of: date | None = None) -> Decimal:
    return asset.cost - accumulated_depreciation(asset, as_of)


def charge_for(asset: FixedAsset, accumulated: Decimal, months: int, places: int = 2) -> Decimal:
    depreciable_left = asset.cost - asset.residual_value - accumulated
    if depreciable_left <= 0:
        return ZERO
    if asset.method == DepreciationMethod.STRAIGHT_LINE:
        monthly = (asset.cost - asset.residual_value) / Decimal(asset.useful_life_months)
    else:
        monthly = (asset.cost - accumulated) * asset.annual_rate / Decimal(100) / Decimal(12)
    return min(quantize(monthly * months, places), depreciable_left)


def sync_statuses(institution):
    """Align run and disposal statuses with the outcome of their journals."""
    for run in DepreciationRun.objects.filter(institution=institution, status=DepreciationRun.Status.PREPARED):
        if run.journal and run.journal.status in (Journal.Status.POSTED, Journal.Status.REVERSED):
            run.status = DepreciationRun.Status.POSTED
            run.save(update_fields=["status", "updated_at"])
    for asset in FixedAsset.objects.filter(institution=institution, status=FixedAsset.Status.DISPOSAL_PENDING):
        journal = asset.disposal_journal
        if journal and journal.status == Journal.Status.POSTED:
            asset.status = FixedAsset.Status.DISPOSED
        elif journal is None or journal.status == Journal.Status.REJECTED:
            asset.status = FixedAsset.Status.ACTIVE
            asset.disposal_journal = None
            asset.disposal_date = None
            asset.disposal_proceeds = None
        else:
            continue
        asset.save()


def create_asset(institution, user, *, credit_account=None, **fields) -> FixedAsset:
    """Add an asset to the register; optionally prepare the capitalisation journal (DR cost / CR ``credit_account``)."""
    require_perm(user, institution, "asset.manage")
    category = fields["category"]
    if category.institution_id != institution.pk:
        raise BusinessRuleError("Asset category belongs to a different institution.")
    fields.setdefault("method", category.method)
    fields.setdefault("useful_life_months", category.useful_life_months)
    fields.setdefault("annual_rate", category.annual_rate)
    if fields["residual_value"] >= fields["cost"]:
        raise BusinessRuleError("Residual value must be less than cost.")
    if fields["in_service_date"] < fields["acquisition_date"]:
        raise BusinessRuleError("In-service date cannot precede acquisition date.")
    if fields["method"] == DepreciationMethod.REDUCING_BALANCE and not fields["annual_rate"]:
        raise BusinessRuleError("Reducing-balance assets need an annual depreciation rate.")
    if fields.get("opening_accumulated_depreciation", ZERO) > fields["cost"] - fields["residual_value"]:
        raise BusinessRuleError("Opening accumulated depreciation exceeds the depreciable amount.")
    with transaction.atomic():
        asset = FixedAsset.objects.create(institution=institution, created_by=user, **fields)
        if credit_account is not None:
            if credit_account.institution_id != institution.pk:
                raise BusinessRuleError("Credit account belongs to a different institution.")
            journal = create_journal(
                institution,
                user,
                transaction_date=asset.acquisition_date,
                description=f"Capitalisation of asset {asset.tag} {asset.name}"[:500],
                source_reference=asset.tag,
                journal_type="MANUAL",
                branch=asset.branch,
                lines=[
                    LineInput(account=category.cost_account, debit=asset.cost, branch=asset.branch),
                    LineInput(account=credit_account, credit=asset.cost, branch=asset.branch),
                ],
            )
            asset.capitalisation_journal = submit_journal(journal, user, "Asset capitalisation")
            asset.save(update_fields=["capitalisation_journal", "updated_at"])
    log_activity("asset.created", user=user, obj=asset, details={"cost": asset.cost})
    return asset


@dataclass
class RunPreview:
    period: FinancialPeriod
    lines: list = field(default_factory=list)  # (asset, nbv_before, amount)

    @property
    def total(self):
        return sum((amount for _, _, amount in self.lines), ZERO)


def preview_run(institution, period: FinancialPeriod) -> RunPreview:
    months = months_in(period)
    preview = RunPreview(period)
    assets = FixedAsset.objects.filter(
        institution=institution, status=FixedAsset.Status.ACTIVE, in_service_date__lte=period.end_date
    ).select_related("category", "branch")
    for asset in assets:
        accumulated = accumulated_depreciation(asset)
        amount = charge_for(asset, accumulated, months, institution.amount_decimal_places)
        if amount > 0:
            preview.lines.append((asset, asset.cost - accumulated, amount))
    return preview


def run_depreciation(institution, user, period: FinancialPeriod) -> DepreciationRun:
    require_perm(user, institution, "asset.depreciate")
    if period.institution_id != institution.pk:
        raise BusinessRuleError("Period belongs to a different institution.")
    sync_statuses(institution)
    if period.status != FinancialPeriod.Status.OPEN:
        raise BusinessRuleError(f"Period {period} is closed.")
    if DepreciationRun.objects.filter(institution=institution, status=DepreciationRun.Status.PREPARED).exists():
        raise BusinessRuleError("A previous depreciation run is awaiting approval/posting. Post or cancel it first.")
    if DepreciationRun.objects.filter(
        institution=institution, period=period, status=DepreciationRun.Status.POSTED
    ).exists():
        raise BusinessRuleError(f"Depreciation for {period} has already been posted.")
    later = DepreciationRun.objects.filter(
        institution=institution, status=DepreciationRun.Status.POSTED, period__start_date__gt=period.start_date
    )
    if later.exists():
        raise BusinessRuleError("Depreciation has been posted for a later period.")
    preview = preview_run(institution, period)
    if not preview.lines:
        raise BusinessRuleError("No depreciation is due for this period.")
    grouped: dict = {}
    for asset, _, amount in preview.lines:
        key = (asset.category_id, asset.branch_id)
        grouped.setdefault(key, [asset.category, asset.branch, ZERO])
        grouped[key][2] += amount
    journal_lines = []
    for category, branch, amount in grouped.values():
        journal_lines.append(
            LineInput(
                account=category.depreciation_expense_account,
                debit=amount,
                branch=branch,
                description=f"Depreciation – {category.name}",
            )
        )
        journal_lines.append(
            LineInput(
                account=category.accumulated_depreciation_account,
                credit=amount,
                branch=branch,
                description=f"Accumulated depreciation – {category.name}",
            )
        )
    with transaction.atomic():
        journal = create_journal(
            institution,
            user,
            transaction_date=period.end_date,
            accounting_date=period.end_date,
            description=f"Depreciation for {period}",
            journal_type="DEPRECIATION",
            lines=journal_lines,
            source_reference=f"DEP-{period.start_date:%Y%m}",
        )
        journal = submit_journal(journal, user, "Monthly depreciation run")
        run = DepreciationRun.objects.create(
            institution=institution, period=period, journal=journal, total=preview.total, prepared_by=user
        )
        DepreciationLine.objects.bulk_create(
            DepreciationLine(run=run, asset=asset, amount=amount, nbv_before=nbv)
            for asset, nbv, amount in preview.lines
        )
    log_activity(
        "asset.depreciation_prepared", user=user, obj=run, details={"total": run.total, "assets": len(preview.lines)}
    )
    return run


def cancel_run(run: DepreciationRun, user, reason: str) -> DepreciationRun:
    require_perm(user, run.institution, "asset.depreciate")
    if not reason.strip():
        raise BusinessRuleError("A reason is required.")
    if run.status != DepreciationRun.Status.PREPARED:
        raise BusinessRuleError("Only unposted runs can be cancelled.")
    if run.journal and run.journal.status not in (Journal.Status.DRAFT, Journal.Status.REJECTED):
        raise BusinessRuleError("Reject the depreciation journal before cancelling the run.")
    run.status = DepreciationRun.Status.CANCELLED
    run.save(update_fields=["status", "updated_at"])
    log_activity("asset.depreciation_cancelled", user=user, obj=run, details={"reason": reason})
    return run


def prepare_disposal(asset: FixedAsset, user, *, disposal_date: date, proceeds: Decimal, proceeds_account=None):
    institution = asset.institution
    require_perm(user, institution, "asset.dispose")
    sync_statuses(institution)
    asset.refresh_from_db()
    if asset.status != FixedAsset.Status.ACTIVE:
        raise BusinessRuleError("Only assets in use can be disposed.")
    if proceeds < 0:
        raise BusinessRuleError("Proceeds cannot be negative.")
    if proceeds > 0 and proceeds_account is None:
        raise BusinessRuleError("Select the account that received the proceeds.")
    if DepreciationRun.objects.filter(institution=institution, status=DepreciationRun.Status.PREPARED).exists():
        raise BusinessRuleError("Post or cancel the pending depreciation run before disposing assets.")
    category = asset.category
    accumulated = accumulated_depreciation(asset)
    gain = proceeds - (asset.cost - accumulated)
    lines = [LineInput(account=category.cost_account, credit=asset.cost, branch=asset.branch, description="Cost")]
    if accumulated > 0:
        lines.append(
            LineInput(
                account=category.accumulated_depreciation_account,
                debit=accumulated,
                branch=asset.branch,
                description="Accumulated depreciation",
            )
        )
    if proceeds > 0:
        lines.append(LineInput(account=proceeds_account, debit=proceeds, branch=asset.branch, description="Proceeds"))
    if gain > 0:
        lines.append(
            LineInput(
                account=category.disposal_account, credit=gain, branch=asset.branch, description="Gain on disposal"
            )
        )
    elif gain < 0:
        lines.append(
            LineInput(
                account=category.disposal_account, debit=-gain, branch=asset.branch, description="Loss on disposal"
            )
        )
    with transaction.atomic():
        journal = create_journal(
            institution,
            user,
            transaction_date=disposal_date,
            description=f"Disposal of asset {asset.tag} {asset.name}"[:500],
            source_reference=asset.tag,
            branch=asset.branch,
            lines=lines,
        )
        journal = submit_journal(journal, user, "Asset disposal")
        asset.status = FixedAsset.Status.DISPOSAL_PENDING
        asset.disposal_date = disposal_date
        asset.disposal_proceeds = proceeds
        asset.disposal_journal = journal
        asset.save()
    log_activity("asset.disposal_prepared", user=user, obj=asset, details={"proceeds": proceeds, "gain": gain})
    return journal


@dataclass
class RegisterControl:
    """Register totals versus the GL for one pair of cost / accumulated-depreciation accounts."""

    label: str
    cost_account: object
    accumulated_account: object
    register_cost: Decimal = ZERO
    register_accumulated: Decimal = ZERO
    gl_cost: Decimal = ZERO
    gl_accumulated: Decimal = ZERO

    @property
    def cost_difference(self):
        return self.register_cost - self.gl_cost

    @property
    def accumulated_difference(self):
        return self.register_accumulated - self.gl_accumulated

    @property
    def reconciled(self):
        return self.cost_difference == 0 and self.accumulated_difference == 0


def register_control(institution, as_of: date) -> list[RegisterControl]:
    """
    Compare register totals with the GL. Categories sharing the same GL accounts
    are combined so that each account balance is compared exactly once.
    """
    sync_statuses(institution)
    groups: dict = {}
    for category in AssetCategory.objects.filter(institution=institution).select_related(
        "cost_account", "accumulated_depreciation_account"
    ):
        key = (category.cost_account_id, category.accumulated_depreciation_account_id)
        row = groups.get(key)
        if row is None:
            row = groups[key] = RegisterControl(
                category.name, category.cost_account, category.accumulated_depreciation_account
            )
        else:
            row.label = f"{row.label}, {category.name}"
        assets = FixedAsset.objects.filter(category=category, acquisition_date__lte=as_of).exclude(
            status=FixedAsset.Status.DISPOSED, disposal_date__lte=as_of
        )
        for asset in assets:
            row.register_cost += asset.cost
            row.register_accumulated += accumulated_depreciation(asset, as_of)
    for row in groups.values():
        row.gl_cost = reports.account_balance(row.cost_account, as_of)
        gl_acc = reports.account_balance(row.accumulated_account, as_of)
        # Accumulated depreciation is a credit balance; express it as a positive amount.
        row.gl_accumulated = -gl_acc if row.accumulated_account.is_debit_normal else gl_acc
    return list(groups.values())
