import io
import zipfile
from datetime import date

from openpyxl import Workbook

from apps.imports import services as import_services
from apps.imports.models import AmountMode, ImportProfile, Side, SourceType


def csv_bytes(rows, header):
    lines = [",".join(header)] + [",".join(f'"{v}"' if "," in str(v) else str(v) for v in r) for r in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def xlsx_bytes(rows, header, sheet="Sheet1"):
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(header)
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def macro_xlsx_bytes():
    data = xlsx_bytes([["x"]], ["a"])
    buf = io.BytesIO(data)
    out = io.BytesIO()
    with zipfile.ZipFile(buf) as src, zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        dst.writestr("xl/vbaProject.bin", b"\x00fake")
    return out.getvalue()


def bank_profile(institution, user=None, **kw):
    defaults = dict(
        institution=institution,
        name="Correspondent bank statement",
        source_type=SourceType.BANK_STATEMENT,
        source_system="Zenith statement",
        amount_mode=AmountMode.SEPARATE,
        date_formats=["%d/%m/%Y"],
        column_mapping={
            "transaction_date": "Date",
            "value_date": "Value Date",
            "transaction_id": "Reference",
            "narration": "Narration",
            "debit_amount": "Debit",
            "credit_amount": "Credit",
        },
        default_account_reference="1201",
        created_by=user,
    )
    defaults.update(kw)
    return ImportProfile.objects.create(**defaults)


def gl_profile(institution, user=None, **kw):
    defaults = dict(
        institution=institution,
        name="Core banking GL",
        source_type=SourceType.GL_EXPORT,
        source_system="Core GL",
        amount_mode=AmountMode.SIGNED,
        signed_positive_is=Side.DEBIT,
        date_formats=["%Y-%m-%d"],
        column_mapping={
            "transaction_date": "TranDate",
            "transaction_id": "TranRef",
            "narration": "Description",
            "amount": "Amount",
            "account_reference": "GL",
        },
        created_by=user,
    )
    defaults.update(kw)
    return ImportProfile.objects.create(**defaults)


def import_file(institution, user, profile, filename, data, allow_partial=False):
    batch = import_services.create_batch(institution, user, profile, filename, data)
    import_services.validate_batch(batch, user)
    batch.refresh_from_db()
    return import_services.commit_batch(batch, user, allow_partial=allow_partial)


def d(day: str) -> date:
    return date.fromisoformat(day)


_counter = {"n": 0}


def source_batch(institution, user, profile):
    from apps.imports.models import ImportBatch

    _counter["n"] += 1
    return ImportBatch.objects.create(
        institution=institution,
        profile=profile,
        original_filename=f"fixture-{_counter['n']}.csv",
        extension=".csv",
        sha256=f"{_counter['n']:064d}",
        size=1,
        uploaded_by=user,
        status=ImportBatch.Status.COMMITTED,
    )


def txn(batch, *, day, amount, ref="", ext="", narration="", account=None, **kw):
    """Create a SourceTransaction. ``amount`` is signed: positive = credit in the source's convention."""
    from decimal import Decimal

    from apps.imports.models import SourceTransaction
    from apps.imports.normalize import normalize_narration, normalize_reference

    _counter["n"] += 1
    amount = Decimal(str(amount))
    profile = batch.profile
    return SourceTransaction.objects.create(
        institution=batch.institution,
        batch=batch,
        source_type=profile.source_type,
        source_system=profile.source_system,
        source_file=batch.original_filename,
        source_row_number=_counter["n"],
        account_reference=account or profile.default_account_reference or "1201",
        transaction_id=ref,
        external_reference=ext,
        norm_transaction_id=normalize_reference(ref),
        norm_external_reference=normalize_reference(ext),
        transaction_date=d(day) if isinstance(day, str) else day,
        debit_amount=-amount if amount < 0 else Decimal("0"),
        credit_amount=amount if amount > 0 else Decimal("0"),
        signed_amount=amount,
        currency=kw.pop("currency", "NGN"),
        narration=narration,
        norm_narration=normalize_narration(narration),
        fingerprint=f"fp-{_counter['n']}",
        **kw,
    )
