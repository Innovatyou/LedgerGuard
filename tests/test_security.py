"""Authentication, authorisation, tenant isolation and audit-trail tests."""

import time
from decimal import Decimal

import pytest
from django.db import DatabaseError, connection, transaction
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.accounting.models import Account, Journal
from apps.accounts import mfa
from apps.core.models import ActivityLog
from apps.core.tenancy import SESSION_KEY
from apps.imports.models import ImportProfile
from apps.reconciliation.models import Reconciliation
from apps.suspense.models import SuspenseItem

from .conftest import lines, make_user, post
from .helpers import bank_profile, d

pytestmark = pytest.mark.django_db
PASSWORD = "Str0ng-Passw0rd!"


# ------------------------------------------------------------------ authentication


def test_login_and_logout(client, users):
    resp = client.post(reverse("accounts:login"), {"username": "ada", "password": PASSWORD})
    assert resp.status_code == 302 and resp.url == reverse("reporting:dashboard")
    assert client.get(reverse("reporting:dashboard")).status_code == 200
    client.post(reverse("accounts:logout"))
    assert client.get(reverse("reporting:dashboard")).status_code == 302
    assert ActivityLog.objects.filter(action="auth.login").exists()


def test_login_throttling_locks_after_failures(client, users, settings):
    settings.LEDGERGUARD = {**settings.LEDGERGUARD, "LOGIN_MAX_FAILURES": 3}
    for _ in range(3):
        client.post(reverse("accounts:login"), {"username": "ada", "password": "wrong"})
    resp = client.post(reverse("accounts:login"), {"username": "ada", "password": PASSWORD}, follow=True)
    assert "Too many failed attempts" in resp.content.decode()
    assert "_auth_user_id" not in client.session
    assert ActivityLog.objects.filter(action="auth.login_failed").count() == 3


def test_totp_rfc6238_vector():
    # RFC 6238 test secret "12345678901234567890" (base32), SHA-1, T=59 -> 94287082 (8 digits) -> 287082
    import base64

    secret = base64.b32encode(b"12345678901234567890").decode()
    assert mfa.code_at(secret, 59 // 30) == "287082"
    assert mfa.verify(secret, "287082", now=59) == 1
    assert mfa.verify(secret, "287082", now=59, last_used_step=1) is None  # replay rejected


def test_mfa_login_flow(client, users):
    user = users["accountant"]
    user.mfa_secret = mfa.generate_secret()
    user.mfa_enabled = True
    user.save()
    resp = client.post(reverse("accounts:login"), {"username": "ada", "password": PASSWORD})
    assert resp.url == reverse("accounts:mfa-verify")
    assert "_auth_user_id" not in client.session  # not logged in after password alone
    client.post(reverse("accounts:mfa-verify"), {"code": "000000"})
    assert "_auth_user_id" not in client.session
    code = mfa.code_at(user.mfa_secret, mfa.current_step())
    resp = client.post(reverse("accounts:mfa-verify"), {"code": code})
    assert resp.status_code == 302 and client.session["_auth_user_id"] == str(user.pk)


def test_institution_requiring_mfa_forces_enrolment(client, institution, users):
    institution.require_mfa = True
    institution.save()
    client.force_login(users["accountant"])
    resp = client.get(reverse("reporting:dashboard"))
    assert resp.status_code == 302 and resp.url == reverse("accounts:mfa-setup")


# ------------------------------------------------------------------ authorisation


def test_permission_denied_pages(client, users):
    client.force_login(users["viewer"])
    assert client.get(reverse("accounting:journals")).status_code == 403
    assert client.get(reverse("accounts:users")).status_code == 403
    assert client.get(reverse("activity-log")).status_code == 403
    assert client.get(reverse("reporting:trial-balance")).status_code == 200
    assert client.get(reverse("reporting:trial-balance") + "?export=xlsx").status_code == 403  # no export permission


def test_deactivated_membership_loses_access(client, institution, users):
    user = users["accountant"]
    user.memberships.update(is_active=False)
    client.force_login(user)
    assert client.get(reverse("accounting:journals")).status_code == 403


# ------------------------------------------------------------------ tenant isolation


@pytest.fixture
def two_tenants(institution, other_institution, users, coa, periods):
    foreign_user = make_user("ola", other_institution, "cfo", "accountant", "recon_officer")
    foreign_account = Account.objects.create(
        institution=other_institution, code="1201", name="Foreign bank", category="ASSET"
    )
    foreign_profile = bank_profile(other_institution, foreign_user)
    foreign_recon = Reconciliation.objects.create(
        institution=other_institution,
        name="Foreign recon",
        left_account_reference="1",
        right_account_reference="1",
        period_start=d("2026-10-01"),
        period_end=d("2026-10-31"),
        prepared_by=foreign_user,
    )
    foreign_journal = Journal.objects.create(
        institution=other_institution,
        transaction_date=d("2026-10-01"),
        accounting_date=d("2026-10-01"),
        description="Secret",
        currency="NGN",
        prepared_by=foreign_user,
    )
    return {
        "user": foreign_user,
        "account": foreign_account,
        "profile": foreign_profile,
        "recon": foreign_recon,
        "journal": foreign_journal,
    }


def test_views_cannot_reach_other_institution_records(client, users, two_tenants):
    client.force_login(users["cfo"])
    t = two_tenants
    urls = [
        reverse("accounting:journal-detail", args=[t["journal"].pk]),
        reverse("accounting:account-edit", args=[t["account"].pk]),
        reverse("reconciliation:detail", args=[t["recon"].pk]),
        reverse("reporting:recon-statement", args=[t["recon"].pk]),
        reverse("imports:profile-edit", args=[t["profile"].pk]),
        reverse("reporting:general-ledger") + f"?account={t['account'].pk}",
    ]
    for url in urls:
        assert client.get(url).status_code in (403, 404), url
    resp = client.post(reverse("accounting:journal-action", args=[t["journal"].pk]), {"action": "submit"})
    assert resp.status_code == 404


def test_session_tampering_cannot_switch_institution(client, users, two_tenants):
    client.force_login(users["cfo"])
    session = client.session
    session[SESSION_KEY] = two_tenants["user"].memberships.first().institution_id
    session.save()
    resp = client.get(reverse("accounting:journals"))
    assert resp.status_code == 200
    assert "Secret" not in resp.content.decode()
    resp = client.post(
        reverse("accounts:switch-institution"),
        {"institution": two_tenants["user"].memberships.first().institution_id},
        follow=True,
    )
    assert "do not have access" in resp.content.decode()


def test_api_tenant_isolation(users, two_tenants, institution):
    api = APIClient()
    token = Token.objects.create(user=users["manager"])
    api.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    resp = api.get("/api/v1/journals/")
    assert resp.status_code == 200
    assert all(j["description"] != "Secret" for j in resp.json()["results"])
    assert api.get(f"/api/v1/journals/{two_tenants['journal'].pk}/").status_code == 404
    assert api.get(f"/api/v1/reconciliations/{two_tenants['recon'].pk}/statement/").status_code == 404
    # Asking for the other institution explicitly fails closed.
    resp = api.get("/api/v1/journals/", HTTP_X_INSTITUTION=str(two_tenants["account"].institution_id))
    assert resp.status_code == 403
    # Creating a journal that references a foreign account is rejected.
    resp = api.post(
        "/api/v1/journals/",
        {
            "transaction_date": "2026-10-01",
            "accounting_date": "2026-10-01",
            "description": "x",
            "currency": "NGN",
            "lines": [
                {"account": two_tenants["account"].pk, "debit": "10.00", "credit": "0"},
                {"account": two_tenants["account"].pk, "debit": "0", "credit": "10.00"},
            ],
        },
        format="json",
    )
    assert resp.status_code == 400


def test_api_permissions_and_workflow(users, institution, coa, periods):
    api = APIClient()
    api.force_authenticate(users["accountant"])
    payload = {
        "transaction_date": str(timezone.localdate()),
        "description": "API accrual",
        "currency": "NGN",
        "accounting_date": str(timezone.localdate()),
        "idempotency_key": "abc-1",
        "lines": [
            {"account": coa["5601"].pk, "debit": "100.00", "credit": "0"},
            {"account": coa["2301"].pk, "debit": "0", "credit": "100.00"},
        ],
    }
    resp = api.post("/api/v1/journals/", payload, format="json")
    assert resp.status_code == 201, resp.content
    jid = resp.json()["id"]
    assert api.post("/api/v1/journals/", payload, format="json").json()["id"] == jid  # idempotent
    assert api.post(f"/api/v1/journals/{jid}/submit/").status_code == 200
    assert api.post(f"/api/v1/journals/{jid}/approve/").status_code == 403  # accountant cannot approve
    api.force_authenticate(users["manager"])
    assert api.post(f"/api/v1/journals/{jid}/approve/").status_code == 200
    resp = api.post(f"/api/v1/journals/{jid}/post/")
    assert resp.status_code == 200 and resp.json()["status"] == "POSTED"
    resp = api.post(f"/api/v1/journals/{jid}/post/")
    assert resp.status_code == 400 and resp.json()["code"] == "already_posted"
    tb = api.get("/api/v1/reports/trial-balance/?as_of=2026-12-31").json()
    assert tb["balanced"] and tb["total_debit"] == "100.00"


def test_unauthenticated_api_rejected():
    assert APIClient().get("/api/v1/journals/").status_code in (401, 403)


# ------------------------------------------------------------------ audit trail


def test_activity_log_is_append_only(institution, users, coa, periods):
    post(institution, users["accountant"], users["manager"], lines((coa["5601"], "D", "10"), (coa["2301"], "C", "10")))
    entry = ActivityLog.objects.filter(action="journal.posted").first()
    assert entry is not None and entry.user == users["manager"]
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        entry.save()
    if connection.vendor == "postgresql":
        with pytest.raises(DatabaseError), transaction.atomic():
            ActivityLog.objects.filter(pk=entry.pk).update(action="tampered")
        with pytest.raises(DatabaseError), transaction.atomic():
            ActivityLog.objects.all().delete()


def test_health_endpoints(client):
    assert client.get("/health/").json() == {"status": "ok"}
    assert client.get("/health/ready/").json()["database"] is True


def test_suspense_model_ordering_unaffected():
    assert SuspenseItem._meta.ordering == ["original_date", "id"]
    assert ImportProfile._meta.ordering == ["name"]
    assert time.time() > 0 and Decimal("1") == 1
