"""
Reconciliation engine and workflow tests.

Sign conventions used throughout: the bank statement (left) is a customer
statement, so credits are inflows; the GL export (right) is the bank asset
account, so debits are inflows. ``txn(amount=...)`` takes the source's signed
amount (credit positive), hence GL inflows are entered as negative numbers.
"""

from decimal import Decimal

import pytest

from apps.core.exceptions import BusinessRuleError, SegregationOfDutiesError
from apps.reconciliation import engine, services
from apps.reconciliation.models import MatchGroup, MatchingRule, Reconciliation, Strategy

from .conftest import make_user
from .helpers import bank_profile, d, gl_profile, source_batch, txn

D = Decimal


# ------------------------------------------------------------------ pure engine


def T(id, side, flow, day="2026-10-01", **kw):
    return engine.Txn(id=id, side=side, flow=D(str(flow)), date=d(day), **kw)


def rule(strategy, **kw):
    defaults = dict(
        strategy=strategy,
        date_tolerance_days=0,
        amount_tolerance=D("0"),
        auto_accept=True,
        left_reference_field="transaction_id",
        right_reference_field="transaction_id",
        composite_fields=[],
        max_group_size=50,
        similarity_threshold=D("0.85"),
        name=strategy,
    )
    defaults.update(kw)
    return MatchingRule(**defaults)


def test_exact_reference_unique_and_ambiguous():
    left = [
        T(1, "L", 100, norm_transaction_id="A1"),
        T(2, "L", 50, norm_transaction_id="B2"),
        T(3, "L", 75, norm_transaction_id="C3"),
    ]
    right = [
        T(10, "R", 100, norm_transaction_id="A1"),
        T(11, "R", 50, norm_transaction_id="B2"),
        T(12, "R", 50, norm_transaction_id="B2"),
        T(13, "R", 70, norm_transaction_id="C3"),
    ]
    proposals = engine.match_reference(rule(Strategy.EXACT_REFERENCE), left, right)
    by_left = {p.left[0].id: p for p in proposals}
    assert by_left[1].unique and by_left[1].right[0].id == 10
    assert not by_left[2].unique  # two candidates
    assert 3 not in by_left  # amount differs and tolerance is zero


def test_amount_date_tolerance():
    left = [T(1, "L", 100, "2026-10-01")]
    right = [T(10, "R", 100, "2026-10-04"), T(11, "R", 100, "2026-10-09")]
    assert engine.match_amount_date(rule(Strategy.AMOUNT_DATE, date_tolerance_days=2), left, right) == []
    proposals = engine.match_amount_date(rule(Strategy.AMOUNT_DATE, date_tolerance_days=3), left, right)
    assert proposals[0].right[0].id == 10 and proposals[0].unique
    tol = engine.match_amount_date(
        rule(Strategy.AMOUNT_DATE, date_tolerance_days=10, amount_tolerance=D("1")), [T(1, "L", "100.50")], right
    )
    assert tol[0].difference == D("0.50") and not tol[0].unique


def test_one_to_many_and_many_to_one():
    left = [T(1, "L", 300, norm_transaction_id="BATCH9")]
    right = [T(10, "R", 100, norm_transaction_id="BATCH9"), T(11, "R", 200, norm_transaction_id="BATCH9")]
    p = engine.match_one_to_many(rule(Strategy.ONE_TO_MANY), left, right)
    assert len(p) == 1 and {t.id for t in p[0].right} == {10, 11} and p[0].difference == 0
    p2 = engine.match_many_to_one(rule(Strategy.MANY_TO_ONE), right, left)
    assert len(p2) == 1 and {t.id for t in p2[0].left} == {10, 11}


def test_fee_net_matching_and_inconsistency_flag():
    bank = [T(1, "L", "9850.00", norm_external_reference="SB-1")]
    records = [
        T(
            10,
            "R",
            "5000",
            settlement_batch_ref="SB-1",
            gross_amount=D("5000"),
            fee_amount=D("75"),
            net_amount=D("4925"),
        ),
        T(
            11,
            "R",
            "5000",
            settlement_batch_ref="SB-1",
            gross_amount=D("5000"),
            fee_amount=D("75"),
            net_amount=D("4925"),
        ),
    ]
    r = rule(Strategy.FEE_NET, left_reference_field="external_reference", right_reference_field="settlement_batch_ref")
    p = engine.match_fee_net(r, bank, records)
    assert len(p) == 1 and not p[0].flags
    # Flows carry the gross, so the 150 fee surfaces as a difference to review.
    assert p[0].difference == D("-150.00")
    bad = [
        T(
            12,
            "R",
            "4925",
            settlement_batch_ref="SB-2",
            gross_amount=D("5000"),
            fee_amount=D("70"),
            net_amount=D("4925"),
        )
    ]
    p_bad = engine.match_fee_net(r, [T(2, "L", "4925", norm_external_reference="SB-2")], bad)
    assert p_bad and p_bad[0].flags  # gross - fee != net


def test_reversal_pairs_same_side():
    left = [
        T(1, "L", 500, norm_transaction_id="X1"),
        T(2, "L", -500, "2026-10-02", norm_transaction_id="X1", is_reversal=True),
    ]
    p = engine.match_reversal_pairs(rule(Strategy.REVERSAL_PAIR, date_tolerance_days=3), left, [])
    assert len(p) == 1 and p[0].kind == "REVERSAL" and p[0].difference == 0


def test_fuzzy_narration_never_unique():
    left = [T(1, "L", 100, norm_narration="TRANSFER FROM JOHN OKAFOR")]
    right = [T(10, "R", 100, norm_narration="TRANSFER FRM JOHN OKAFOR")]
    p = engine.match_similar_narration(rule(Strategy.NARRATION_SIMILARITY), left, right)
    assert len(p) == 1 and not p[0].unique
    assert services.classify(rule(Strategy.NARRATION_SIMILARITY), p[0]) == MatchGroup.Category.MANUAL_REVIEW


def test_classification_rules():
    exact = engine.Proposal([T(1, "L", 10)], [T(2, "R", 10)], unique=True, reason="")
    assert services.classify(rule(Strategy.EXACT_REFERENCE), exact) == MatchGroup.Category.EXACT
    assert (
        services.classify(rule(Strategy.EXACT_REFERENCE, auto_accept=False), exact)
        == MatchGroup.Category.HIGH_CONFIDENCE
    )
    assert services.classify(rule(Strategy.AMOUNT_DATE), exact) == MatchGroup.Category.HIGH_CONFIDENCE  # never auto
    diff = engine.Proposal([T(1, "L", 10)], [T(2, "R", 9)], unique=True, reason="")
    assert services.classify(rule(Strategy.EXACT_REFERENCE), diff) == MatchGroup.Category.MANUAL_REVIEW
    dup = engine.Proposal([T(1, "L", 10, suspected_duplicate=True)], [T(2, "R", 10)], unique=True, reason="")
    assert services.classify(rule(Strategy.EXACT_REFERENCE), dup) == MatchGroup.Category.MANUAL_REVIEW


# ------------------------------------------------------------------ full workflow


@pytest.fixture
def setup(institution, users):
    user = users["recon"]
    bank = source_batch(institution, user, bank_profile(institution, user))
    gl = source_batch(institution, user, gl_profile(institution, user, default_account_reference="1201"))
    MatchingRule.objects.create(
        institution=institution,
        name="Exact ref",
        strategy=Strategy.EXACT_REFERENCE,
        priority=10,
        auto_accept=True,
        date_tolerance_days=7,
    )
    MatchingRule.objects.create(
        institution=institution, name="Amount/date", strategy=Strategy.AMOUNT_DATE, priority=20, date_tolerance_days=3
    )
    return {"bank": bank, "gl": gl, "user": user}


def make_recon(institution, user, **kw):
    defaults = dict(
        name="Zenith current account – Oct",
        left_account_reference="1201",
        right_account_reference="1201",
        period_start=d("2026-10-01"),
        period_end=d("2026-10-31"),
        opening_left_balance=D("1000000.00"),
        opening_right_balance=D("1000000.00"),
    )
    defaults.update(kw)
    return services.create_reconciliation(institution, user, **defaults)


def load_scenario(setup):
    bank, gl = setup["bank"], setup["gl"]
    # Matched by reference
    txn(bank, day="2026-10-02", amount="250000.00", ref="FT001", narration="Inward transfer Okafor")
    txn(gl, day="2026-10-02", amount="-250000.00", ref="FT001", narration="Inward transfer")
    txn(bank, day="2026-10-05", amount="-120000.00", ref="CHQ1001", narration="Cheque 1001")
    txn(gl, day="2026-10-04", amount="120000.00", ref="CHQ1001", narration="Cheque 1001 – supplier")
    # Amount/date candidate only (different references) → high confidence, needs review
    txn(bank, day="2026-10-10", amount="75000.00", ref="NIPX99", narration="NIP inflow")
    txn(gl, day="2026-10-11", amount="-75000.00", ref="RCPT-55", narration="Receipt")
    # Bank only: charge and unidentified credit
    txn(bank, day="2026-10-31", amount="-1075.00", ref="SMSCHG", narration="SMS alert charges")
    txn(bank, day="2026-10-20", amount="30000.00", ref="UNK1", narration="Unknown credit")
    # GL only: outstanding cheque and deposit in transit
    txn(gl, day="2026-10-30", amount="45000.00", ref="CHQ1002", narration="Cheque 1002 not yet presented")
    txn(gl, day="2026-10-31", amount="-60000.00", ref="DEP77", narration="Deposit in transit")


def test_full_reconciliation_statement(institution, users, setup):
    load_scenario(setup)
    user = setup["user"]
    # Bank closing: 1,000,000 + 250,000 - 120,000 + 75,000 - 1,075 + 30,000 = 1,233,925
    # GL closing:   1,000,000 + 250,000 - 120,000 + 75,000 - 45,000 + 60,000 = 1,220,000
    recon = make_recon(institution, user, closing_left_balance=D("1233925.00"), closing_right_balance=D("1220000.00"))
    summary = services.run_reconciliation(recon, user)
    assert summary["auto_accepted"] == 2
    assert summary["high_confidence"] == 1
    recon.refresh_from_db()
    assert recon.status == Reconciliation.Status.IN_REVIEW

    stmt = services.build_statement(recon)
    assert stmt.left.computed_closing == D("1233925.00") and stmt.left.balance_check_difference == 0
    assert stmt.right.computed_closing == D("1220000.00") and stmt.right.balance_check_difference == 0
    assert stmt.pending_review == 1

    # Accept the high-confidence candidate after review.
    group = recon.match_groups.get(status=MatchGroup.Status.PROPOSED)
    assert group.category == MatchGroup.Category.HIGH_CONFIDENCE and group.rule_snapshot["name"] == "Amount/date"
    services.accept_match(group, user, "Confirmed receipt RCPT-55 is the NIP inflow")

    stmt = services.build_statement(recon)
    assert stmt.total_unrecorded_credits == D("30000.00")
    assert stmt.total_unrecorded_charges == D("1075.00")
    assert stmt.total_outstanding_deposits == D("60000.00")
    assert stmt.total_outstanding_payments == D("45000.00")
    # Adjusted bank = 1,233,925 + 60,000 - 45,000 = 1,248,925
    # Adjusted book = 1,220,000 + 30,000 - 1,075 = 1,248,925
    assert stmt.adjusted_left_balance == stmt.adjusted_right_balance == D("1248925.00")
    assert stmt.final_difference == 0
    assert stmt.validation_errors(institution.reconciliation_tolerance) == []

    services.submit_reconciliation(recon, user)
    services.approve_reconciliation(recon, users["reviewer"])
    recon.refresh_from_db()
    assert recon.status == Reconciliation.Status.APPROVED and recon.approved_by == users["reviewer"]
    # Approved reconciliations are locked.
    with pytest.raises(BusinessRuleError):
        services.run_reconciliation(recon, user)


def test_preparer_cannot_approve_own_reconciliation(institution, users, setup):
    preparer = make_user("dual", institution, "recon_officer", "recon_reviewer")
    recon = make_recon(institution, preparer)
    services.run_reconciliation(recon, preparer)
    services.submit_reconciliation(recon, preparer)
    with pytest.raises(SegregationOfDutiesError):
        services.approve_reconciliation(recon, preparer)
    services.approve_reconciliation(recon, users["reviewer"])


def test_approval_blocked_by_unexplained_difference(institution, users, setup):
    bank, gl = setup["bank"], setup["gl"]
    txn(bank, day="2026-10-02", amount="100.00", ref="A")
    txn(gl, day="2026-10-02", amount="-100.00", ref="A")
    # Openings disagree by 500 with no reconciling items -> unexplained difference.
    recon = make_recon(
        institution, setup["user"], opening_left_balance=D("1500.00"), opening_right_balance=D("1000.00")
    )
    services.run_reconciliation(recon, setup["user"])
    stmt = services.build_statement(recon)
    assert stmt.final_difference == D("500.00")
    with pytest.raises(BusinessRuleError, match="Unexplained difference"):
        services.submit_reconciliation(recon, setup["user"])
    # An adjustment with a supporting reference explains it.
    services.add_adjustment(
        recon,
        setup["user"],
        side="RIGHT",
        description="Unrecorded interest per bank advice",
        amount=D("500.00"),
        reference="ADV-2026-10",
    )
    assert services.build_statement(recon).final_difference == 0
    services.submit_reconciliation(recon, setup["user"])


def test_tolerance_allows_small_differences(institution, users, setup):
    institution.reconciliation_tolerance = D("1.00")
    institution.save()
    recon = make_recon(institution, setup["user"], opening_left_balance=D("1000000.50"))
    services.run_reconciliation(recon, setup["user"])
    assert services.build_statement(recon).final_difference == D("0.50")
    services.submit_reconciliation(recon, setup["user"])


def test_balance_check_detects_incomplete_data(institution, users, setup):
    txn(setup["bank"], day="2026-10-02", amount="100.00", ref="A")
    recon = make_recon(institution, setup["user"], closing_left_balance=D("1000200.00"))
    services.run_reconciliation(recon, setup["user"])
    errors = services.build_statement(recon).validation_errors(D("0"))
    assert any("incomplete" in e for e in errors)


def test_pending_review_blocks_submission(institution, users, setup):
    txn(setup["bank"], day="2026-10-10", amount="75000.00", ref="X")
    txn(setup["gl"], day="2026-10-11", amount="-75000.00", ref="Y")
    recon = make_recon(institution, setup["user"])
    services.run_reconciliation(recon, setup["user"])
    with pytest.raises(BusinessRuleError, match="await review"):
        services.submit_reconciliation(recon, setup["user"])


def test_rejected_proposal_is_not_reproposed(institution, users, setup):
    txn(setup["bank"], day="2026-10-10", amount="75000.00", ref="X")
    txn(setup["gl"], day="2026-10-11", amount="-75000.00", ref="Y")
    recon = make_recon(institution, setup["user"])
    services.run_reconciliation(recon, setup["user"])
    group = recon.match_groups.get(status=MatchGroup.Status.PROPOSED)
    services.reject_match(group, setup["user"], "Different customers")
    summary = services.run_reconciliation(recon, setup["user"])
    assert summary["high_confidence"] == 0 and summary["unmatched_left"] == 1


def test_unmatch_requires_note_and_releases_items(institution, users, setup):
    txn(setup["bank"], day="2026-10-02", amount="100.00", ref="A")
    txn(setup["gl"], day="2026-10-02", amount="-100.00", ref="A")
    recon = make_recon(institution, setup["user"])
    services.run_reconciliation(recon, setup["user"])
    group = recon.match_groups.get(status=MatchGroup.Status.ACCEPTED)
    with pytest.raises(BusinessRuleError, match="note"):
        services.reject_match(group, setup["user"], "")
    services.reject_match(group, setup["user"], "Matched to wrong entry")
    stmt = services.build_statement(recon)
    assert len(stmt.unrecorded_credits) == 1 and len(stmt.outstanding_deposits) == 1


def test_manual_match_exclusion_and_scope(institution, users, setup):
    a = txn(setup["bank"], day="2026-10-02", amount="300.00", ref="SPLIT")
    b = txn(setup["gl"], day="2026-10-02", amount="-100.00", ref="P1")
    c = txn(setup["gl"], day="2026-10-03", amount="-200.00", ref="P2")
    e = txn(setup["bank"], day="2026-10-04", amount="-15.00", ref="ERR")
    other = txn(setup["bank"], day="2026-10-04", amount="5.00", ref="OTHERACCT", account="9999")
    spare = txn(setup["gl"], day="2026-10-05", amount="-5.00", ref="SPARE")
    recon = make_recon(institution, setup["user"])
    services.run_reconciliation(recon, setup["user"])
    with pytest.raises(BusinessRuleError, match="note"):
        services.manual_match(recon, setup["user"], [a.pk], [b.pk, c.pk], "")
    group = services.manual_match(recon, setup["user"], [a.pk], [b.pk, c.pk], "Split settlement confirmed")
    assert group.difference == 0 and group.category == MatchGroup.Category.MANUAL
    with pytest.raises(BusinessRuleError, match="already matched"):
        services.manual_match(recon, setup["user"], [a.pk], [b.pk], "again")
    with pytest.raises(BusinessRuleError, match="not in scope"):
        services.manual_match(recon, setup["user"], [other.pk], [spare.pk], "wrong account")
    services.exclude_transaction(recon, setup["user"], e.pk, "Bank error; bank to reverse (ticket 4471)")
    stmt = services.build_statement(recon)
    assert stmt.total_excluded_left == D("-15.00") and not stmt.unrecorded_charges
    assert [t.pk for t, _ in stmt.outstanding_deposits] == [spare.pk]
    # Bank: 1,000,000 + 300 - 15 = 1,000,285; GL: 1,000,000 + 300 + 5 = 1,000,305
    # Adjusted bank = 1,000,285 + 5 = 1,000,290; adjusted book = 1,000,305 - 15 = 1,000,290
    assert stmt.final_difference == 0


def test_matched_transaction_cannot_be_reused_in_another_reconciliation(institution, users, setup):
    txn(setup["bank"], day="2026-10-02", amount="100.00", ref="A")
    txn(setup["gl"], day="2026-10-02", amount="-100.00", ref="A")
    first = make_recon(institution, setup["user"])
    services.run_reconciliation(first, setup["user"])
    second = make_recon(institution, setup["user"], name="Duplicate scope")
    summary = services.run_reconciliation(second, setup["user"])
    assert summary["auto_accepted"] == 0 and summary["unmatched_left"] == 0


def test_reopen_requires_permission_and_reason(institution, users, setup):
    recon = make_recon(institution, setup["user"])
    services.run_reconciliation(recon, setup["user"])
    services.submit_reconciliation(recon, setup["user"])
    services.approve_reconciliation(recon, users["reviewer"])
    with pytest.raises(BusinessRuleError):
        services.reopen_reconciliation(recon, users["cfo"], "")
    from apps.core.exceptions import PermissionDeniedError

    with pytest.raises(PermissionDeniedError):
        services.reopen_reconciliation(recon, setup["user"], "Late bank advice")
    services.reopen_reconciliation(recon, users["cfo"], "Late bank advice")
    recon.refresh_from_db()
    assert recon.status == Reconciliation.Status.REOPENED and recon.approved_by is None
    services.run_reconciliation(recon, setup["user"])


def test_carry_forward_outstanding_items(institution, users, setup):
    # October: cheque issued (GL outflow) but not yet presented at the bank.
    txn(setup["gl"], day="2026-10-30", amount="45000.00", ref="CHQ1002")
    october = make_recon(
        institution,
        setup["user"],
        name="Oct",
        opening_left_balance=D("100000.00"),
        opening_right_balance=D("100000.00"),
    )
    services.run_reconciliation(october, setup["user"])
    stmt = services.build_statement(october)
    assert stmt.right.computed_closing == D("55000.00") and stmt.total_outstanding_payments == D("45000.00")
    assert stmt.final_difference == 0
    services.submit_reconciliation(october, setup["user"])
    services.approve_reconciliation(october, users["reviewer"])

    # November: the cheque clears the bank and matches the October GL item carried forward.
    txn(setup["bank"], day="2026-11-03", amount="-45000.00", ref="CHQ1002")
    november = make_recon(
        institution,
        setup["user"],
        name="Nov",
        period_start=d("2026-11-01"),
        period_end=d("2026-11-30"),
        opening_left_balance=stmt.left.closing,
        opening_right_balance=stmt.right.closing,
    )
    summary = services.run_reconciliation(november, setup["user"])
    assert summary["auto_accepted"] == 1
    nov = services.build_statement(november)
    assert nov.left.computed_closing == nov.right.computed_closing == D("55000.00")
    assert nov.right.outflows == 0  # the October item is not a November movement
    assert nov.final_difference == 0 and not nov.outstanding_payments


def test_settlement_fee_net_end_to_end(institution, users):
    from apps.imports.models import Channel, SourceType
    from apps.settlements.services import install_default_rules, settlement_summary

    user = users["recon"]
    bank = source_batch(
        institution, user, bank_profile(institution, user, name="Settlement acct", default_account_reference="1502")
    )
    report = source_batch(
        institution,
        user,
        bank_profile(
            institution,
            user,
            name="POS report",
            source_type=SourceType.SETTLEMENT_REPORT,
            source_system="Processor",
            default_account_reference="1502",
        ),
    )
    # Settlement report rows: amount mapped as the net settlement (recommended).
    for i in range(3):
        txn(
            report,
            day="2026-10-05",
            amount="9850.00",
            ref=f"POS{i}",
            settlement_batch_ref="SB20261005",
            gross_amount=D("10000.00"),
            fee_amount=D("150.00"),
            net_amount=D("9850.00"),
            channel=Channel.POS,
            settlement_date=d("2026-10-06"),
        )
    txn(bank, day="2026-10-06", amount="29550.00", ext="SB20261005", narration="POS SETTLEMENT SB20261005")
    install_default_rules(institution, users["admin"])
    recon = services.create_reconciliation(
        institution,
        user,
        name="POS settlement Oct",
        recon_type="SETTLEMENT",
        left_account_reference="1502",
        right_account_reference="1502",
        right_source_type=SourceType.SETTLEMENT_REPORT,
        right_inflow_side="CREDIT",
        period_start=d("2026-10-01"),
        period_end=d("2026-10-31"),
    )
    summary = services.run_reconciliation(recon, user)
    assert summary["auto_accepted"] == 1 and summary["unmatched_left"] == 0 and summary["unmatched_right"] == 0
    batches = settlement_summary(institution)
    assert len(batches) == 1
    b = batches[0]
    assert b.records == 3 and b.gross == D("30000.00") and b.fees == D("450.00") and b.net_computed == D("29550.00")
    assert b.fully_matched and not b.exceptions


def test_cross_institution_rule_rejected(institution, other_institution, users):
    foreign_rule = MatchingRule.objects.create(
        institution=other_institution, name="x", strategy=Strategy.EXACT_REFERENCE
    )
    with pytest.raises(BusinessRuleError, match="different institution"):
        make_recon(institution, users["recon"], rules=[foreign_rule])
