"""
Matching strategies.

The functions in this module are pure: they receive lightweight transaction
records and a rule and return match proposals. They never decide acceptance;
`services.run_reconciliation` classifies each proposal (exact / high
confidence / manual review) and persists it with full traceability.

Amounts are "flow" amounts: positive means money flowing *into* the account
from that source's point of view, so a bank-statement credit and a GL debit on
the bank asset account are both positive and compare directly.
"""

import difflib
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .models import REFERENCE_FIELDS, Strategy

ZERO = Decimal("0.00")


@dataclass(frozen=True)
class Txn:
    id: int
    side: str  # LEFT / RIGHT
    flow: Decimal
    date: date
    value_date: date | None = None
    norm_transaction_id: str = ""
    norm_external_reference: str = ""
    norm_narration: str = ""
    counterparty_reference: str = ""
    settlement_batch_ref: str = ""
    channel: str = ""
    currency: str = ""
    gross_amount: Decimal | None = None
    fee_amount: Decimal | None = None
    tax_amount: Decimal | None = None
    net_amount: Decimal | None = None
    is_reversal: bool = False
    suspected_duplicate: bool = False

    def ref(self, field_name: str) -> str:
        if field_name == "narration_contains":
            return ""
        attr = REFERENCE_FIELDS.get(field_name, field_name)
        return getattr(self, attr, "") or ""

    @property
    def transaction_date(self):
        return self.date


@dataclass
class Proposal:
    left: list[Txn]
    right: list[Txn]
    unique: bool
    reason: str
    kind: str = "MATCH"
    flags: list[str] = field(default_factory=list)

    @property
    def left_total(self) -> Decimal:
        return sum((t.flow for t in self.left), ZERO)

    @property
    def right_total(self) -> Decimal:
        return sum((t.flow for t in self.right), ZERO)

    @property
    def difference(self) -> Decimal:
        if self.kind == "REVERSAL":
            return self.left_total + self.right_total
        return self.left_total - self.right_total

    @property
    def ids(self) -> list[int]:
        return sorted(t.id for t in self.left + self.right)

    @property
    def signature(self) -> str:
        return ",".join(str(i) for i in self.ids)


def _within_days(a: Txn, b: Txn, days: int) -> bool:
    return abs((a.date - b.date).days) <= days


def _amount_ok(a: Decimal, b: Decimal, tolerance: Decimal) -> bool:
    return abs(a - b) <= tolerance


def _best(left: Txn, candidates: list[Txn]) -> Txn:
    return min(candidates, key=lambda r: (abs((left.date - r.date).days), abs(left.flow - r.flow), r.id))


def _one_to_one(rule, left, right, left_key, right_key, reason, amount_in_key=False):
    """
    Generic keyed one-to-one matcher with date and amount tolerance.

    A proposal is *unique* only when the left item has exactly one eligible
    candidate and that candidate is not eligible for any other left item.
    """
    tol_days = rule.date_tolerance_days
    tol_amount = rule.amount_tolerance
    index: dict = defaultdict(list)
    for r in right:
        key = right_key(r)
        if key:
            index[key].append(r)
    eligible: dict[int, list[Txn]] = {}
    claims: dict[int, int] = defaultdict(int)
    ordered_left = sorted(left, key=lambda t: (t.date, t.id))
    for lt in ordered_left:
        key = left_key(lt)
        if not key or key not in index:
            continue
        candidates = [
            r
            for r in index[key]
            if _within_days(lt, r, tol_days) and (amount_in_key or _amount_ok(lt.flow, r.flow, tol_amount))
        ]
        if candidates:
            eligible[lt.id] = candidates
            for r in candidates:
                claims[r.id] += 1
    used: set[int] = set()
    proposals = []
    for lt in ordered_left:
        candidates = [r for r in eligible.get(lt.id, []) if r.id not in used]
        if not candidates:
            continue
        best = _best(lt, candidates)
        used.add(best.id)
        unique = len(eligible[lt.id]) == 1 and claims[best.id] == 1
        proposals.append(Proposal([lt], [best], unique, reason))
    return proposals


def match_reference(rule, left, right):
    lf, rf = rule.left_reference_field, rule.right_reference_field
    if lf == "narration_contains":
        return _narration_contains(rule, left, right, rf)
    return _one_to_one(
        rule,
        left,
        right,
        lambda t: t.ref(lf),
        lambda t: t.ref(rf),
        f"Reference {lf}↔{rf} equal; amount within {rule.amount_tolerance}",
    )


def _narration_contains(rule, left, right, right_field):
    proposals, used = [], set()
    for lt in left:
        candidates = [
            r
            for r in right
            if r.id not in used
            and r.ref(right_field)
            and r.ref(right_field) in lt.norm_narration
            and _within_days(lt, r, rule.date_tolerance_days)
            and _amount_ok(lt.flow, r.flow, rule.amount_tolerance)
        ]
        if candidates:
            best = _best(lt, candidates)
            used.add(best.id)
            proposals.append(Proposal([lt], [best], len(candidates) == 1, f"{right_field} found in narration"))
    return proposals


def match_amount_date(rule, left, right):
    tol = rule.amount_tolerance
    if tol == ZERO:
        return _one_to_one(
            rule,
            left,
            right,
            lambda t: t.flow,
            lambda t: t.flow,
            f"Exact amount within {rule.date_tolerance_days} day(s)",
            amount_in_key=True,
        )
    ordered = sorted(right, key=lambda t: t.flow)
    flows = [t.flow for t in ordered]
    used, proposals = set(), []
    for lt in sorted(left, key=lambda t: (t.date, t.id)):
        lo, hi = bisect_left(flows, lt.flow - tol), bisect_right(flows, lt.flow + tol)
        candidates = [r for r in ordered[lo:hi] if r.id not in used and _within_days(lt, r, rule.date_tolerance_days)]
        if candidates:
            best = _best(lt, candidates)
            used.add(best.id)
            proposals.append(
                Proposal(
                    [lt],
                    [best],
                    len(candidates) == 1,
                    f"Amount within {tol}, date within {rule.date_tolerance_days} day(s)",
                )
            )
    return proposals


def match_narration(rule, left, right):
    if rule.amount_tolerance == ZERO:
        return _one_to_one(
            rule,
            left,
            right,
            lambda t: (t.norm_narration, t.flow) if t.norm_narration else None,
            lambda t: (t.norm_narration, t.flow) if t.norm_narration else None,
            "Normalised narration and amount equal",
            amount_in_key=True,
        )
    return _one_to_one(
        rule,
        left,
        right,
        lambda t: t.norm_narration,
        lambda t: t.norm_narration,
        "Normalised narration equal; amount within tolerance",
    )


def match_composite(rule, left, right):
    fields = list(rule.composite_fields or [])
    if not fields:
        return []

    def key(t):
        values = tuple(getattr(t, f) for f in fields)
        if any(v in (None, "") for v in values):
            return None
        return values + ((t.flow,) if rule.amount_tolerance == ZERO else ())

    return _one_to_one(
        rule,
        left,
        right,
        key,
        key,
        f"Composite key equal: {', '.join(fields)}",
        amount_in_key=rule.amount_tolerance == ZERO,
    )


def _grouped(rule, singles, many, single_field, many_field, reason, swap=False):
    groups: dict = defaultdict(list)
    for t in many:
        key = t.ref(many_field)
        if key:
            groups[key].append(t)
    singles_by_key: dict = defaultdict(list)
    for t in singles:
        key = t.ref(single_field)
        if key:
            singles_by_key[key].append(t)
    proposals, used = [], set()
    for key, members in groups.items():
        if not 2 <= len(members) <= rule.max_group_size or key not in singles_by_key:
            continue
        total = sum((m.flow for m in members), ZERO)
        candidates = [
            s
            for s in singles_by_key[key]
            if s.id not in used
            and _amount_ok(s.flow, total, rule.amount_tolerance)
            and all(_within_days(s, m, rule.date_tolerance_days) for m in members)
        ]
        if not candidates:
            continue
        best = min(candidates, key=lambda s: (abs(s.flow - total), s.id))
        used.add(best.id)
        unique = len(candidates) == 1
        if swap:
            proposals.append(Proposal(members, [best], unique, reason))
        else:
            proposals.append(Proposal([best], members, unique, reason))
    return proposals


def match_one_to_many(rule, left, right):
    return _grouped(
        rule,
        left,
        right,
        rule.left_reference_field,
        rule.right_reference_field,
        "One left item equals the sum of right items with the same reference",
    )


def match_many_to_one(rule, left, right):
    return _grouped(
        rule,
        right,
        left,
        rule.right_reference_field,
        rule.left_reference_field,
        "Sum of left items with the same reference equals one right item",
        swap=True,
    )


def settlement_net(t: Txn) -> tuple[Decimal, list[str]]:
    """Effective net settlement amount of a settlement record and any consistency flags."""
    flags = []
    sign = Decimal(-1) if t.flow < 0 else Decimal(1)
    if t.gross_amount is not None:
        computed = t.gross_amount - (t.fee_amount or ZERO) - (t.tax_amount or ZERO)
        if t.net_amount is not None and t.net_amount != computed:
            flags.append(f"Record {t.id}: gross − fee − tax ({computed}) ≠ net ({t.net_amount})")
        return sign * (t.net_amount if t.net_amount is not None else computed), flags
    if t.net_amount is not None:
        return sign * t.net_amount, flags
    return t.flow, flags


def match_fee_net(rule, left, right):
    """
    Match bank settlement credits (net) against settlement records (gross, fee,
    tax, net), grouped by settlement batch reference.

    Matching uses the records' net settlement amounts. The persisted match
    difference is still computed from flow amounts, so if a settlement profile
    maps the *gross* amount as the record amount, the fees surface as a
    difference that must be reviewed rather than silently absorbed.
    """

    def has_settlement_fields(items):
        return sum(1 for t in items if t.gross_amount is not None or t.net_amount is not None)

    swap = has_settlement_fields(left) > has_settlement_fields(right)
    bank, records = (right, left) if swap else (left, right)
    bank_field = rule.right_reference_field if swap else rule.left_reference_field
    record_field = rule.left_reference_field if swap else rule.right_reference_field
    if record_field in ("transaction_id",):
        record_field = "settlement_batch_ref"

    groups: dict = defaultdict(list)
    for t in records:
        key = t.ref(record_field)
        if key:
            groups[key].append(t)
    proposals, used = [], set()
    for key, members in groups.items():
        if len(members) > rule.max_group_size:
            continue
        net_total, flags = ZERO, []
        for m in members:
            net, f = settlement_net(m)
            net_total += net
            flags += f
        if bank_field == "narration_contains":
            candidates = [b for b in bank if key in b.norm_narration]
        else:
            candidates = [b for b in bank if b.ref(bank_field) == key]
        candidates = [
            b
            for b in candidates
            if b.id not in used
            and _amount_ok(b.flow, net_total, rule.amount_tolerance)
            and all(_within_days(b, m, rule.date_tolerance_days) for m in members)
        ]
        if not candidates:
            continue
        best = min(candidates, key=lambda b: (abs(b.flow - net_total), b.id))
        used.add(best.id)
        reason = f"Net settlement of batch {key} ({len(members)} record(s)) equals bank amount"
        proposal = (
            Proposal(members, [best], len(candidates) == 1, reason, flags=flags)
            if swap
            else Proposal([best], members, len(candidates) == 1, reason, flags=flags)
        )
        proposals.append(proposal)
    return proposals


def match_reversal_pairs(rule, left, right):
    proposals = []
    for side_items, field_name, is_left in (
        (left, rule.left_reference_field, True),
        (right, rule.right_reference_field, False),
    ):
        groups: dict = defaultdict(list)
        for t in side_items:
            key = t.ref(field_name)
            if key:
                groups[(key, abs(t.flow))].append(t)
        for (key, _), members in groups.items():
            positives = sorted([m for m in members if m.flow > 0], key=lambda t: (t.date, t.id))
            negatives = sorted([m for m in members if m.flow < 0], key=lambda t: (t.date, t.id))
            unique = len(positives) == 1 and len(negatives) == 1
            used = set()
            for p in positives:
                options = [n for n in negatives if n.id not in used and _within_days(p, n, rule.date_tolerance_days)]
                if not options:
                    continue
                n = min(options, key=lambda x: (not x.is_reversal, abs((p.date - x.date).days), x.id))
                used.add(n.id)
                pair = [p, n]
                proposals.append(
                    Proposal(
                        pair if is_left else [],
                        [] if is_left else pair,
                        unique,
                        f"Entry and reversal with reference {key}",
                        kind="REVERSAL",
                    )
                )
    return proposals


def match_similar_narration(rule, left, right):
    threshold = float(rule.similarity_threshold)
    by_flow: dict = defaultdict(list)
    for r in right:
        by_flow[r.flow].append(r)
    proposals, used = [], set()
    for lt in left:
        if not lt.norm_narration:
            continue
        pool = by_flow.get(lt.flow, []) if rule.amount_tolerance == ZERO else right
        scored = []
        for r in pool:
            if r.id in used or not r.norm_narration or not _within_days(lt, r, rule.date_tolerance_days):
                continue
            if not _amount_ok(lt.flow, r.flow, rule.amount_tolerance):
                continue
            score = difflib.SequenceMatcher(None, lt.norm_narration, r.norm_narration).ratio()
            if score >= threshold:
                scored.append((score, r))
        if scored:
            score, best = max(scored, key=lambda s: (s[0], -s[1].id))
            used.add(best.id)
            proposals.append(Proposal([lt], [best], False, f"Narration similarity {score:.2f} (fuzzy)"))
    return proposals


STRATEGIES = {
    Strategy.EXACT_REFERENCE: match_reference,
    Strategy.EXTERNAL_ID: match_reference,
    Strategy.AMOUNT_DATE: match_amount_date,
    Strategy.NARRATION_AMOUNT: match_narration,
    Strategy.COMPOSITE: match_composite,
    Strategy.ONE_TO_MANY: match_one_to_many,
    Strategy.MANY_TO_ONE: match_many_to_one,
    Strategy.FEE_NET: match_fee_net,
    Strategy.REVERSAL_PAIR: match_reversal_pairs,
    Strategy.NARRATION_SIMILARITY: match_similar_narration,
}


def find_duplicates(items: list[Txn]) -> list[list[Txn]]:
    """Groups of same-side items with identical date, amount and reference/narration."""
    groups: dict = defaultdict(list)
    for t in items:
        ident = t.norm_transaction_id or t.norm_external_reference or t.norm_narration
        groups[(t.date, t.flow, ident)].append(t)
    return [g for g in groups.values() if len(g) > 1]
