"""
Import workflow: upload → preview/map → validate → (correct) → commit.
"""

import hashlib
import os
from collections import Counter

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.services import require_perm
from apps.core.activity import log_activity
from apps.core.exceptions import BusinessRuleError

from .models import STANDARD_FIELDS, AmountMode, ImportBatch, ImportProfile, ImportRow, SourceTransaction
from .normalize import content_key, fingerprint, from_jsonable, normalize_row, to_jsonable
from .parsers import FileRejected, inspect_file, read_table

REQUIRED_MAPPINGS = {
    AmountMode.SEPARATE: [("transaction_date",), ("debit_amount", "credit_amount")],
    AmountMode.SIGNED: [("transaction_date",), ("amount",)],
    AmountMode.INDICATOR: [("transaction_date",), ("amount",), ("dr_cr_indicator",)],
}


def validate_profile_mapping(profile: ImportProfile):
    mapping = profile.column_mapping or {}
    unknown = set(mapping) - set(STANDARD_FIELDS)
    if unknown:
        raise BusinessRuleError(f"Unknown mapped fields: {', '.join(sorted(unknown))}")
    for options in REQUIRED_MAPPINGS[profile.amount_mode]:
        if not any(mapping.get(f) for f in options):
            raise BusinessRuleError(f"Profile must map: {' or '.join(options)}")


def _file_bytes(batch: ImportBatch) -> bytes:
    if not batch.file:
        raise BusinessRuleError("The original file is no longer retained for this batch.")
    with batch.file.open("rb") as fh:
        return fh.read()


def create_batch(institution, user, profile: ImportProfile, filename: str, data: bytes) -> ImportBatch:
    require_perm(user, institution, "import.upload")
    if profile.institution_id != institution.pk:
        raise BusinessRuleError("Import profile belongs to a different institution.")
    if not profile.is_active:
        raise BusinessRuleError("Import profile is inactive.")
    extension = os.path.splitext(filename)[1].lower()
    if extension not in settings.LEDGERGUARD["ALLOWED_IMPORT_EXTENSIONS"]:
        raise BusinessRuleError(f"Unsupported file type {extension or '(none)'}. Accepted: .csv, .xlsx")
    try:
        inspect_file(data, extension, settings.LEDGERGUARD["MAX_UPLOAD_BYTES"])
    except FileRejected as exc:
        raise BusinessRuleError(str(exc)) from exc
    sha256 = hashlib.sha256(data).hexdigest()
    previous = ImportBatch.objects.filter(
        institution=institution, sha256=sha256, status=ImportBatch.Status.COMMITTED
    ).first()
    if previous:
        raise BusinessRuleError(
            f"This exact file was already imported in batch #{previous.pk} on {previous.committed_at:%Y-%m-%d}.",
            code="duplicate_file",
        )
    batch = ImportBatch(
        institution=institution,
        profile=profile,
        original_filename=os.path.basename(filename)[:255],
        extension=extension,
        sha256=sha256,
        size=len(data),
        uploaded_by=user,
    )
    batch.file.save(f"{sha256}{extension}", ContentFile(data), save=False)
    batch.save()
    log_activity("import.uploaded", user=user, obj=batch, details={"sha256": sha256, "size": len(data)})
    return batch


def preview(batch: ImportBatch, limit: int = 20):
    data = _file_bytes(batch)
    try:
        headers, rows = read_table(data, batch.extension, batch.profile.header_row, batch.profile.sheet_name)
    except FileRejected as exc:
        raise BusinessRuleError(str(exc)) from exc
    return headers, rows[:limit], len(rows)


def _evaluate(batch: ImportBatch, raw: dict, places: int):
    normalized, errors = normalize_row(raw, batch.profile, places)
    return normalized, errors


def validate_batch(batch: ImportBatch, user) -> ImportBatch:
    """Parse, normalise and validate every row; flag duplicates. Re-runnable until commit."""
    require_perm(user, batch.institution, "import.upload")
    if batch.status not in (ImportBatch.Status.UPLOADED, ImportBatch.Status.VALIDATED, ImportBatch.Status.FAILED):
        raise BusinessRuleError(f"Batch in status {batch.get_status_display()} cannot be validated.")
    profile = batch.profile
    validate_profile_mapping(profile)
    data = _file_bytes(batch)
    try:
        headers, rows = read_table(
            data,
            batch.extension,
            profile.header_row,
            profile.sheet_name,
            max_rows=settings.LEDGERGUARD["MAX_IMPORT_ROWS"],
        )
    except FileRejected as exc:
        batch.status = ImportBatch.Status.FAILED
        batch.error_message = str(exc)
        batch.save()
        raise BusinessRuleError(str(exc)) from exc

    missing = [col for col in (profile.column_mapping or {}).values() if col and col not in headers]
    if missing:
        batch.status = ImportBatch.Status.FAILED
        batch.error_message = f"Mapped columns not found in file: {', '.join(missing)}"
        batch.save()
        raise BusinessRuleError(batch.error_message)

    with transaction.atomic():
        batch.rows.all().delete()
        row_objs = [_build_row(batch, row_number, raw) for row_number, raw in rows]
        _assign_fingerprints(batch, row_objs)
        ImportRow.objects.bulk_create(row_objs, batch_size=2000)
        _refresh_counts(batch)
        batch.status = ImportBatch.Status.VALIDATED
        batch.validated_at = timezone.now()
        batch.error_message = ""
        batch.save()
    log_activity("import.validated", user=user, obj=batch, details=batch.summary)
    return batch


def _build_row(batch, row_number, raw) -> ImportRow:
    normalized, errors = _evaluate(batch, raw, batch.institution.amount_decimal_places)
    return ImportRow(
        batch=batch,
        row_number=row_number,
        raw=raw,
        normalized=to_jsonable(normalized),
        status=ImportRow.Status.INVALID if errors else ImportRow.Status.VALID,
        errors=errors,
    )


def _assign_fingerprints(batch: ImportBatch, rows: list[ImportRow]):
    """Compute duplicate fingerprints across the file and against committed data."""
    occurrences: Counter = Counter()
    candidates = []
    for row in rows:
        if row.status in (ImportRow.Status.INVALID, ImportRow.Status.EXCLUDED):
            row.fingerprint = ""
            continue
        key = content_key(batch.institution_id, batch.profile.source_system, from_jsonable(row.normalized))
        occurrences[key] += 1
        row.fingerprint = fingerprint(key, occurrences[key])
        row.warnings = []
        row.status = ImportRow.Status.VALID
        if occurrences[key] > 1:
            row.status = ImportRow.Status.WARNING
            row.warnings = [f"Identical to an earlier row in this file (occurrence {occurrences[key]})."]
        candidates.append(row)
    existing = set(
        SourceTransaction.objects.filter(
            institution=batch.institution, fingerprint__in=[r.fingerprint for r in candidates]
        ).values_list("fingerprint", flat=True)
    )
    for row in candidates:
        if row.fingerprint in existing:
            row.status = ImportRow.Status.DUPLICATE
            row.warnings = ["Already imported previously; will not be imported again."]


def _refresh_counts(batch: ImportBatch):
    counts = Counter(batch.rows.values_list("status", flat=True))
    batch.total_rows = sum(counts.values())
    batch.valid_rows = counts[ImportRow.Status.VALID] + counts[ImportRow.Status.WARNING]
    batch.warning_rows = counts[ImportRow.Status.WARNING]
    batch.invalid_rows = counts[ImportRow.Status.INVALID]
    batch.duplicate_rows = counts[ImportRow.Status.DUPLICATE]
    batch.committed_rows = counts[ImportRow.Status.COMMITTED]
    batch.summary = {k: v for k, v in counts.items()}


def correct_row(row: ImportRow, user, changes: dict, note: str) -> ImportRow:
    """Correct raw source values for one row before commit; the original values are kept in the log."""
    batch = row.batch
    require_perm(user, batch.institution, "import.upload")
    if batch.status != ImportBatch.Status.VALIDATED:
        raise BusinessRuleError("Rows can only be corrected on a validated, uncommitted batch.")
    if not note or not note.strip():
        raise BusinessRuleError("A correction note is required.")
    unknown = set(changes) - set(row.raw)
    if unknown:
        raise BusinessRuleError(f"Unknown columns: {', '.join(sorted(unknown))}")
    before = {k: row.raw.get(k) for k in changes}
    with transaction.atomic():
        row.raw = {**row.raw, **changes}
        normalized, errors = _evaluate(batch, row.raw, batch.institution.amount_decimal_places)
        row.normalized = to_jsonable(normalized)
        row.errors = errors
        row.status = ImportRow.Status.INVALID if errors else ImportRow.Status.VALID
        row.corrected_by = user
        row.corrected_at = timezone.now()
        row.correction_note = note
        row.save()
        all_rows = list(batch.rows.exclude(status=ImportRow.Status.EXCLUDED))
        _assign_fingerprints(batch, all_rows)
        ImportRow.objects.bulk_update(all_rows, ["fingerprint", "status", "warnings"])
        _refresh_counts(batch)
        batch.save()
    log_activity(
        "import.row_corrected",
        user=user,
        obj=batch,
        details={"row": row.row_number, "before": before, "after": changes, "note": note},
    )
    row.refresh_from_db()
    return row


def set_row_excluded(row: ImportRow, user, excluded: bool, reason: str = "") -> ImportRow:
    batch = row.batch
    require_perm(user, batch.institution, "import.upload")
    if batch.status != ImportBatch.Status.VALIDATED:
        raise BusinessRuleError("Rows can only be excluded on a validated, uncommitted batch.")
    if excluded and not reason.strip():
        raise BusinessRuleError("A reason is required to exclude a row.")
    with transaction.atomic():
        if excluded:
            row.status = ImportRow.Status.EXCLUDED
            row.correction_note = reason
            row.save()
        else:
            row.status = ImportRow.Status.VALID if not row.errors else ImportRow.Status.INVALID
            row.save()
        all_rows = list(batch.rows.exclude(status=ImportRow.Status.EXCLUDED))
        _assign_fingerprints(batch, all_rows)
        ImportRow.objects.bulk_update(all_rows, ["fingerprint", "status", "warnings"])
        _refresh_counts(batch)
        batch.save()
    log_activity(
        "import.row_excluded" if excluded else "import.row_included",
        user=user,
        obj=batch,
        details={"row": row.row_number, "reason": reason},
    )
    return row


def commit_batch(batch: ImportBatch, user, allow_partial: bool = False) -> ImportBatch:
    """
    Commit valid rows as SourceTransactions. Invalid rows block the commit unless
    ``allow_partial`` is set, in which case they remain recorded on the batch as
    not imported. Previously imported rows are never duplicated.
    """
    require_perm(user, batch.institution, "import.upload")
    with transaction.atomic():
        batch = ImportBatch.objects.select_for_update().select_related("profile", "institution").get(pk=batch.pk)
        if batch.status != ImportBatch.Status.VALIDATED:
            raise BusinessRuleError(f"Batch in status {batch.get_status_display()} cannot be committed.")
        if batch.invalid_rows and not allow_partial:
            raise BusinessRuleError(
                f"{batch.invalid_rows} row(s) are invalid. Correct or exclude them, or commit valid rows only."
            )
        rows = list(batch.rows.filter(status__in=[ImportRow.Status.VALID, ImportRow.Status.WARNING]))
        profile = batch.profile
        txns = []
        for row in rows:
            n = from_jsonable(row.normalized)
            txns.append(
                SourceTransaction(
                    institution=batch.institution,
                    batch=batch,
                    import_row=row,
                    source_type=profile.source_type,
                    source_system=profile.source_system,
                    source_file=batch.original_filename,
                    source_row_number=row.row_number,
                    account_reference=n["account_reference"],
                    transaction_id=n["transaction_id"],
                    external_reference=n["external_reference"],
                    norm_transaction_id=n["norm_transaction_id"],
                    norm_external_reference=n["norm_external_reference"],
                    transaction_date=n["transaction_date"],
                    value_date=n.get("value_date"),
                    posting_date=n.get("posting_date"),
                    debit_amount=n["debit_amount"],
                    credit_amount=n["credit_amount"],
                    signed_amount=n["signed_amount"],
                    currency=n["currency"],
                    narration=n["narration"],
                    norm_narration=n["norm_narration"],
                    counterparty_reference=n["counterparty_reference"],
                    channel=n["channel"],
                    settlement_batch_ref=n["settlement_batch_ref"],
                    gross_amount=n.get("gross_amount"),
                    fee_amount=n.get("fee_amount"),
                    tax_amount=n.get("tax_amount"),
                    net_amount=n.get("net_amount"),
                    settlement_date=n.get("settlement_date"),
                    is_reversal=n.get("is_reversal", False),
                    source_values=n.get("source_values", {}),
                    fingerprint=row.fingerprint,
                    is_suspected_duplicate=row.status == ImportRow.Status.WARNING,
                )
            )
        try:
            with transaction.atomic():
                SourceTransaction.objects.bulk_create(txns, batch_size=2000)
        except IntegrityError as exc:
            raise BusinessRuleError(
                "Some rows were imported by another batch in the meantime. Re-validate the batch and try again."
            ) from exc
        batch.rows.filter(status__in=[ImportRow.Status.VALID, ImportRow.Status.WARNING]).update(
            status=ImportRow.Status.COMMITTED
        )
        # Validation counts are kept as the import summary; record what was committed.
        batch.committed_rows = len(rows)
        batch.summary = {**batch.summary, "COMMITTED": len(rows)}
        batch.status = ImportBatch.Status.COMMITTED
        batch.committed_by = user
        batch.committed_at = timezone.now()
        try:
            with transaction.atomic():
                batch.save()
        except IntegrityError as exc:
            raise BusinessRuleError("This exact file has already been committed in another batch.") from exc
    log_activity("import.committed", user=user, obj=batch, details=batch.summary)
    return batch


def cancel_batch(batch: ImportBatch, user, reason: str) -> ImportBatch:
    require_perm(user, batch.institution, "import.upload")
    if batch.status == ImportBatch.Status.COMMITTED:
        raise BusinessRuleError("Committed batches cannot be cancelled.")
    batch.status = ImportBatch.Status.CANCELLED
    batch.error_message = reason
    batch.save()
    log_activity("import.cancelled", user=user, obj=batch, details={"reason": reason})
    return batch


def purge_expired_files(institution, now=None) -> int:
    """Remove stored source files older than the retention period; checksum and metadata are retained."""
    now = now or timezone.now()
    years = institution.document_retention_years
    try:
        cutoff = now.replace(year=now.year - years)
    except ValueError:  # 29 February
        cutoff = now.replace(year=now.year - years, day=28)
    purged = 0
    for batch in ImportBatch.objects.filter(
        institution=institution, created_at__lt=cutoff, file_purged_at__isnull=True
    ):
        if batch.file:
            batch.file.delete(save=False)
        batch.file = None
        batch.file_purged_at = now
        batch.save(update_fields=["file", "file_purged_at", "updated_at"])
        log_activity("import.file_purged", institution=institution, obj=batch, details={"sha256": batch.sha256})
        purged += 1
    return purged
