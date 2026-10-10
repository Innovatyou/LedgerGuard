"""Fixed assets, loans, deposits, regulatory workpapers, integrations and administration."""

import shutil
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounting import reports
from apps.accounting import services as acc
from apps.accounting.models import FinancialPeriod, Journal
from apps.administration import services as admin_services
from apps.core.exceptions import BusinessRuleError, SegregationOfDutiesError
from apps.deposits import services as dep
from apps.deposits.models import DepositProduct
from apps.fixed_assets import services as fa
from apps.fixed_assets.models import AssetCategory, DepreciationRun, FixedAsset
from apps.imports.models import ImportBatch
from apps.integrations import services as integ
from apps.integrations.models import IntegrationEndpoint, IntegrationLog
from apps.loans import services as loans
from apps.loans.models import LoanSnapshot
from apps.regulatory import services as reg
from apps.regulatory.services import FormulaError, evaluate

from .conftest import lines, make_user, post
from .helpers import bank_profile, csv_bytes

pytestmark = pytest.mark.django_db
D = Decimal


def approve_and_post(journal, users):
    acc.approve_journal(journal, users["cfo"])
    return acc.post_journal(journal, users["cfo"])


# ------------------------------------------------------------------ fixed assets


@pytest.fixture
def category(institution, coa):
    return AssetCategory.objects.create(
        institution=institution,
        code="IT",
        name="IT equipment",
        cost_account=coa["1401"],
        accumulated_depreciation_account=coa["1409"],
        depreciation_expense_account=coa["5501"],
        disposal_account=coa["4901"],
        useful_life_months=36,
    )


def current_period(institution):
    return acc.period_for(institution, timezone.localdate())


def test_asset_depreciation_and_disposal(institution, users, coa, periods, category):
    period = current_period(institution)
    asset = fa.create_asset(
        institution,
        users["accountant"],
        category=category,
        tag="IT-001",
        name="Core switch",
        cost=D("3600000.00"),
        residual_value=D("0"),
        acquisition_date=period.start_date,
        in_service_date=period.start_date,
        credit_account=coa["1201"],
    )
    assert asset.capitalisation_journal.status == Journal.Status.SUBMITTED
    approve_and_post(asset.capitalisation_journal, users)

    preview = fa.preview_run(institution, period)
    assert [(a.tag, amount) for a, _, amount in preview.lines] == [("IT-001", D("100000.00"))]
    run = fa.run_depreciation(institution, users["accountant"], period)
    assert run.journal.status == Journal.Status.SUBMITTED and run.total == D("100000.00")
    with pytest.raises(BusinessRuleError, match="awaiting"):
        fa.run_depreciation(institution, users["accountant"], period)
    assert fa.accumulated_depreciation(asset) == 0  # not counted until posted
    approve_and_post(run.journal, users)
    fa.sync_statuses(institution)
    run.refresh_from_db()
    assert run.status == DepreciationRun.Status.POSTED
    assert fa.net_book_value(asset) == D("3500000.00")
    with pytest.raises(BusinessRuleError, match="already been posted"):
        fa.run_depreciation(institution, users["accountant"], period)

    controls = fa.register_control(institution, period.end_date)
    assert len(controls) == 1 and controls[0].reconciled
    assert controls[0].register_accumulated == controls[0].gl_accumulated == D("100000.00")

    journal = fa.prepare_disposal(
        asset, users["manager"], disposal_date=period.end_date, proceeds=D("3700000.00"), proceeds_account=coa["1201"]
    )
    amounts = {(ln.account.code, ln.debit, ln.credit) for ln in journal.lines.select_related("account")}
    assert ("4901", D("0"), D("200000.00")) in amounts  # gain = 3.7m − (3.6m − 0.1m)
    approve_and_post(journal, users)
    fa.sync_statuses(institution)
    asset.refresh_from_db()
    assert asset.status == FixedAsset.Status.DISPOSED
    assert reports.account_balance(coa["1401"]) == 0 and reports.account_balance(coa["1409"]) == 0


def test_depreciation_methods_and_caps(institution, users, category):
    asset = FixedAsset(
        cost=D("1000.00"), residual_value=D("100.00"), useful_life_months=3, method="STRAIGHT_LINE", annual_rate=D("0")
    )
    assert fa.charge_for(asset, D("0"), 1) == D("300.00")
    assert fa.charge_for(asset, D("800.00"), 1) == D("100.00")  # capped at residual
    assert fa.charge_for(asset, D("900.00"), 1) == 0
    rb = FixedAsset(
        cost=D("1200.00"), residual_value=D("0"), useful_life_months=60, method="REDUCING_BALANCE", annual_rate=D("25")
    )
    assert fa.charge_for(rb, D("0"), 1) == D("25.00")
    assert fa.charge_for(rb, D("600.00"), 1) == D("12.50")
    with pytest.raises(BusinessRuleError, match="Residual"):
        fa.create_asset(
            institution,
            users["accountant"],
            category=category,
            tag="X",
            name="x",
            cost=D("10"),
            residual_value=D("10"),
            acquisition_date=date(2026, 1, 1),
            in_service_date=date(2026, 1, 1),
        )


# ------------------------------------------------------------------ loans


LOAN_HEADER = ["Account No", "Customer", "Product", "Branch", "Principal Outstanding", "Accrued Interest", "DPD"]


def loan_rows():
    return [
        ["L001", "Ada Stores", "SME", "001", "1,000,000.00", "10,000.00", "0"],
        ["L002", "Bola Farms", "AGRIC", "001", "500,000.00", "0", "45"],
        ["L003", "Chika Ventures", "SME", "002", "200,000.00", "0", "75"],
        ["L004", "Dayo Trading", "SME", "002", "100,000.00", "0", "400"],
    ]


def test_loan_portfolio_control(institution, users, coa, periods):
    post(
        institution,
        users["accountant"],
        users["manager"],
        lines((coa["1301"], "D", "1800000.00"), (coa["1201"], "C", "1800000.00")),
    )
    post(
        institution,
        users["accountant"],
        users["manager"],
        lines((coa["5301"], "D", "50000.00"), (coa["1309"], "C", "50000.00")),
    )
    snapshot = loans.load_snapshot(
        institution,
        users["manager"],
        filename="loans.csv",
        data=csv_bytes(loan_rows(), LOAN_HEADER),
        as_of=timezone.localdate(),
        principal_account=coa["1301"],
        provision_account=coa["1309"],
    )
    control = loans.control(snapshot)
    by = control.by_classification
    assert by["Performing"].count == 1 and by["Pass and watch"].count == 1
    assert by["Substandard"].principal == D("200000.00") and by["Lost"].principal == D("100000.00")
    # 1% of 1,010,000 + 5% of 500,000 + 20% of 200,000 + 100% of 100,000
    assert control.total.provision == D("10100.00") + D("25000.00") + D("40000.00") + D("100000.00")
    assert control.principal_difference == 0
    assert control.provision_shortfall == D("175100.00") - D("50000.00")
    assert control.npl_ratio == D("44.44")
    journal = loans.prepare_provision_journal(snapshot, users["manager"], coa["5301"])
    approve_and_post(journal, users)
    snapshot.refresh_from_db()
    assert loans.control(snapshot).provision_shortfall == 0
    with pytest.raises(SegregationOfDutiesError):
        loans.review(snapshot, users["manager"])
    loans.review(snapshot, users["cfo"], "Agreed to core banking")
    with pytest.raises(BusinessRuleError, match="signed off"):
        loans.load_snapshot(
            institution,
            users["manager"],
            filename="again.csv",
            data=csv_bytes(loan_rows(), LOAN_HEADER),
            as_of=timezone.localdate(),
            principal_account=coa["1301"],
        )


def test_loan_file_with_bad_rows_is_rejected(institution, users, coa):
    rows = loan_rows() + [["L005", "Bad", "SME", "001", "abc", "0", "x"]]
    with pytest.raises(BusinessRuleError, match="Row 6"):
        loans.load_snapshot(
            institution,
            users["manager"],
            filename="bad.csv",
            data=csv_bytes(rows, LOAN_HEADER),
            as_of=date(2026, 9, 30),
            principal_account=coa["1301"],
        )
    with pytest.raises(BusinessRuleError, match="Missing required columns"):
        loans.load_snapshot(
            institution,
            users["manager"],
            filename="bad.csv",
            data=csv_bytes([["L1", "1"]], ["Account No", "Principal"]),
            as_of=date(2026, 9, 30),
            principal_account=coa["1301"],
        )
    assert not LoanSnapshot.objects.exists()


# ------------------------------------------------------------------ deposits


def test_deposit_control(institution, users, coa, periods):
    post(
        institution,
        users["accountant"],
        users["manager"],
        lines((coa["1201"], "D", "1500000.00"), (coa["2102"], "C", "1000000.00"), (coa["2101"], "C", "500000.00")),
    )
    DepositProduct.objects.create(institution=institution, code="SAV", name="Savings", gl_account=coa["2102"])
    DepositProduct.objects.create(institution=institution, code="CUR", name="Current", gl_account=coa["2101"])
    header = ["Account Number", "Account Name", "Product", "Branch", "Ledger Balance", "Dormant"]
    rows = [
        ["0011", "A", "SAV", "001", "600,000.00", "N"],
        ["0012", "B", "SAV", "001", "400,000.00", "Y"],
        ["0021", "C", "CUR", "002", "520,000.00", "N"],
        ["0022", "D", "CUR", "002", "-20,000.00", "N"],
        ["0031", "E", "FD90", "002", "1,000.00", "N"],
    ]
    snapshot = dep.load_snapshot(
        institution, users["manager"], filename="deposits.csv", data=csv_bytes(rows, header), as_of=timezone.localdate()
    )
    control = dep.control(snapshot)
    products = {p.code: p for p in control.products}
    assert products["SAV"].difference == 0
    assert products["CUR"].balance == D("500000.00") and products["CUR"].difference == 0
    assert [p.code for p in control.unmapped] == ["FD90"]
    assert control.overdrawn_total == D("-20000.00") and control.dormant_count == 1
    assert not control.reconciled


# ------------------------------------------------------------------ regulatory


def test_formula_evaluator():
    values = {"A": D("10"), "B": D("4")}
    assert evaluate("A + B * 2", values) == D("18")
    assert evaluate("(A + B) * 2", values) == D("28")
    assert evaluate("-A + 3", values) == D("-7")
    assert evaluate("A / B", values) == D("2.5")
    for bad in ("A / 0", "C + 1", "__import__('os')", "A +", "A ) ("):
        with pytest.raises(FormulaError):
            evaluate(bad, values)


def test_regulatory_workpaper(institution, users, coa, periods):
    post(
        institution,
        users["accountant"],
        users["manager"],
        lines((coa["1201"], "D", "1000000.00"), (coa["3101"], "C", "1000000.00")),
    )
    post(
        institution,
        users["accountant"],
        users["manager"],
        lines((coa["1301"], "D", "400000.00"), (coa["2102"], "C", "400000.00")),
    )
    post(
        institution,
        users["accountant"],
        users["manager"],
        lines((coa["1201"], "D", "50000.00"), (coa["4101"], "C", "50000.00")),
    )
    template = reg.install_starter_template(institution, users["manager"])
    today = timezone.localdate()
    ret = reg.prepare(institution, users["accountant"], template, today.replace(day=1), today)
    values = {k: D(v) for k, v in ret.values.items()}
    assert values["A900"] == values["T900"] == D("1450000.00")
    assert values["L100"] == D("400000.00") and values["E200"] == D("50000.00")
    assert values["R10"] == D("100.00")
    assert ret.passed
    preparer_reviewer = make_user("dual", institution, "accountant", "compliance_officer")
    ret = reg.prepare(institution, preparer_reviewer, template, today.replace(day=1), today)
    with pytest.raises(SegregationOfDutiesError):
        reg.review(ret, preparer_reviewer)
    ret = reg.review(ret, make_user("cco", institution, "compliance_officer"), "Agreed to TB")
    assert ret.status == "REVIEWED"
    with pytest.raises(BusinessRuleError, match="signed off"):
        reg.prepare(institution, users["accountant"], template, today.replace(day=1), today)


# ------------------------------------------------------------------ integrations


@pytest.fixture
def endpoint(institution, users):
    profile = bank_profile(institution, users["recon"])
    service = make_user("svc-core", institution, "recon_officer")
    ep, key = integ.create_endpoint(
        institution,
        users["admin"],
        name="Core banking",
        kind="API_UPLOAD",
        import_profile=profile,
        service_user=service,
        allowed_ips=["127.0.0.1/32"],
        auto_commit=True,
    )
    return ep, key


STATEMENT = csv_bytes(
    [["01/10/2026", "01/10/2026", "NIP1", "Inflow", "", "100.00"]],
    ["Date", "Value Date", "Reference", "Narration", "Debit", "Credit"],
)


def test_inbound_api(endpoint):
    ep, key = endpoint
    api = APIClient(REMOTE_ADDR="127.0.0.1")
    url = reverse("api-integration-inbound")
    resp = api.post(
        url, {"file": SimpleUploadedFile("stmt.csv", STATEMENT)}, HTTP_AUTHORIZATION=f"Bearer {key}", format="multipart"
    )
    assert resp.status_code == 201, resp.content
    assert resp.json()["committed"] is True
    assert ImportBatch.objects.get(pk=resp.json()["batch"]).uploaded_by == ep.service_user
    resp = api.post(
        url, {"file": SimpleUploadedFile("stmt.csv", STATEMENT)}, HTTP_AUTHORIZATION=f"Bearer {key}", format="multipart"
    )
    assert resp.status_code == 422  # same file already imported
    bad = api.post(
        url, {"file": SimpleUploadedFile("s.csv", STATEMENT)}, HTTP_AUTHORIZATION=f"Bearer {key}x", format="multipart"
    )
    assert bad.status_code == 401
    other = APIClient(REMOTE_ADDR="10.9.9.9").post(
        url, {"file": SimpleUploadedFile("s.csv", STATEMENT)}, HTTP_AUTHORIZATION=f"Bearer {key}", format="multipart"
    )
    assert other.status_code == 403
    assert IntegrationLog.objects.filter(endpoint=ep).count() == 3
    assert not IntegrationEndpoint.objects.filter(key_hash__contains=key.split(".")[1]).exists()


def test_endpoint_requires_authorised_service_user(institution, users):
    profile = bank_profile(institution)
    with pytest.raises(BusinessRuleError, match="import.upload"):
        integ.create_endpoint(
            institution,
            users["admin"],
            name="x",
            kind="API_UPLOAD",
            import_profile=profile,
            service_user=users["viewer"],
        )
    with pytest.raises(BusinessRuleError, match="relative"):
        integ.create_endpoint(
            institution,
            users["admin"],
            name="y",
            kind="DROP_FOLDER",
            import_profile=profile,
            service_user=users["recon"],
            folder="../etc",
        )


def test_drop_folder_ingestion(institution, users, settings, tmp_path):
    settings.LEDGERGUARD = {**settings.LEDGERGUARD, "INGEST_ROOT": str(tmp_path / "ingest")}
    profile = bank_profile(institution)
    integ.create_endpoint(
        institution,
        users["admin"],
        name="SFTP",
        kind="DROP_FOLDER",
        import_profile=profile,
        service_user=users["recon"],
        folder="statements",
    )
    folder = tmp_path / "ingest" / institution.code / "statements"
    folder.mkdir(parents=True)
    (folder / "good.csv").write_bytes(STATEMENT)
    (folder / "bad.xlsx").write_bytes(b"not a workbook")
    assert integ.process_drop_folders() == {"processed": 1, "failed": 1}
    assert len(list((folder / "processed").iterdir())) == 1 and len(list((folder / "failed").iterdir())) == 1
    assert ImportBatch.objects.filter(institution=institution, status="VALIDATED").count() == 1


# ------------------------------------------------------------------ administration


def test_integrity_check(institution, users, coa, periods):
    journal = post(
        institution, users["accountant"], users["manager"], lines((coa["5601"], "D", "10"), (coa["2301"], "C", "10"))
    )
    assert admin_services.check_integrity(institution).passed
    Journal.objects.filter(pk=journal.pk).update(status=Journal.Status.DRAFT)  # simulated tampering
    check = admin_services.check_integrity(institution)
    assert not check.passed and "not in posted status" in check.findings[0]


@pytest.mark.skipif(shutil.which("pg_dump") is None, reason="pg_dump not installed")
def test_backup_and_verify(settings, tmp_path, institution):
    settings.LEDGERGUARD = {**settings.LEDGERGUARD, "BACKUP_DIR": str(tmp_path / "backups")}
    record = admin_services.verify_backup(admin_services.backup_database())
    assert record.status == "VERIFIED" and len(record.sha256) == 64
    with open(record.path, "ab") as fh:
        fh.write(b"tamper")
    with pytest.raises(admin_services.BackupError, match="Checksum"):
        admin_services.verify_backup(record)
    media = admin_services.verify_backup(admin_services.backup_media())
    assert media.status == "VERIFIED"
    assert admin_services.prune_backups(days=-1) == 2


def test_system_page_superuser_only(client, users, django_user_model):
    client.force_login(users["admin"])
    assert client.get(reverse("administration:system")).status_code == 403
    root = django_user_model.objects.create_superuser("root", "root@example.com", "Sup3r-Secret-Passw0rd")
    client.force_login(root)
    resp = client.get(reverse("administration:system"))
    assert resp.status_code == 200 and resp.context["health"]["database"] == "ok"


def test_new_pages_render(client, institution, users, coa, periods, category):
    client.force_login(users["cfo"])
    period = current_period(institution)
    for url in [
        reverse("fixed_assets:register"),
        reverse("fixed_assets:depreciation") + f"?period={period.pk}",
        reverse("loans:index"),
        reverse("deposits:index"),
        reverse("regulatory:index"),
        reverse("accounting:recurring"),
        reverse("approvals:changes"),
    ]:
        assert client.get(url).status_code == 200, url
    client.force_login(users["admin"])
    assert client.get(reverse("integrations:index")).status_code == 200
    assert FinancialPeriod.objects.filter(institution=institution).exists()
    assert timedelta(days=1)
