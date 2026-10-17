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
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from itertools import combinations

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
    time_of_day: time | None = None
    row: int = 0

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


# --------------------------------------------------------------------------
# Bulk / session settlement
# --------------------------------------------------------------------------
#
# A settlement account statement shows one bulk line per settlement session
# (e.g. one NIP inward credit of 320,000 for a session), while the core
# banking GL shows every transfer and charge individually. The two strategies
# below rebuild those bulk amounts from the individual items.

BOUNDARY_POOL_LIMIT = 12  # boundary items considered when moving items between sessions
SUBSET_CANDIDATE_LIMIT = 60  # items considered by the free amount search per bulk line
SUBSET_NODE_BUDGET = 200_000  # search steps per bulk line


def _sides(rule, left, right):
    """(bulk items, detail items, bulk is left)."""
    if getattr(rule, "bulk_side", "LEFT") == "RIGHT":
        return right, left, False
    return left, right, True


def _proposal(bulk_is_left, lines, items, unique, reason, flags=None, kind="MATCH"):
    if bulk_is_left:
        return Proposal(list(lines), list(items), unique, reason, kind, list(flags or []))
    return Proposal(list(items), list(lines), unique, reason, kind, list(flags or []))


def _cutoffs(rule) -> list[time]:
    return [time.fromisoformat(c) for c in (rule.session_cutoffs or [])]


def session_of(t_date: date, t_time: time, cutoffs: list[time]) -> tuple[date, int]:
    """
    Settlement session of a transaction: (session date, 1-based session number).

    Session *n* covers times after cut-off n-1 up to and including cut-off n;
    items after the last cut-off fall into session 1 of the next day.
    """
    for number, cutoff in enumerate(cutoffs, start=1):
        if t_time <= cutoff:
            return t_date, number
    return t_date + timedelta(days=1), 1


def _neighbour(t_date: date, t_time: time, cutoffs: list[time], minutes: int):
    """The adjacent session an item near a cut-off may have settled in, or None."""
    if not minutes:
        return None
    moment = datetime.combine(t_date, t_time)
    window = timedelta(minutes=minutes)
    for day in (t_date - timedelta(days=1), t_date, t_date + timedelta(days=1)):
        for cutoff in cutoffs:
            edge = datetime.combine(day, cutoff)
            if abs(moment - edge) > window:
                continue
            # The item sits at/just before (just after) this cut-off: the alternative is the other side of it.
            other = edge + timedelta(seconds=1) if moment <= edge else edge
            return session_of(other.date(), other.time(), cutoffs)
    return None


def _is_fee(t: Txn, keywords) -> bool:
    words = set(t.norm_narration.split())
    return any(k.upper() in words or (" " in k and k.upper() in t.norm_narration) for k in keywords)


def _session_label(key) -> str:
    return f"{key[0].isoformat()} session {key[1]}"


def match_session_bulk(rule, left, right):
    """
    Group detail items (e.g. GL transfers and charges) by settlement session
    and direction, and match each group's total to one bulk line on the
    other side dated within the rule's date tolerance of the session date.

    For each session and direction, in order:

    1. all items (principal and charges settled together);
    2. principal and charges separately, when fee keywords are configured
       (charges settled as their own bulk line);
    3. items near a cut-off moved to or from the neighbouring session
       (only when ``session_boundary_minutes`` is set; never unique, so a
       person confirms the move);
    4. inward and outward items of a session netted against one line
       (never unique).

    Items without a time of day cannot be placed in a session and are left
    for other rules (e.g. the bulk amount finder).
    """
    cutoffs = _cutoffs(rule)
    if not cutoffs:
        return []
    bulk, detail, bulk_is_left = _sides(rule, left, right)
    tol_days, tol_amount = rule.date_tolerance_days, rule.amount_tolerance
    keywords = list(rule.fee_keywords or [])

    groups: dict = defaultdict(list)
    alternatives: dict[int, tuple] = {}
    for t in detail:
        if t.time_of_day is None or t.flow == ZERO:
            continue
        key = session_of(t.date, t.time_of_day, cutoffs)
        sign = 1 if t.flow > 0 else -1
        groups[(key, sign)].append(t)
        alt = _neighbour(t.date, t.time_of_day, cutoffs, rule.session_boundary_minutes)
        if alt is not None and alt != key:
            alternatives[t.id] = alt

    lines_by_sign: dict = defaultdict(list)
    for b in bulk:
        if b.flow != ZERO:
            lines_by_sign[1 if b.flow > 0 else -1].append(b)
    used_lines: set[int] = set()
    used_items: set[int] = set()
    proposals = []

    def find_lines(total, sign, session_date):
        return [
            b
            for b in lines_by_sign[sign]
            if b.id not in used_lines
            and _amount_ok(b.flow, total, tol_amount)
            and abs((b.date - session_date).days) <= tol_days
        ]

    def best_line(candidates, total, session_date):
        return min(candidates, key=lambda b: (abs(b.flow - total), abs((b.date - session_date).days), b.id))

    def emit(lines, items, unique, reason, flags=None):
        used_lines.update(b.id for b in lines)
        used_items.update(t.id for t in items)
        proposals.append(_proposal(bulk_is_left, lines, items, unique, reason, flags))

    ordered = sorted(groups, key=lambda k: (k[0], -k[1]))
    leftovers = []
    for key, sign in ordered:
        members = [t for t in groups[(key, sign)] if t.id not in used_items]
        if not members or len(members) > rule.max_group_size:
            continue
        session_date = key[0]
        label = f"{'Inward' if sign > 0 else 'Outward'} {_session_label(key)}"
        total = sum((t.flow for t in members), ZERO)

        # 1. Everything in the session settled as one bulk line.
        candidates = find_lines(total, sign, session_date)
        if candidates:
            emit(
                [best_line(candidates, total, session_date)],
                members,
                len(candidates) == 1,
                f"{label}: {len(members)} item(s) total {total} equal one bulk line",
            )
            continue

        # 2. Charges settled separately from the principal amounts.
        if keywords:
            fees = [t for t in members if _is_fee(t, keywords)]
            principal = [t for t in members if not _is_fee(t, keywords)]
            if fees and principal:
                p_total = sum((t.flow for t in principal), ZERO)
                p_lines = find_lines(p_total, sign, session_date)
                if p_lines:
                    emit(
                        [best_line(p_lines, p_total, session_date)],
                        principal,
                        len(p_lines) == 1,
                        f"{label}: {len(principal)} principal item(s) total {p_total} equal one bulk line",
                    )
                    f_total = sum((t.flow for t in fees), ZERO)
                    f_lines = find_lines(f_total, sign, session_date)
                    if f_lines:
                        emit(
                            [best_line(f_lines, f_total, session_date)],
                            fees,
                            len(f_lines) == 1,
                            f"{label}: {len(fees)} charge item(s) total {f_total} equal one bulk line",
                        )
                    continue
        leftovers.append((key, sign))

    # 3. Items near a cut-off that settled in the neighbouring session.
    if rule.session_boundary_minutes:
        for key, sign in leftovers:
            members = [t for t in groups[(key, sign)] if t.id not in used_items]
            if not members:
                continue
            removable = [t for t in members if t.id in alternatives]
            addable = [
                t
                for (k, s), items in groups.items()
                if s == sign and k != key
                for t in items
                if t.id not in used_items and alternatives.get(t.id) == key
            ]
            pool = (removable + addable)[:BOUNDARY_POOL_LIMIT]
            if not pool:
                continue
            base = sum((t.flow for t in members), ZERO)
            hit = None
            for size in range(1, len(pool) + 1):
                for combo in combinations(pool, size):
                    moved_out = [t for t in combo if t in removable]
                    moved_in = [t for t in combo if t not in removable]
                    total = base - sum((t.flow for t in moved_out), ZERO) + sum((t.flow for t in moved_in), ZERO)
                    candidates = find_lines(total, sign, key[0])
                    if candidates:
                        hit = (combo, moved_out, moved_in, total, candidates)
                        break
                if hit:
                    break
            if not hit:
                continue
            _, moved_out, moved_in, total, candidates = hit
            items = [t for t in members if t not in moved_out] + moved_in
            if not items or len(items) > rule.max_group_size:
                continue
            label = f"{'Inward' if sign > 0 else 'Outward'} {_session_label(key)}"
            emit(
                [best_line(candidates, total, key[0])],
                items,
                False,
                f"{label}: total {total} equals one bulk line after moving "
                f"{len(moved_out) + len(moved_in)} item(s) across a session cut-off",
            )

    # 4. Inward and outward items of a session settled net.
    for key in sorted({k for k, _ in groups}):
        items = [t for s in (1, -1) for t in groups.get((key, s), []) if t.id not in used_items]
        if not any(t.flow > 0 for t in items) or not any(t.flow < 0 for t in items):
            continue
        if len(items) > rule.max_group_size:
            continue
        total = sum((t.flow for t in items), ZERO)
        if total == ZERO:
            continue
        candidates = find_lines(total, 1 if total > 0 else -1, key[0])
        if candidates:
            emit(
                [best_line(candidates, total, key[0])],
                items,
                False,
                f"Net of inward and outward items in {_session_label(key)} ({total}) equals one bulk line",
            )
    return proposals


def _order_key(t: Txn):
    return (t.date, t.time_of_day or time.min, t.row, t.id)


def _contiguous_runs(items: list[Txn], target: Decimal, max_size: int) -> list[tuple[int, int]]:
    """All runs items[i:j] (2 ≤ length ≤ max_size) whose amounts add up to target (amounts all the same sign)."""
    runs, total, start = [], ZERO, 0
    for end, t in enumerate(items):
        total += abs(t.flow)
        while total > target and start <= end:
            total -= abs(items[start].flow)
            start += 1
        if total == target and 2 <= end - start + 1 <= max_size:
            runs.append((start, end + 1))
    return runs


def _subset_search(items: list[Txn], target: Decimal, max_size: int, limit: int = 2):
    """Up to `limit` subsets (2..max_size items) adding up exactly to target; bounded depth-first search."""
    values = sorted(items, key=lambda t: (-abs(t.flow), t.id))
    amounts = [abs(t.flow) for t in values]
    suffix = [ZERO] * (len(amounts) + 1)
    for i in range(len(amounts) - 1, -1, -1):
        suffix[i] = suffix[i + 1] + amounts[i]
    found: list[list[Txn]] = []
    steps = 0
    chosen: list[int] = []

    def walk(i, remaining):
        nonlocal steps
        if len(found) >= limit or steps > SUBSET_NODE_BUDGET:
            return
        steps += 1
        if remaining == ZERO:
            if len(chosen) >= 2:
                found.append([values[k] for k in chosen])
            return
        if i == len(values) or len(chosen) >= max_size or suffix[i] < remaining:
            return
        if amounts[i] <= remaining:
            chosen.append(i)
            walk(i + 1, remaining - amounts[i])
            chosen.pop()
        # Skip equal amounts at this position to avoid enumerating the same total twice.
        j = i + 1
        while j < len(values) and amounts[j] == amounts[i]:
            j += 1
        walk(j, remaining)

    walk(0, target)
    return found, steps > SUBSET_NODE_BUDGET


def match_bulk_subset(rule, left, right):
    """
    Bulk amount finder for detail items that carry no session ID and no time.

    For each bulk line, considers unmatched detail items in the same direction
    dated from (line date − date tolerance) to the line date, in source order
    (date, time, file row), and looks for:

    1. a contiguous run of items whose total equals the bulk amount: sessions
       are consecutive in a chronological GL export, so this is usually the
       session (high-confidence candidate, a person accepts it);
    2. otherwise any combination of up to ``max_group_size`` items that adds
       up exactly, using a bounded search over the nearest
       ``SUBSET_CANDIDATE_LIMIT`` items (always manual review).

    The amount must agree exactly; this strategy is never auto-accepted.
    """
    bulk, detail, bulk_is_left = _sides(rule, left, right)
    tol_days = rule.date_tolerance_days
    by_sign: dict = defaultdict(list)
    for t in detail:
        if t.flow != ZERO:
            by_sign[1 if t.flow > 0 else -1].append(t)
    for items in by_sign.values():
        items.sort(key=_order_key)
    used: set[int] = set()
    proposals = []
    for line in sorted(bulk, key=lambda b: (b.date, b.id)):
        if line.flow == ZERO:
            continue
        sign = 1 if line.flow > 0 else -1
        target = abs(line.flow)
        earliest = line.date - timedelta(days=tol_days)
        candidates = [t for t in by_sign[sign] if t.id not in used and earliest <= t.date <= line.date]
        if len(candidates) < 2:
            continue
        runs = _contiguous_runs(candidates, target, rule.max_group_size)
        if runs:
            start, end = runs[0]
            items = candidates[start:end]
            used.update(t.id for t in items)
            proposals.append(
                _proposal(
                    bulk_is_left,
                    [line],
                    items,
                    len(runs) == 1,
                    f"{len(items)} consecutive item(s) from {items[0].date} total {target}, equal to the bulk line",
                    flags=[] if len(runs) == 1 else [f"{len(runs)} different runs add up to this amount"],
                )
            )
            continue
        nearest = sorted(
            (t for t in candidates if abs(t.flow) <= target),
            key=lambda t: ((line.date - t.date).days, t.id),
        )[:SUBSET_CANDIDATE_LIMIT]
        found, exhausted = _subset_search(nearest, target, rule.max_group_size)
        if not found:
            continue
        items = sorted(found[0], key=_order_key)
        used.update(t.id for t in items)
        flags = ["Items found by amount search: confirm they belong to this bulk line"]
        if len(found) > 1:
            flags.append("Another combination of items also adds up to this amount")
        if exhausted:
            flags.append("Search limit reached; other combinations were not examined")
        proposals.append(
            _proposal(
                bulk_is_left,
                [line],
                items,
                False,
                f"{len(items)} item(s) adding up to bulk line {target}",
                flags=flags,
            )
        )
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
    Strategy.SESSION_BULK: match_session_bulk,
    Strategy.BULK_SUBSET: match_bulk_subset,
}


def find_duplicates(items: list[Txn]) -> list[list[Txn]]:
    """Groups of same-side items with identical date, amount and reference/narration."""
    groups: dict = defaultdict(list)
    for t in items:
        ident = t.norm_transaction_id or t.norm_external_reference or t.norm_narration
        groups[(t.date, t.flow, ident)].append(t)
    return [g for g in groups.values() if len(g) > 1]
