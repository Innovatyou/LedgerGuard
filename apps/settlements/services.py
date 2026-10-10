"""
Settlement reconciliation support for payment channels (NIP, NIBSS, POS, ATM,
card, interbank clearing, processors).

Settlement reports are imported with the import framework (source type
SETTLEMENT_REPORT) with transaction amount, fees, taxes, gross and net
settlement and settlement dates kept in separate fields. This module validates
those figures, summarises them by settlement batch, and sets up the matching
rules that reconcile net settlement against bank settlement-account credits.
"""

from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Exists, OuterRef

from apps.accounts.services import require_perm
from apps.core.activity import log_activity
from apps.imports.models import SourceTransaction, SourceType
from apps.reconciliation.engine import settlement_net
from apps.reconciliation.models import MatchGroupItem, MatchingRule, ReconType, Strategy
from apps.reconciliation.services import to_engine

ZERO = Decimal("0.00")


@dataclass
class BatchSummary:
    batch_ref: str
    channel: str
    settlement_date: object
    records: int = 0
    reversals: int = 0
    gross: Decimal = ZERO
    fees: Decimal = ZERO
    taxes: Decimal = ZERO
    net_reported: Decimal = ZERO
    net_computed: Decimal = ZERO
    reversal_amount: Decimal = ZERO
    matched_records: int = 0
    exceptions: list[str] = field(default_factory=list)

    @property
    def net_difference(self) -> Decimal:
        return self.net_reported - self.net_computed

    @property
    def fully_matched(self) -> bool:
        return self.records > 0 and self.matched_records == self.records


def validate_record(txn: SourceTransaction) -> list[str]:
    issues = []
    if txn.gross_amount is None and txn.net_amount is None:
        issues.append("Neither gross nor net settlement amount supplied.")
    if txn.gross_amount is not None and txn.net_amount is not None:
        computed = txn.gross_amount - (txn.fee_amount or ZERO) - (txn.tax_amount or ZERO)
        if computed != txn.net_amount:
            issues.append(f"Gross − fee − tax = {computed} but net = {txn.net_amount}.")
    if not txn.settlement_batch_ref:
        issues.append("Missing settlement batch reference.")
    if txn.settlement_date and txn.settlement_date < txn.transaction_date:
        issues.append("Settlement date precedes transaction date.")
    return issues


def settlement_summary(institution, *, date_from=None, date_to=None, channel=None, account_reference=None):
    qs = SourceTransaction.objects.filter(institution=institution, source_type=SourceType.SETTLEMENT_REPORT)
    if date_from:
        qs = qs.filter(transaction_date__gte=date_from)
    if date_to:
        qs = qs.filter(transaction_date__lte=date_to)
    if channel:
        qs = qs.filter(channel=channel)
    if account_reference:
        qs = qs.filter(account_reference=account_reference)
    qs = qs.annotate(_matched=Exists(MatchGroupItem.objects.filter(transaction=OuterRef("pk"), active=True)))
    batches: OrderedDict[tuple, BatchSummary] = OrderedDict()
    for txn in qs.order_by("settlement_date", "settlement_batch_ref", "pk"):
        key = (txn.settlement_batch_ref or "(none)", txn.channel)
        summary = batches.get(key)
        if summary is None:
            summary = batches[key] = BatchSummary(key[0], txn.channel, txn.settlement_date or txn.transaction_date)
        summary.records += 1
        sign = Decimal(-1) if txn.is_reversal or txn.signed_amount < 0 else Decimal(1)
        if sign < 0:
            summary.reversals += 1
            summary.reversal_amount += txn.gross_amount or txn.net_amount or abs(txn.signed_amount)
        summary.gross += sign * (txn.gross_amount or ZERO)
        summary.fees += sign * (txn.fee_amount or ZERO)
        summary.taxes += sign * (txn.tax_amount or ZERO)
        summary.net_reported += sign * (txn.net_amount if txn.net_amount is not None else ZERO)
        net, _ = settlement_net(to_engine_like(txn))
        summary.net_computed += sign * abs(net)
        if txn._matched:
            summary.matched_records += 1
        for issue in validate_record(txn):
            summary.exceptions.append(f"Row {txn.source_row_number}: {issue}")
    return list(batches.values())


def to_engine_like(txn: SourceTransaction):
    """Engine record using the transaction's own credit-positive sign."""
    from apps.reconciliation.models import Reconciliation

    dummy = Reconciliation(left_inflow_side="CREDIT", right_inflow_side="CREDIT")
    return to_engine(dummy, "RIGHT", txn)


DEFAULT_SETTLEMENT_RULES = [
    dict(
        name="Settlement: batch net vs bank credit (reference)",
        strategy=Strategy.FEE_NET,
        priority=10,
        left_reference_field="external_reference",
        right_reference_field="settlement_batch_ref",
        date_tolerance_days=3,
        auto_accept=True,
    ),
    dict(
        name="Settlement: batch reference in bank narration",
        strategy=Strategy.FEE_NET,
        priority=20,
        left_reference_field="narration_contains",
        right_reference_field="settlement_batch_ref",
        date_tolerance_days=3,
        auto_accept=False,
    ),
    dict(
        name="Settlement: one-to-one transaction reference",
        strategy=Strategy.EXACT_REFERENCE,
        priority=30,
        left_reference_field="transaction_id",
        right_reference_field="transaction_id",
        date_tolerance_days=2,
        auto_accept=True,
    ),
    dict(
        name="Settlement: reversal pairs",
        strategy=Strategy.REVERSAL_PAIR,
        priority=40,
        left_reference_field="transaction_id",
        right_reference_field="transaction_id",
        date_tolerance_days=5,
        auto_accept=False,
    ),
]


def install_default_rules(institution, user) -> int:
    require_perm(user, institution, "recon.rule_manage")
    created = 0
    for spec in DEFAULT_SETTLEMENT_RULES:
        _, was_created = MatchingRule.objects.get_or_create(
            institution=institution,
            name=spec["name"],
            defaults={**spec, "recon_type": ReconType.SETTLEMENT, "updated_by": user},
        )
        created += was_created
    log_activity("settlement.default_rules_installed", user=user, institution=institution, details={"created": created})
    return created
