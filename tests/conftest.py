from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.accounting.coa_templates import apply_template
from apps.accounting.models import Account
from apps.accounting.services import (
    LineInput,
    approve_journal,
    create_journal,
    generate_monthly_periods,
    post_journal,
    submit_journal,
)
from apps.accounts.models import Membership, Role, User
from apps.organizations.models import Institution, InstitutionType


@pytest.fixture(autouse=True)
def _test_settings(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    settings.SECURE_SSL_REDIRECT = False
    settings.CELERY_TASK_ALWAYS_EAGER = True
    cache.clear()


def make_institution(code="acme-mfb", name="Acme Microfinance Bank", kind=InstitutionType.MFB, **kw):
    return Institution.objects.create(code=code, name=name, institution_type=kind, **kw)


def make_user(username, institution, *roles, branch=None, **kw):
    user = User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password="Str0ng-Passw0rd!",
        first_name=username.capitalize(),
        **kw,
    )
    for role_code in roles:
        Membership.objects.create(
            user=user, institution=institution, role=Role.objects.get(code=role_code, institution=None), branch=branch
        )
    return user


@pytest.fixture
def institution(db):
    return make_institution()


@pytest.fixture
def other_institution(db):
    return make_institution(code="other-bank", name="Other Commercial Bank", kind=InstitutionType.COMMERCIAL_BANK)


@pytest.fixture
def users(institution):
    return {
        "admin": make_user("admin", institution, "institution_admin"),
        "accountant": make_user("ada", institution, "accountant"),
        "manager": make_user("musa", institution, "finance_manager"),
        "cfo": make_user("chioma", institution, "cfo"),
        "recon": make_user("rita", institution, "recon_officer"),
        "reviewer": make_user("remi", institution, "recon_reviewer"),
        "auditor": make_user("ibrahim", institution, "internal_auditor"),
        "viewer": make_user("vera", institution, "management_viewer"),
    }


@pytest.fixture
def coa(institution, users):
    apply_template(institution, users["manager"], "MFB")
    return {a.code: a for a in Account.objects.filter(institution=institution)}


@pytest.fixture
def periods(institution, users):
    year = timezone.localdate().year
    generate_monthly_periods(institution, users["manager"], year - 1)
    return generate_monthly_periods(institution, users["manager"], year)


def post(institution, preparer, approver, lines, day=None, description="Test journal", **kw):
    day = day or timezone.localdate()
    journal = create_journal(institution, preparer, transaction_date=day, description=description, lines=lines, **kw)
    submit_journal(journal, preparer)
    approve_journal(journal, approver)
    return post_journal(journal, approver)


def lines(*specs):
    """lines((account, 'D', '100.00'), (account, 'C', '100.00'))"""
    out = []
    for account, side, amount in specs:
        amount = Decimal(amount)
        out.append(
            LineInput(
                account=account,
                debit=amount if side == "D" else Decimal("0"),
                credit=amount if side == "C" else Decimal("0"),
            )
        )
    return out


@pytest.fixture
def today():
    return timezone.localdate()


@pytest.fixture
def yesterday(today):
    return today - timedelta(days=1)


__all__ = ["make_institution", "make_user", "post", "lines", "date"]
