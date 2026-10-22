"""
Bulk / session settlement: a settlement account statement shows one bulk line
per NIP/NIBSS session while the core banking GL shows every transfer and
charge individually.
"""

from datetime import time
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.imports.models import ImportProfile, TimeFromReference
from apps.imports.normalize import normalize_row, parse_time, time_from_reference
from apps.reconciliation import engine, services
from apps.reconciliation.models import MatchGroup, MatchingRule, Strategy

from .helpers import bank_profile, csv_bytes, d, gl_profile, import_file, source_batch, txn

D = Decimal
CUTOFFS = ["06:00", "12:00", "18:00"]


def T(id, side, flow, day="2026-10-17", at=None, row=0, narration=""):
    return engine.Txn(
        id=id,
        side=side,
        flow=D(str(flow)),
        date=d(day),
        time_of_day=time.fromisoformat(at) if at else None,
        row=row or id,
        norm_narration=narration,
    )


def rule(strategy=Strategy.SESSION_BULK, **kw):
    defaults = dict(
        name=strategy,
        strategy=strategy,
        date_tolerance_days=0,
        amount_tolerance=D("0"),
        auto_accept=True,
        max_group_size=5000,
        session_cutoffs=CUTOFFS,
        session_boundary_minutes=0,
        fee_keywords=[],
        bulk_side="LEFT",
    )
    defaults.update(kw)
    return MatchingRule(**defaults)


# ------------------------------------------------------------------ sessions


def test_session_assignment():
    cut = [time.fromisoformat(c) for c in CUTOFFS]
    day = d("2026-10-17")
    assert engine.session_of(day, time(5, 59), cut) == (day, 1)
    assert engine.session_of(day, time(6, 0), cut) == (day, 1)
    assert engine.session_of(day, time(6, 0, 1), cut) == (day, 2)
    assert engine.session_of(day, time(17, 59), cut) == (day, 3)
    assert engine.session_of(day, time(19, 0), cut) == (d("2026-10-18"), 1)


def test_inward_session_bulk_matches_and_auto_accepts():
    # Chike 20,000, Bola 100,000 and a business 200,000 in session 2; statement shows 320,000 once.
    gl = [
        T(1, "RIGHT", "20000", at="07:10"),
        T(2, "RIGHT", "100000", at="08:30"),
        T(3, "RIGHT", "200000", at="11:45"),
        T(4, "RIGHT", "55000", at="13:05"),  # session 3
    ]
    bank = [T(10, "LEFT", "320000"), T(11, "LEFT", "55000")]
    r = rule()
    proposals = engine.match_session_bulk(r, bank, gl)
    assert len(proposals) == 2
    first = next(p for p in proposals if p.left[0].id == 10)
    assert sorted(t.id for t in first.right) == [1, 2, 3]
    assert first.difference == 0 and first.unique
    assert services.classify(r, first) == MatchGroup.Category.EXACT


def test_outward_principal_and_charges_in_one_bulk_line():
    gl = [
        T(1, "RIGHT", "-50000", at="13:00", narration="NIP TRANSFER TO ADA"),
        T(2, "RIGHT", "-10.75", at="13:00", narration="NIP TRANSFER CHARGE"),
        T(3, "RIGHT", "-0.81", at="13:00", narration="VAT ON NIP CHARGE"),
        T(4, "RIGHT", "-25000", at="14:00", narration="NIP TRANSFER TO OBI"),
        T(5, "RIGHT", "-10.75", at="14:00", narration="NIP TRANSFER CHARGE"),
        T(6, "RIGHT", "-0.81", at="14:00", narration="VAT ON NIP CHARGE"),
    ]
    bank = [T(10, "LEFT", "-75023.12")]
    proposals = engine.match_session_bulk(rule(), bank, gl)
    assert len(proposals) == 1 and len(proposals[0].right) == 6 and proposals[0].difference == 0


def test_outward_charges_settled_as_separate_bulk_line():
    gl = [
        T(1, "RIGHT", "-50000", at="13:00", narration="NIP TRANSFER TO ADA"),
        T(2, "RIGHT", "-10.75", at="13:00", narration="NIP TRANSFER CHARGE"),
        T(3, "RIGHT", "-0.81", at="13:00", narration="VAT ON NIP CHARGE"),
        T(4, "RIGHT", "-25000", at="14:00", narration="NIP TRANSFER TO OBI"),
        T(5, "RIGHT", "-10.75", at="14:00", narration="NIP TRANSFER CHARGE"),
        T(6, "RIGHT", "-0.81", at="14:00", narration="VAT ON NIP CHARGE"),
    ]
    bank = [T(10, "LEFT", "-75000"), T(11, "LEFT", "-23.12")]
    assert engine.match_session_bulk(rule(), bank, gl) == []  # without fee keywords nothing adds up
    proposals = engine.match_session_bulk(rule(fee_keywords=["CHARGE", "VAT"]), bank, gl)
    by_line = {p.left[0].id: sorted(t.id for t in p.right) for p in proposals}
    assert by_line == {10: [1, 4], 11: [2, 3, 5, 6]}
    assert all(p.difference == 0 for p in proposals)


def test_item_near_cut_off_moved_to_next_session_needs_review():
    gl = [
        T(1, "RIGHT", "20000", at="10:00"),
        T(2, "RIGHT", "30000", at="11:59:30"),  # posted just before the 12:00 cut-off, settled in session 3
        T(3, "RIGHT", "40000", at="13:00"),
    ]
    bank = [T(10, "LEFT", "20000"), T(11, "LEFT", "70000")]
    assert engine.match_session_bulk(rule(), bank, gl) == []  # sessions total 50,000 and 40,000 without a window
    r = rule(session_boundary_minutes=5)
    proposals = engine.match_session_bulk(r, bank, gl)
    by_line = {p.left[0].id: sorted(t.id for t in p.right) for p in proposals}
    assert by_line == {10: [1], 11: [2, 3]}
    moved = [p for p in proposals if p.left[0].id == 11][0]
    assert not moved.unique and services.classify(r, moved) == MatchGroup.Category.MANUAL_REVIEW


def test_net_settlement_of_a_session_is_proposed_for_review():
    gl = [T(1, "RIGHT", "100000", at="09:00"), T(2, "RIGHT", "-30000", at="09:30")]
    bank = [T(10, "LEFT", "70000")]
    proposals = engine.match_session_bulk(rule(), bank, gl)
    assert len(proposals) == 1 and len(proposals[0].right) == 2 and not proposals[0].unique


def test_items_without_time_are_left_for_other_rules():
    gl = [T(1, "RIGHT", "20000"), T(2, "RIGHT", "300000")]
    assert engine.match_session_bulk(rule(), [T(10, "LEFT", "320000")], gl) == []


def test_bulk_side_right():
    statement = [T(10, "RIGHT", "320000")]
    gl = [T(1, "LEFT", "20000", at="07:00"), T(2, "LEFT", "300000", at="08:00")]
    proposals = engine.match_session_bulk(rule(bulk_side="RIGHT"), gl, statement)
    assert len(proposals) == 1 and [t.id for t in proposals[0].right] == [10] and len(proposals[0].left) == 2


# ------------------------------------------------------------------ amount finder (no session data)


def test_amount_finder_contiguous_run_is_high_confidence():
    gl = [
        T(1, "RIGHT", "5000", row=1),
        T(2, "RIGHT", "20000", row=2),
        T(3, "RIGHT", "100000", row=3),
        T(4, "RIGHT", "200000", row=4),
        T(5, "RIGHT", "7000", row=5),
    ]
    r = rule(Strategy.BULK_SUBSET, session_cutoffs=[])
    proposals = engine.match_bulk_subset(r, [T(10, "LEFT", "320000")], gl)
    assert len(proposals) == 1 and sorted(t.id for t in proposals[0].right) == [2, 3, 4]
    assert services.classify(r, proposals[0]) == MatchGroup.Category.HIGH_CONFIDENCE


def test_amount_finder_free_search_is_manual_review():
    gl = [
        T(1, "RIGHT", "20000", row=1),
        T(2, "RIGHT", "3000", row=2),
        T(3, "RIGHT", "100000", row=3),
        T(4, "RIGHT", "9000", row=4),
        T(5, "RIGHT", "200000", row=5),
    ]
    r = rule(Strategy.BULK_SUBSET, session_cutoffs=[])
    proposals = engine.match_bulk_subset(r, [T(10, "LEFT", "320000")], gl)
    assert len(proposals) == 1 and sorted(t.id for t in proposals[0].right) == [1, 3, 5]
    assert services.classify(r, proposals[0]) == MatchGroup.Category.MANUAL_REVIEW


def test_amount_finder_respects_direction_and_dates():
    gl = [
        T(1, "RIGHT", "20000", day="2026-10-15"),  # outside the 1-day window
        T(2, "RIGHT", "300000", day="2026-10-17"),
        T(3, "RIGHT", "-20000", day="2026-10-17"),  # wrong direction
    ]
    r = rule(Strategy.BULK_SUBSET, session_cutoffs=[], date_tolerance_days=1)
    assert engine.match_bulk_subset(r, [T(10, "LEFT", "320000")], gl) == []


# ------------------------------------------------------------------ time of day on import


def test_parse_time_formats():
    assert parse_time("14:32") == time(14, 32)
    assert parse_time("14:32:05") == time(14, 32, 5)
    assert parse_time("2:05 PM") == time(14, 5)
    assert parse_time("2026-10-17T07:10:00") == time(7, 10)


def test_time_from_nip_session_id():
    ref = "000013" + "261017143205" + "123456789012"
    assert time_from_reference([ref], TimeFromReference.NIP, txn_date=d("2026-10-17")) == time(14, 32, 5)
    assert time_from_reference([f"NIP/{ref}/ADA"], TimeFromReference.NIP, txn_date=d("2026-10-17")) == time(14, 32, 5)
    # Embedded date far from the transaction date: not a session ID.
    assert time_from_reference([ref], TimeFromReference.NIP, txn_date=d("2026-06-01")) is None
    assert time_from_reference(["FT26290ABC"], TimeFromReference.NIP, txn_date=d("2026-10-17")) is None
    assert time_from_reference([ref], TimeFromReference.NONE, txn_date=d("2026-10-17")) is None


def test_custom_reference_pattern():
    pattern = r"T(?P<HH>\d{2})(?P<MM>\d{2})"
    assert time_from_reference(["FT-T0915-22"], TimeFromReference.PATTERN, pattern) == time(9, 15)


def test_normalize_row_reads_time_from_column_date_cell_or_reference():
    base = dict(
        amount_mode="SIGNED",
        signed_positive_is="DEBIT",
        date_formats=["%d/%m/%Y"],
        default_account_reference="1502",
        default_currency="NGN",
    )
    profile = ImportProfile(
        column_mapping={"transaction_date": "Date", "amount": "Amount", "transaction_time": "Time"}, **base
    )
    out, errors = normalize_row({"Date": "17/10/2026", "Amount": "100", "Time": "07:10"}, profile)
    assert not errors and out["transaction_time"] == time(7, 10)
    out, errors = normalize_row({"Date": "17/10/2026", "Amount": "100", "Time": "99:99"}, profile)
    assert errors

    profile = ImportProfile(column_mapping={"transaction_date": "Date", "amount": "Amount"}, **base)
    out, errors = normalize_row({"Date": "17/10/2026 14:32:05", "Amount": "100"}, profile)
    assert not errors and out["transaction_date"] == d("2026-10-17") and out["transaction_time"] == time(14, 32, 5)

    profile = ImportProfile(
        column_mapping={"transaction_date": "Date", "amount": "Amount", "transaction_id": "Ref"},
        time_from_reference=TimeFromReference.NIP,
        **base,
    )
    ref = "000013261017093000123456789012"
    out, errors = normalize_row({"Date": "17/10/2026", "Amount": "100", "Ref": ref}, profile)
    assert not errors and out["transaction_time"] == time(9, 30)
    assert out["source_values"]["transaction_time"] == "derived from reference"


def test_rule_validation():
    with pytest.raises(ValidationError):
        rule(session_cutoffs=["25:00"]).clean()
    with pytest.raises(ValidationError):
        rule(session_cutoffs=["12:00", "06:00"]).clean()
    with pytest.raises(ValidationError):
        rule(session_cutoffs=[]).clean()
    rule(Strategy.BULK_SUBSET, session_cutoffs=[]).clean()
    rule().clean()


def test_rule_form_parses_lists(institution):
    from apps.reconciliation.forms import MatchingRuleForm

    data = {
        "name": "NIP sessions",
        "strategy": Strategy.SESSION_BULK,
        "priority": 5,
        "is_active": "on",
        "recon_type": "",
        "date_tolerance_days": 0,
        "amount_tolerance": "0",
        "auto_accept": "on",
        "left_reference_field": "transaction_id",
        "right_reference_field": "transaction_id",
        "max_group_size": 5000,
        "similarity_threshold": "0.85",
        "bulk_side": "LEFT",
        "session_boundary_minutes": 2,
        "session_cutoffs_text": "6:00, 12:00, 18:00",
        "fee_keywords_text": "charge, vat",
    }
    form = MatchingRuleForm(data, instance=MatchingRule(institution=institution))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["session_cutoffs"] == ["06:00", "12:00", "18:00"]
    assert form.cleaned_data["fee_keywords"] == ["CHARGE", "VAT"]
    bad = MatchingRuleForm({**data, "session_cutoffs_text": ""}, instance=MatchingRule(institution=institution))
    assert not bad.is_valid()


# ------------------------------------------------------------------ end to end


def nip(hhmmss, n):
    return f"000013261017{hhmmss}{n:012d}"


def test_session_bulk_end_to_end(institution, users, client):
    from apps.settlements.services import session_report

    user = users["recon"]
    profile = gl_profile(
        institution,
        user,
        name="Core GL NIP",
        default_account_reference="1502",
        time_from_reference=TimeFromReference.NIP,
    )
    rows = [
        # Inward transfers (debit the settlement GL): session 2 → one 320,000 credit on the statement
        ["2026-10-17", nip("071000", 1), "NIP INWARD CHIKE", "20000.00", "1502"],
        ["2026-10-17", nip("083000", 2), "NIP INWARD BOLA", "100000.00", "1502"],
        ["2026-10-17", nip("114500", 3), "NIP INWARD ACME LTD", "200000.00", "1502"],
        # Outward transfer and its charges: session 3 → one 50,011.56 debit
        ["2026-10-17", nip("130000", 4), "NIP OUTWARD TO ADA", "-50000.00", "1502"],
        ["2026-10-17", nip("130000", 5), "NIP TRANSFER CHARGE", "-10.75", "1502"],
        ["2026-10-17", nip("130000", 6), "VAT ON NIP CHARGE", "-0.81", "1502"],
        # Internal reference without time: left for the amount finder
        ["2026-10-17", "FT2629011", "NIP INWARD OBI", "15000.00", "1502"],
        ["2026-10-17", "FT2629012", "NIP INWARD EZE", "5000.00", "1502"],
    ]
    data = csv_bytes(rows, ["TranDate", "TranRef", "Description", "Amount", "GL"])
    import_file(institution, user, profile, "gl.csv", data)

    bank = source_batch(
        institution, user, bank_profile(institution, user, name="Settlement stmt", default_account_reference="1502")
    )
    txn(bank, day="2026-10-17", amount="320000.00", ref="NIPS2", narration="NIP INWARD SETTLEMENT SESSION 2")
    txn(bank, day="2026-10-17", amount="-50011.56", ref="NIPS3", narration="NIP OUTWARD SETTLEMENT SESSION 3")
    txn(bank, day="2026-10-17", amount="20000.00", ref="NIPS4", narration="NIP INWARD SETTLEMENT")

    sessions = MatchingRule.objects.create(
        institution=institution,
        name="NIP sessions",
        strategy=Strategy.SESSION_BULK,
        priority=10,
        auto_accept=True,
        max_group_size=5000,
        session_cutoffs=CUTOFFS,
    )
    finder = MatchingRule.objects.create(
        institution=institution, name="Amount finder", strategy=Strategy.BULK_SUBSET, priority=20, max_group_size=5000
    )
    recon = services.create_reconciliation(
        institution,
        user,
        name="NIP settlement 17 Oct",
        recon_type="SETTLEMENT",
        left_account_reference="1502",
        right_account_reference="1502",
        period_start=d("2026-10-17"),
        period_end=d("2026-10-17"),
        rules=[sessions, finder],
    )
    summary = services.run_reconciliation(recon, user)
    assert summary["auto_accepted"] == 2
    assert summary["high_confidence"] == 1
    assert summary["unmatched_left"] == 0 and summary["unmatched_right"] == 0

    report = session_report(recon)
    by_key = {(r.session, r.direction): r for r in report.rows}
    inward = by_key[(2, "Inward")]
    assert inward.items == 3 and inward.items_total == D("320000.00") and inward.status == "Matched"
    assert inward.bulk_total == D("320000.00") and inward.difference == 0
    outward = by_key[(3, "Outward")]
    assert outward.items == 3 and outward.items_total == D("-50011.56") and outward.status == "Matched"
    assert report.untimed_items == 2 and not report.unmatched_bulk

    client.force_login(users["manager"])
    url = reverse("reporting:recon-sessions", args=[recon.pk])
    resp = client.get(url)
    assert resp.status_code == 200 and b"Settlement Sessions" in resp.content
    resp = client.get(url + "?export=xlsx")
    assert resp.status_code == 200 and resp["Content-Type"].startswith("application/vnd.openxmlformats")


# ------------------------------------------------------------------ session labels and bank reversals


NIBSS_CUTOFFS = [
    "06:00",
    "09:00",
    "10:00",
    "11:00",
    "12:00",
    "13:00",
    "14:00",
    "15:00",
    "17:00",
    "19:00",
    "21:00",
    "24:00",
]


def L(id, flow, day, narration, row=0):
    from apps.imports.normalize import normalize_narration

    return engine.Txn(
        id=id, side="LEFT", flow=D(str(flow)), date=d(day), row=row or id, norm_narration=normalize_narration(narration)
    )


def test_end_of_day_cut_off_and_nibss_numbering():
    cut = engine._cutoffs(rule(session_cutoffs=NIBSS_CUTOFFS))
    day = d("2026-09-01")
    assert engine.session_of(day, time(23, 59, 59), cut) == (day, 12)
    assert engine.session_of(day, time(10, 14, 30), cut) == (day, 4)
    assert engine.session_of(day, time(0, 38), cut) == (day, 1)
    rule(session_cutoffs=NIBSS_CUTOFFS).clean()
    with pytest.raises(ValidationError):
        rule(session_cutoffs=["24:00", "06:00"]).clean()


def test_session_label_ties_line_to_its_session_even_when_posted_next_day():
    r = rule(session_cutoffs=NIBSS_CUTOFFS, session_label_format="NIBSS", date_tolerance_days=0)
    gl = [
        T(1, "RIGHT", "117000", day="2026-08-31", at="19:30"),  # session 11 of 31 Aug
        T(2, "RIGHT", "5900", day="2026-08-31", at="22:10"),  # session 12 of 31 Aug
    ]
    bank = [
        L(10, "5900", "2026-09-01", "SETTLEMENT TRANSACTIONS   31.08.2026 12th  SESSION NIP SETT PRODIGY MFB"),
        L(11, "117000", "2026-09-01", "SETTLEMENT TRANSACTIONS   31.08.2026 11TH  SESSION NIP SETT PRODIGY MFB"),
    ]
    proposals = engine.match_session_bulk(r, bank, gl)
    pairs = {p.left[0].id: [t.id for t in p.right] for p in proposals}
    assert pairs == {10: [2], 11: [1]} and all(p.unique for p in proposals)
    # A line naming another session is never used for this one, whatever its amount.
    wrong = [L(12, "117000", "2026-09-01", "SETTLEMENT TRANSACTIONS 31.08.2026 10th SESSION NIP SETT")]
    assert engine.match_session_bulk(r, wrong, gl[:1]) == []


def test_bank_posting_and_reversal_under_same_session_label_are_paired():
    r = rule(session_cutoffs=NIBSS_CUTOFFS, session_label_format="NIBSS")
    bank = [
        L(10, "117000", "2026-09-01", "CCI FEES - INWARD BC 31.08.2026 11th SESSION NIP SETT", row=1),
        L(11, "-117000", "2026-09-01", "REVERSAL OF ENTRY 31.08.2026 11th SESSION NIP SETT", row=2),
        L(12, "117000", "2026-09-01", "SETTLEMENT TRANSACTIONS 31.08.2026 11th SESSION NIP SETT", row=3),
    ]
    gl = [T(1, "RIGHT", "117000", day="2026-08-31", at="19:30")]
    proposals = engine.match_session_bulk(r, bank, gl)
    reversal = next(p for p in proposals if p.kind == "REVERSAL")
    assert sorted(t.id for t in reversal.left) == [10, 11] and reversal.difference == 0 and reversal.unique
    settlement = next(p for p in proposals if p.kind == "MATCH")
    assert [t.id for t in settlement.left] == [12] and [t.id for t in settlement.right] == [1]


def test_custom_session_label_pattern_validation():
    with pytest.raises(ValidationError):
        rule(session_label_format="PATTERN", session_label_pattern="(?P<DD>\\d+)").clean()
    rule(
        session_label_format="PATTERN",
        session_label_pattern=r"S(?P<N>\d+) (?P<DD>\d{2})(?P<MO>\d{2})(?P<YY>\d{2})",
    ).clean()


def test_session_retried_after_neighbouring_item_moves_away():
    r = rule(session_cutoffs=NIBSS_CUTOFFS, session_boundary_minutes=15)
    gl = [
        T(1, "RIGHT", "48500", day="2026-09-11", at="18:39:03"),  # session 10
        T(2, "RIGHT", "432000", day="2026-09-11", at="19:00:07"),  # just after the 19:00 cut-off: settled in 10
        T(3, "RIGHT", "500", day="2026-09-11", at="19:55:35"),  # session 11
    ]
    bank = [T(10, "LEFT", "480500", day="2026-09-11"), T(11, "LEFT", "500", day="2026-09-11")]
    proposals = engine.match_session_bulk(r, bank, gl)
    assert {p.left[0].id: sorted(t.id for t in p.right) for p in proposals} == {10: [1, 2], 11: [3]}


def test_gl_reversal_paired_by_truncated_narration():
    from apps.imports.normalize import normalize_narration

    def G(id, flow, narration, day="2026-09-02"):
        return engine.Txn(id=id, side="RIGHT", flow=D(flow), date=d(day), norm_narration=normalize_narration(narration))

    original = G(1, "-50005.38", "Mob trans to ADA OBI @ GTB (0106590190)", day="2026-09-01")
    reversal = G(2, "50005.38", "Reversal of (Mob trans to ADA OBI @ GTB (01)")
    other = G(3, "-50005.38", "Mob trans to BOLA ADE @ UBA (2161810021)", day="2026-09-01")
    r = MatchingRule(
        strategy=Strategy.REVERSAL_PAIR,
        left_reference_field="transaction_id",
        right_reference_field="narration_contains",
        date_tolerance_days=7,
    )
    proposals = engine.match_reversal_pairs(r, [], [original, reversal, other])
    assert len(proposals) == 1 and sorted(t.id for t in proposals[0].right) == [1, 2] and proposals[0].unique


# ------------------------------------------------------------------ import details seen in real bank files


def test_reference_taken_from_narration():
    from apps.imports.normalize import reference_from_narration

    narr = "CHANNEL PAYMENT 9999021033 9999 POS@<3QTL002> <QuickTeller0002> <PRF,T,2273158762000037,Ola Gideon>"
    assert reference_from_narration(narr, r"PRF,T,\d{4}(\d{12})") == "158762000037"
    assert reference_from_narration(narr, r"PRF,T,\d{4}(?P<REF>\d{6})") == "158762"
    assert reference_from_narration("no ref", r"PRF,T,\d{4}(\d{12})") == ""
    profile = ImportProfile(
        column_mapping={"transaction_date": "Date", "debit_amount": "Debit", "narration": "Narration"},
        amount_mode="SEPARATE",
        date_formats=["%d/%m/%Y"],
        default_account_reference="1305716706",
        default_currency="NGN",
        narration_reference_pattern=r"PRF,T,\d{4}(\d{12})",
    )
    out, errors = normalize_row({"Date": "01/09/2026", "Debit": "60,000.00", "Narration": narr}, profile)
    assert not errors and out["norm_external_reference"] == "158762000037"


def test_xlsx_with_wrong_declared_dimension_is_read_in_full():
    import io
    import re
    import zipfile

    from apps.imports.parsers import read_table

    from .helpers import xlsx_bytes

    data = xlsx_bytes([["01/09/2026", "100.00"], ["02/09/2026", "200.00"]], ["Date", "Amount"])
    src, out = zipfile.ZipFile(io.BytesIO(data)), io.BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            content = src.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                content = re.sub(rb'<dimension ref="[^"]+"', b'<dimension ref="A1"', content)
            dst.writestr(item, content)
    headers, rows = read_table(out.getvalue(), ".xlsx")
    assert headers == ["Date", "Amount"] and len(rows) == 2
