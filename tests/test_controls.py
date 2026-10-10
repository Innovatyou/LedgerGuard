"""Branch scoping, configuration maker-checker, password policy, recurring journals and security headers."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounting import recurring, reports, services
from apps.accounting.models import Account, Journal, RecurringJournal
from apps.approvals import changes
from apps.approvals.models import ChangeRequest
from apps.core.exceptions import BusinessRuleError, PermissionDeniedError, SegregationOfDutiesError
from apps.organizations.models import Branch
from apps.reconciliation.models import MatchingRule

from .conftest import lines, make_user

pytestmark = pytest.mark.django_db
D = Decimal


# ------------------------------------------------------------------ branch scope


@pytest.fixture
def branches(institution):
    return (
        Branch.objects.create(institution=institution, code="001", name="Lagos"),
        Branch.objects.create(institution=institution, code="002", name="Abuja"),
    )


def _post_branch_journal(institution, users, coa, branch, amount="100.00"):
    journal = services.create_journal(
        institution,
        users["accountant"],
        transaction_date=timezone.localdate(),
        description=f"{branch.name} rent",
        lines=lines((coa["5601"], "D", amount), (coa["2301"], "C", amount)),
        branch=branch,
    )
    services.submit_journal(journal, users["accountant"])
    services.approve_journal(journal, users["manager"])
    return services.post_journal(journal, users["manager"])


def test_branch_scoped_reports_and_journals(client, institution, users, coa, periods, branches):
    lagos, abuja = branches
    _post_branch_journal(institution, users, coa, lagos, "100.00")
    _post_branch_journal(institution, users, coa, abuja, "250.00")
    today = timezone.localdate()
    assert reports.trial_balance(institution, today).total_debit == D("350.00")
    assert reports.trial_balance(institution, today, branches={lagos.pk}).total_debit == D("100.00")

    manager = make_user("bm", institution, "branch_manager", "finance_manager", branch=lagos)
    client.force_login(manager)
    html = client.get(reverse("accounting:journals")).content.decode()
    assert "Lagos rent" in html and "Abuja rent" not in html
    abuja_journal = Journal.objects.get(description="Abuja rent")
    assert client.get(reverse("accounting:journal-detail", args=[abuja_journal.pk])).status_code == 404
    tb = client.get(reverse("reporting:trial-balance")).context["tb"]
    assert tb.total_debit == D("100.00")

    api = APIClient()
    api.force_authenticate(manager)
    assert api.get("/api/v1/reports/trial-balance/").json()["total_debit"] == "100.00"
    assert all(j["branch"] == lagos.pk for j in api.get("/api/v1/journals/").json()["results"])


def test_branch_scoped_user_cannot_prepare_or_approve_other_branch(institution, users, coa, periods, branches):
    lagos, abuja = branches
    scoped = make_user("lagosacct", institution, "accountant", branch=lagos)
    with pytest.raises(PermissionDeniedError):
        services.create_journal(
            institution,
            scoped,
            transaction_date=timezone.localdate(),
            description="x",
            lines=lines((coa["5601"], "D", "1"), (coa["2301"], "C", "1")),
            branch=abuja,
        )
    with pytest.raises(PermissionDeniedError):  # no branch at all = head-office journal
        services.create_journal(
            institution,
            scoped,
            transaction_date=timezone.localdate(),
            description="x",
            lines=lines((coa["5601"], "D", "1"), (coa["2301"], "C", "1")),
        )
    journal = services.create_journal(
        institution,
        users["accountant"],
        transaction_date=timezone.localdate(),
        description="abuja",
        branch=abuja,
        lines=lines((coa["5601"], "D", "1"), (coa["2301"], "C", "1")),
    )
    services.submit_journal(journal, users["accountant"])
    scoped_approver = make_user("lagosfm", institution, "finance_manager", branch=lagos)
    with pytest.raises(PermissionDeniedError):
        services.approve_journal(journal, scoped_approver)


# ------------------------------------------------------------------ configuration maker-checker


def test_account_change_requires_independent_approval(institution, users, coa):
    account = coa["5601"]
    cr = changes.request_change(
        institution,
        users["manager"],
        "account",
        {"name": "Rent and premises costs", "code": account.code},
        target_obj=account,
    )
    assert cr.payload == {"name": "Rent and premises costs"} and cr.before == {"name": "Rent and premises"}
    account.refresh_from_db()
    assert account.name == "Rent and premises"  # not applied yet
    with pytest.raises(SegregationOfDutiesError):
        changes.approve_change(cr, users["manager"])
    with pytest.raises(PermissionDeniedError):
        changes.approve_change(cr, users["accountant"])
    changes.approve_change(cr, users["cfo"], "OK")
    account.refresh_from_db()
    assert account.name == "Rent and premises costs" and account.approved_by == users["cfo"]
    assert ChangeRequest.objects.get(pk=cr.pk).status == ChangeRequest.Status.APPROVED


def test_new_account_and_invalid_change_rejected_early(institution, users, coa):
    with pytest.raises(BusinessRuleError):  # duplicate code caught before approval
        changes.request_change(
            institution,
            users["manager"],
            "account",
            {
                "code": "5601",
                "name": "Dup",
                "category": "EXPENSE",
                "account_type": "POSTING",
                "subcategory": "OTHER",
                "normal_balance": "DEBIT",
            },
        )
    cr = changes.request_change(
        institution,
        users["manager"],
        "account",
        {
            "code": "5699",
            "name": "Generator diesel",
            "category": "EXPENSE",
            "subcategory": "OPERATING_EXPENSE",
            "account_type": "POSTING",
            "normal_balance": "DEBIT",
            "parent": coa["5000"],
        },
    )
    obj = changes.approve_change(cr, users["cfo"])
    assert obj.code == "5699" and obj.parent == coa["5000"] and obj.created_by == users["manager"]


def test_rule_and_settings_changes_via_ui(client, institution, users):
    client.force_login(users["admin"])
    resp = client.post(
        reverse("reconciliation:rule-create"),
        {
            "name": "Exact",
            "strategy": "EXACT_REFERENCE",
            "priority": 10,
            "is_active": "on",
            "recon_type": "",
            "date_tolerance_days": 2,
            "amount_tolerance": "0.00",
            "auto_accept": "on",
            "left_reference_field": "transaction_id",
            "right_reference_field": "transaction_id",
            "max_group_size": 50,
            "similarity_threshold": "0.85",
        },
    )
    assert resp.status_code == 302 and resp.url == reverse("approvals:changes")
    assert not MatchingRule.objects.exists()
    cr = ChangeRequest.objects.get(target_type="matching_rule")
    client.force_login(users["reviewer"])
    client.post(reverse("approvals:changes"), {"change": cr.pk, "action": "approve"})
    assert MatchingRule.objects.get(institution=institution).name == "Exact"

    client.force_login(users["admin"])
    client.post(
        reverse("organizations:settings"),
        {
            "name": institution.name,
            "base_currency": "NGN",
            "amount_decimal_places": 2,
            "rounding_mode": "HALF_UP",
            "financial_year_start_month": 1,
            "backdate_limit_days": 5,
            "reconciliation_tolerance": "1000.00",
            "require_independent_approval": "",
            "require_config_approval": "on",
            "document_retention_years": 7,
        },
    )
    institution.refresh_from_db()
    assert institution.require_independent_approval is True  # weakening controls needs approval
    cr = ChangeRequest.objects.get(target_type="institution_settings")
    assert cr.payload == {"reconciliation_tolerance": "1000.00", "require_independent_approval": False}
    changes.reject_change(cr, users["cfo"], "Tolerance too high")
    institution.refresh_from_db()
    assert institution.reconciliation_tolerance == D("0.00")


# ------------------------------------------------------------------ passwords


def test_admin_password_reset_forces_change(client, institution, users):
    client.force_login(users["admin"])
    membership = users["accountant"].memberships.first()
    resp = client.post(
        reverse("accounts:membership-edit", args=[membership.pk]), {"action": "reset_password"}, follow=True
    )
    message = [str(m) for m in resp.context["messages"]][0]
    temporary = message.split(": ")[1].split(" ")[0]
    client.logout()
    client.post(reverse("accounts:login"), {"username": "ada", "password": temporary})
    assert client.get(reverse("reporting:dashboard")).url == reverse("accounts:password-change")
    resp = client.post(
        reverse("accounts:password-change"),
        {
            "old_password": temporary,
            "new_password1": "N3w-Secure-Passphrase!",
            "new_password2": "N3w-Secure-Passphrase!",
        },
    )
    assert resp.status_code == 302
    assert client.get(reverse("reporting:dashboard")).status_code == 200


# ------------------------------------------------------------------ recurring journals


def test_recurring_journal_lifecycle(institution, users, coa, periods):
    today = timezone.localdate()
    start = today.replace(day=1) - timedelta(days=40)
    template = recurring.create_template(
        institution,
        users["manager"],
        name="Rent accrual",
        description="Monthly rent accrual",
        lines=lines((coa["5601"], "D", "1250000.00"), (coa["2301"], "C", "1250000.00")),
        start_date=start,
        day_of_month=31,
    )
    assert template.status == RecurringJournal.Status.PENDING
    assert recurring.generate_due(institution, today) == []  # not active until approved
    with pytest.raises(SegregationOfDutiesError):
        recurring.approve_template(template, users["manager"])
    recurring.approve_template(template, users["cfo"])
    generated = recurring.generate_due(institution, today)
    assert len(generated) >= 1
    assert all(j.status == Journal.Status.SUBMITTED and j.journal_type == "RECURRING" for j in generated)
    assert generated[0].accounting_date.day == min(31, generated[0].accounting_date.day)
    # Idempotent: running again creates nothing new.
    assert recurring.generate_due(institution, today) == []
    template.refresh_from_db()
    assert template.next_run_date > today


def test_recurring_unbalanced_template_rejected(institution, users, coa, periods):
    with pytest.raises(BusinessRuleError, match="not balanced"):
        recurring.create_template(
            institution,
            users["manager"],
            name="Bad",
            description="Bad",
            lines=lines((coa["5601"], "D", "10"), (coa["2301"], "C", "9")),
            start_date=date.today(),
        )
    assert not RecurringJournal.objects.exists()


def test_month_end_clamping():
    assert recurring.add_months(date(2026, 1, 31), 1, 31) == date(2026, 2, 28)
    assert recurring.add_months(date(2026, 11, 30), 3, 31) == date(2027, 2, 28)
    assert recurring.first_run(date(2026, 3, 15), 10) == date(2026, 4, 10)


# ------------------------------------------------------------------ headers


def test_security_headers(client, users):
    client.force_login(users["accountant"])
    resp = client.get(reverse("reporting:dashboard"))
    assert "script-src 'self'" in resp["Content-Security-Policy"]
    assert resp["X-Frame-Options"] == "DENY"
    assert "<script>" not in resp.content.decode()
    assert client.get(reverse("accounts:login"))["Cache-Control"] == "no-store"


def test_account_ui_creates_change_request(client, institution, users, coa):
    client.force_login(users["manager"])
    resp = client.post(
        reverse("accounting:account-edit", args=[coa["5601"].pk]),
        {
            "code": "5601",
            "name": "Renamed",
            "category": "EXPENSE",
            "subcategory": "OPERATING_EXPENSE",
            "account_type": "POSTING",
            "normal_balance": "DEBIT",
            "is_active": "on",
            "parent": coa["5000"].pk,
        },
    )
    assert resp.status_code == 302
    assert Account.objects.get(pk=coa["5601"].pk).name == "Rent and premises"
    assert ChangeRequest.objects.filter(target_type="account", status="PENDING").count() == 1


def test_api_configuration_changes_need_approval(institution, users, coa):
    api = APIClient()
    api.force_authenticate(users["manager"])
    resp = api.patch(f"/api/v1/accounts/{coa['5601'].pk}/", {"name": "Via API"}, format="json")
    assert resp.status_code == 202 and resp.json()["change_request"].startswith("CHG-")
    assert Account.objects.get(pk=coa["5601"].pk).name == "Rent and premises"
    api.force_authenticate(users["admin"])
    resp = api.post("/api/v1/matching-rules/", {"name": "API rule", "strategy": "EXACT_REFERENCE"}, format="json")
    assert resp.status_code == 202 and not MatchingRule.objects.exists()
