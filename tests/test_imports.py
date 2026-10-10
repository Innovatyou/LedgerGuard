from datetime import date
from decimal import Decimal

import pytest

from apps.core.exceptions import BusinessRuleError, PermissionDeniedError
from apps.core.money import AmountParseError, parse_amount
from apps.imports import services
from apps.imports.models import AmountMode, ImportBatch, ImportRow, SourceTransaction
from apps.imports.normalize import normalize_narration, normalize_reference, parse_date

from .helpers import bank_profile, csv_bytes, gl_profile, import_file, macro_xlsx_bytes, xlsx_bytes

D = Decimal
HEADER = ["Date", "Value Date", "Reference", "Narration", "Debit", "Credit"]
ROWS = [
    ["01/10/2026", "01/10/2026", "NIP/0001", "Transfer from Okafor", "", "250,000.00"],
    ["02/10/2026", "02/10/2026", "CHG-77", "SMS alert charge", "50.00", ""],
    ["03/10/2026", "03/10/2026", "POS-9", "POS settlement", "", "1,250,500.50"],
]


# ------------------------------------------------------------------ unit


@pytest.mark.parametrize(
    "raw,expected,hint",
    [
        ("1,250,000.00", D("1250000.00"), None),
        ("(5,000.00)", D("-5000.00"), None),
        ("₦12,500", D("12500.00"), None),
        ("NGN 7,000.5", D("7000.50"), None),
        ("5,000.00 DR", D("5000.00"), "DR"),
        ("5000CR", D("5000.00"), "CR"),
        ("-42.10", D("-42.10"), None),
        ("42.10-", D("-42.10"), None),
        (1234.5, D("1234.50"), None),
        (100, D("100.00"), None),
        ("", None, None),
    ],
)
def test_parse_amount(raw, expected, hint):
    assert parse_amount(raw) == (expected, hint)


@pytest.mark.parametrize("raw", ["12.345", "abc", "1.2.3", "--5"])
def test_parse_amount_rejects_bad_values(raw):
    with pytest.raises(AmountParseError):
        parse_amount(raw)


def test_parse_amount_never_uses_float_rounding():
    assert parse_amount(1234.1)[0] == D("1234.10")  # float cells are converted via repr, not binary rounding
    assert parse_amount("0.10")[0] + parse_amount("0.20")[0] == D("0.30")


def test_dates_and_references():
    assert parse_date("31/10/2026", ["%d/%m/%Y"]) == date(2026, 10, 31)
    assert parse_date("31-Oct-2026") == date(2026, 10, 31)
    assert parse_date("2026-10-31T00:00:00") == date(2026, 10, 31)
    assert parse_date("46326") == date(2026, 10, 31)  # Excel serial
    assert normalize_reference(" nip/ 0001 ") == "NIP/0001"
    assert normalize_reference("000123") == "000123"  # leading zeros preserved
    assert normalize_narration("Trf: from  OKAFOR, J.") == "TRF FROM OKAFOR J"


# ------------------------------------------------------------------ workflow


@pytest.fixture
def profile(institution, users):
    return bank_profile(institution, users["recon"])


def test_csv_import_end_to_end(institution, users, profile):
    batch = import_file(institution, users["recon"], profile, "stmt.csv", csv_bytes(ROWS, HEADER))
    assert batch.status == ImportBatch.Status.COMMITTED
    assert batch.committed_rows == 3 and batch.invalid_rows == 0
    txns = list(SourceTransaction.objects.filter(batch=batch).order_by("source_row_number"))
    assert [t.source_row_number for t in txns] == [2, 3, 4]
    first, charge, pos = txns
    assert first.credit_amount == D("250000.00") and first.signed_amount == D("250000.00")
    assert charge.debit_amount == D("50.00") and charge.signed_amount == D("-50.00")
    assert pos.credit_amount == D("1250500.50")
    assert first.norm_transaction_id == "NIP/0001" and first.account_reference == "1201"
    assert first.source_values["credit_amount"] == "250,000.00"  # original text preserved
    assert len(batch.sha256) == 64 and batch.file


def test_xlsx_import_with_native_types(institution, users, profile):
    rows = [
        [date(2026, 10, 1), date(2026, 10, 1), "NIP/0001", "Transfer", None, 250000.5],
        [date(2026, 10, 2), date(2026, 10, 2), "CHG-77", "Charge", 50, None],
    ]
    batch = import_file(institution, users["recon"], profile, "stmt.xlsx", xlsx_bytes(rows, HEADER))
    txns = list(SourceTransaction.objects.filter(batch=batch).order_by("source_row_number"))
    assert txns[0].transaction_date == date(2026, 10, 1)
    assert txns[0].credit_amount == D("250000.50")
    assert txns[1].debit_amount == D("50.00")


def test_invalid_rows_block_commit_unless_partial(institution, users, profile):
    rows = ROWS + [
        ["32/13/2026", "", "BAD-1", "Bad date", "", "10.00"],
        ["04/10/2026", "", "BAD-2", "Both sides", "10.00", "10.00"],
        ["05/10/2026", "", "BAD-3", "No amount", "", ""],
    ]
    batch = services.create_batch(institution, users["recon"], profile, "stmt.csv", csv_bytes(rows, HEADER))
    services.validate_batch(batch, users["recon"])
    batch.refresh_from_db()
    assert batch.invalid_rows == 3 and batch.valid_rows == 3
    errors = {r.row_number: r.errors for r in batch.rows.filter(status=ImportRow.Status.INVALID)}
    assert "Unrecognised date" in errors[5][0]
    assert "both a debit and a credit" in errors[6][0]
    assert "no transaction amount" in errors[7][0]
    with pytest.raises(BusinessRuleError, match="invalid"):
        services.commit_batch(batch, users["recon"])
    batch = services.commit_batch(batch, users["recon"], allow_partial=True)
    assert batch.committed_rows == 3
    assert batch.rows.filter(status=ImportRow.Status.INVALID).count() == 3  # never silently discarded


def test_row_correction_before_commit(institution, users, profile):
    rows = ROWS + [["32/13/2026", "", "FIX-1", "Typo date", "", "10.00"]]
    batch = services.create_batch(institution, users["recon"], profile, "stmt.csv", csv_bytes(rows, HEADER))
    services.validate_batch(batch, users["recon"])
    row = batch.rows.get(row_number=5)
    with pytest.raises(BusinessRuleError, match="note"):
        services.correct_row(row, users["recon"], {"Date": "04/10/2026"}, "")
    row = services.correct_row(row, users["recon"], {"Date": "04/10/2026"}, "Date mistyped in source; confirmed")
    assert row.status == ImportRow.Status.VALID and row.corrected_by == users["recon"]
    batch.refresh_from_db()
    assert batch.invalid_rows == 0
    batch = services.commit_batch(batch, users["recon"])
    assert batch.committed_rows == 4


def test_exact_same_file_cannot_be_imported_twice(institution, users, profile):
    data = csv_bytes(ROWS, HEADER)
    import_file(institution, users["recon"], profile, "stmt.csv", data)
    with pytest.raises(BusinessRuleError, match="already imported"):
        services.create_batch(institution, users["recon"], profile, "stmt-copy.csv", data)


def test_overlapping_file_imports_only_new_rows(institution, users, profile):
    import_file(institution, users["recon"], profile, "week1.csv", csv_bytes(ROWS[:2], HEADER))
    batch = import_file(institution, users["recon"], profile, "week1-2.csv", csv_bytes(ROWS, HEADER))
    assert batch.duplicate_rows == 2 and batch.committed_rows == 1
    assert SourceTransaction.objects.filter(institution=institution).count() == 3


def test_identical_rows_within_file_are_flagged_not_dropped(institution, users, profile):
    rows = ROWS + [ROWS[1]]  # two identical SMS charges
    batch = import_file(institution, users["recon"], profile, "dups.csv", csv_bytes(rows, HEADER))
    assert batch.committed_rows == 4 and batch.warning_rows == 1
    assert SourceTransaction.objects.filter(batch=batch, is_suspected_duplicate=True).count() == 1
    # Re-importing the same content in a new file is still idempotent.
    batch2 = import_file(institution, users["recon"], profile, "dups-again.csv", csv_bytes(rows, HEADER) + b"\n")
    assert batch2.committed_rows == 0 and batch2.duplicate_rows == 4


def test_same_transactions_in_another_institution_are_independent(institution, other_institution, users, profile):
    from .conftest import make_user

    import_file(institution, users["recon"], profile, "a.csv", csv_bytes(ROWS, HEADER))
    other_user = make_user("ola", other_institution, "recon_officer")
    other_profile = bank_profile(other_institution, other_user)
    batch = import_file(other_institution, other_user, other_profile, "a.csv", csv_bytes(ROWS, HEADER))
    assert batch.committed_rows == 3


def test_signed_amount_profile(institution, users):
    profile = gl_profile(institution, users["recon"])
    header = ["TranDate", "TranRef", "Description", "Amount", "GL"]
    rows = [
        ["2026-10-01", "FT001", "Inward transfer", "250000.00", "1201"],
        ["2026-10-02", "FT002", "Charge", "-50.00", "1201"],
        ["2026-10-02", "FT003", "Charge", "75.00 CR", "1201"],
    ]
    batch = import_file(institution, users["recon"], profile, "gl.csv", csv_bytes(rows, header))
    txns = {t.transaction_id: t for t in SourceTransaction.objects.filter(batch=batch)}
    assert txns["FT001"].debit_amount == D("250000.00")  # positive = debit for this source
    assert txns["FT002"].credit_amount == D("50.00")
    assert txns["FT003"].credit_amount == D("75.00")  # explicit suffix wins


def test_indicator_amount_profile(institution, users):
    profile = bank_profile(
        institution,
        users["recon"],
        name="Indicator",
        amount_mode=AmountMode.INDICATOR,
        column_mapping={"transaction_date": "Date", "transaction_id": "Ref", "amount": "Amt", "dr_cr_indicator": "DC"},
        indicator_debit_values=["D"],
        indicator_credit_values=["C"],
    )
    rows = [["01/10/2026", "R1", "100.00", "C"], ["01/10/2026", "R2", "40.00", "D"], ["01/10/2026", "R3", "5", "X"]]
    batch = services.create_batch(
        institution, users["recon"], profile, "ind.csv", csv_bytes(rows, ["Date", "Ref", "Amt", "DC"])
    )
    services.validate_batch(batch, users["recon"])
    batch.refresh_from_db()
    assert batch.valid_rows == 2 and batch.invalid_rows == 1


def test_file_safety_checks(institution, users, profile):
    with pytest.raises(BusinessRuleError, match="Unsupported"):
        services.create_batch(institution, users["recon"], profile, "evil.xlsm", b"data")
    with pytest.raises(BusinessRuleError, match="Macro-enabled"):
        services.create_batch(institution, users["recon"], profile, "evil.xlsx", macro_xlsx_bytes())
    with pytest.raises(BusinessRuleError, match="not a valid"):
        services.create_batch(institution, users["recon"], profile, "fake.xlsx", b"not a zip")
    with pytest.raises(BusinessRuleError, match="empty"):
        services.create_batch(institution, users["recon"], profile, "empty.csv", b"")


def test_missing_mapped_column_fails_validation(institution, users, profile):
    batch = services.create_batch(
        institution, users["recon"], profile, "x.csv", csv_bytes([["01/10/2026", "1"]], ["Date", "Amount"])
    )
    with pytest.raises(BusinessRuleError, match="not found"):
        services.validate_batch(batch, users["recon"])
    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.FAILED


def test_profile_mapping_validation(institution):
    profile = bank_profile(institution, column_mapping={"transaction_date": "Date"})
    with pytest.raises(BusinessRuleError, match="must map"):
        services.validate_profile_mapping(profile)


def test_import_requires_permission(institution, users, profile):
    with pytest.raises(PermissionDeniedError):
        services.create_batch(institution, users["viewer"], profile, "x.csv", csv_bytes(ROWS, HEADER))


def test_profile_from_other_institution_rejected(institution, other_institution, users):
    foreign = bank_profile(other_institution)
    with pytest.raises(BusinessRuleError, match="different institution"):
        services.create_batch(institution, users["recon"], foreign, "x.csv", csv_bytes(ROWS, HEADER))


def test_retention_purges_file_but_keeps_checksum(institution, users, profile):
    from datetime import timedelta

    from django.utils import timezone

    batch = import_file(institution, users["recon"], profile, "old.csv", csv_bytes(ROWS, HEADER))
    future = timezone.now() + timedelta(days=366 * (institution.document_retention_years + 1))
    assert services.purge_expired_files(institution, now=future) == 1
    batch.refresh_from_db()
    assert not batch.file and batch.sha256 and batch.file_purged_at
    assert SourceTransaction.objects.filter(batch=batch).count() == 3
