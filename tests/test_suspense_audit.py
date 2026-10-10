from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounting import services as acc
from apps.audit import services as audit
from apps.audit.models import CorrectiveAction, Engagement, Finding, Workpaper
from apps.core.exceptions import BusinessRuleError, SegregationOfDutiesError
from apps.notifications.models import Notification
from apps.suspense import services
from apps.suspense.models import EscalationRule, SuspenseItem, bucket_index, bucket_labels

D = Decimal
pytestmark = pytest.mark.django_db


def make_item(institution, user, coa, days_old=0, amount="5000.00", side="CREDIT", **kw):
    return services.create_item(
        institution,
        user,
        account=coa["2390"],
        original_date=timezone.localdate() - timedelta(days=days_old),
        original_amount=D(amount),
        side=side,
        reason=kw.pop("reason", "Unidentified inward transfer"),
        **kw,
    )


def test_bucket_math():
    bounds = [7, 30, 60, 90]
    assert bucket_labels(bounds) == ["0–7 days", "8–30 days", "31–60 days", "61–90 days", "Over 90 days"]
    assert [bucket_index(a, bounds) for a in (0, 7, 8, 30, 31, 90, 91, 400)] == [0, 0, 1, 1, 2, 3, 4, 4]


def test_aging_report_with_configurable_buckets(institution, users, coa):
    u = users["accountant"]
    for days, amt in [(1, "100"), (10, "200"), (45, "300"), (75, "400"), (120, "500"), (200, "600")]:
        make_item(institution, u, coa, days_old=days, amount=amt)
    report = services.aging_report(institution)
    assert report.bucket_totals == [D("100"), D("200"), D("300"), D("400"), D("1100")]
    assert report.total == D("2100") and report.count == 6
    services.set_aging_bounds(institution, users["admin"], [30, 180])
    report = services.aging_report(institution)
    assert report.labels == ["0–30 days", "31–180 days", "Over 180 days"]
    assert report.bucket_totals == [D("300"), D("1200"), D("600")]


def test_suspense_requires_suspense_account(institution, users, coa):
    with pytest.raises(BusinessRuleError, match="not designated"):
        services.create_item(
            institution,
            users["accountant"],
            account=coa["5601"],
            original_date=date.today(),
            original_amount=D("1"),
            side="DEBIT",
            reason="x",
        )


def test_resolution_workflow_with_clearing_journal(institution, users, coa, periods):
    item = make_item(institution, users["accountant"], coa)
    services.assign(item, users["accountant"], users["recon"])
    item.refresh_from_db()
    assert item.status == SuspenseItem.Status.INVESTIGATING
    assert Notification.objects.filter(user=users["recon"]).exists()
    journal = services.prepare_clearing_journal(item, users["recon"], contra_account=coa["2101"])
    item.refresh_from_db()
    assert item.related_journal == journal and journal.status == "DRAFT"
    lines = {ln.account.code: (ln.debit, ln.credit) for ln in journal.lines.select_related("account")}
    assert lines["2390"] == (D("5000.00"), D("0")) and lines["2101"] == (D("0"), D("5000.00"))
    services.propose_resolution(item, users["recon"], "CLEARED", "Beneficiary identified as customer 0012345")
    # Approval needs an independent approver and a posted journal.
    with pytest.raises(BusinessRuleError, match="posted"):
        services.approve_resolution(item, users["reviewer"])
    acc.submit_journal(journal, users["recon"])
    acc.approve_journal(journal, users["manager"])
    acc.post_journal(journal, users["manager"])
    services.approve_resolution(item, users["reviewer"], "Evidence reviewed")
    item.refresh_from_db()
    assert item.status == SuspenseItem.Status.RESOLVED and item.date_resolved == timezone.localdate()
    assert services.aging_report(institution).count == 0


def test_self_approval_of_resolution_blocked(institution, users, coa):
    from .conftest import make_user

    dual = make_user("dual", institution, "recon_officer", "recon_reviewer")
    item = make_item(institution, dual, coa)
    services.propose_resolution(item, dual, "REFUNDED", "Returned to remitting bank")
    with pytest.raises(SegregationOfDutiesError):
        services.approve_resolution(item, dual)
    services.reject_resolution(item, users["reviewer"], "Provide the refund confirmation")
    item.refresh_from_db()
    assert item.status == SuspenseItem.Status.INVESTIGATING


def test_escalations(institution, users, coa):
    EscalationRule.objects.create(institution=institution, name="30+", min_age_days=30, repeat_every_days=7)
    EscalationRule.objects.create(
        institution=institution, name="90+", min_age_days=90, notify_permission="report.sensitive"
    )
    old = make_item(institution, users["accountant"], coa, days_old=95, assigned_to=users["recon"])
    make_item(institution, users["accountant"], coa, days_old=5)
    sent = services.run_escalations(institution)
    assert sent == 1
    old.refresh_from_db()
    assert old.escalation_level == 2
    assert Notification.objects.filter(user=users["cfo"], title__contains=old.reference).exists()
    assert Notification.objects.filter(user=users["recon"], title__contains=old.reference).exists()
    assert services.run_escalations(institution) == 0  # not repeated until due
    assert services.run_escalations(institution, today=timezone.localdate() + timedelta(days=7)) == 1


def test_create_from_unmatched_transaction(institution, users, coa):
    from apps.reconciliation.services import create_reconciliation

    from .helpers import bank_profile, d, source_batch, txn

    batch = source_batch(institution, users["recon"], bank_profile(institution, users["recon"]))
    t = txn(batch, day="2026-10-20", amount="30000.00", ref="UNK1", narration="Unknown credit")
    recon = create_reconciliation(
        institution,
        users["recon"],
        name="r",
        left_account_reference="1201",
        right_account_reference="1201",
        period_start=d("2026-10-01"),
        period_end=d("2026-10-31"),
    )
    item = services.create_from_transaction(recon, t, users["recon"], account=coa["2390"], reason="Unidentified credit")
    assert item.original_amount == D("30000.00") and item.side == "CREDIT" and item.reconciliation == recon
    with pytest.raises(BusinessRuleError, match="already exists"):
        services.create_from_transaction(recon, t, users["recon"], account=coa["2390"], reason="again")


# ------------------------------------------------------------------ audit


def test_audit_engagement_lifecycle(institution, users):
    from .conftest import make_user

    auditor, head = users["auditor"], make_user("head", institution, "internal_auditor")
    eng = audit.create_engagement(
        institution,
        auditor,
        title="Q3 reconciliations review",
        scope="Bank recs",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 9, 30),
        lead=head,
        team=[auditor],
    )
    assert eng.reference.startswith("AUD-")
    wp = audit.create_workpaper(eng, auditor, reference="WP-1", title="Test approvals")
    with pytest.raises(BusinessRuleError, match="conclusion"):
        audit.submit_workpaper(wp, auditor)
    wp.conclusion = "Approvals evidenced for 25/25 samples"
    wp.save()
    audit.submit_workpaper(wp, auditor)
    with pytest.raises(SegregationOfDutiesError):
        audit.review_workpaper(wp, auditor, approve=True)
    audit.review_workpaper(wp, head, approve=True)
    assert Workpaper.objects.get(pk=wp.pk).status == Workpaper.Status.REVIEWED

    finding = audit.raise_finding(
        eng,
        auditor,
        title="Late reconciliations",
        rating="HIGH",
        condition="3 accounts unreconciled > 60 days",
        recommendation="Reconcile monthly",
        owner=users["manager"],
        workpaper=wp,
    )
    audit.respond_to_finding(finding, users["manager"], "Agreed; dedicated officer assigned")
    action = audit.add_corrective_action(
        finding, users["manager"], description="Assign officer", owner=users["manager"], due_date=date(2026, 11, 30)
    )
    with pytest.raises(BusinessRuleError, match="verified"):
        audit.close_finding(finding, head)
    audit.complete_action(action, users["manager"], "Officer assigned 1 Nov")
    finding.refresh_from_db()
    assert finding.status == Finding.Status.PENDING_VERIFICATION
    audit.verify_action(CorrectiveAction.objects.get(pk=action.pk), head)
    audit.close_finding(finding, head)
    for _ in range(3):
        audit.advance_engagement(eng, head)
    assert Engagement.objects.get(pk=eng.pk).status == Engagement.Status.CLOSED


def test_external_auditor_is_read_only(institution):
    from apps.core.exceptions import PermissionDeniedError

    from .conftest import make_user

    ext = make_user("kpmg", institution, "external_auditor")
    with pytest.raises(PermissionDeniedError):
        audit.create_engagement(
            institution,
            ext,
            title="x",
            scope="x",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            lead=ext,
        )
