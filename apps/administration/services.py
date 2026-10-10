"""Backups, integrity checks and system health for platform operators."""

import hashlib
import os
import shutil
import subprocess
import tarfile
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from apps.accounting.models import Journal, JournalLine, LedgerEntry
from apps.accounting.reports import trial_balance
from apps.core.activity import log_activity

from .models import BackupRecord, IntegrityCheck


class BackupError(RuntimeError):
    pass


def backup_dir() -> Path:
    path = Path(settings.LEDGERGUARD["BACKUP_DIR"])
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pg_env():
    db = settings.DATABASES["default"]
    env = {**os.environ, "PGPASSWORD": str(db.get("PASSWORD") or "")}
    args = [
        "-h",
        str(db.get("HOST") or "localhost"),
        "-p",
        str(db.get("PORT") or 5432),
        "-U",
        str(db.get("USER") or ""),
        "-d",
        db["NAME"],
    ]
    return env, args


def backup_database() -> BackupRecord:
    """pg_dump custom-format backup with SHA-256 checksum. The file is written with owner-only permissions."""
    if connection.vendor != "postgresql":
        raise BackupError("Database backups are supported for PostgreSQL only.")
    pg_dump = shutil.which("pg_dump")
    if pg_dump is None:
        raise BackupError("pg_dump is not installed on this host.")
    target = backup_dir() / f"ledgerguard-db-{timezone.now():%Y%m%dT%H%M%S}.dump"
    env, args = _pg_env()
    # Arguments come from settings, never from user input.
    result = subprocess.run(  # noqa: S603
        [pg_dump, "-Fc", "--no-owner", "-f", str(target), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        record = BackupRecord.objects.create(
            kind=BackupRecord.Kind.DATABASE,
            path=str(target),
            status=BackupRecord.Status.FAILED,
            message=result.stderr[-4000:],
        )
        log_activity("system.backup_failed", details={"path": str(target), "error": result.stderr[-500:]})
        raise BackupError(result.stderr.strip() or "pg_dump failed")
    os.chmod(target, 0o600)
    record = BackupRecord.objects.create(
        kind=BackupRecord.Kind.DATABASE,
        path=str(target),
        size=target.stat().st_size,
        sha256=_sha256(target),
        status=BackupRecord.Status.CREATED,
    )
    log_activity("system.backup_created", details={"path": str(target), "sha256": record.sha256})
    return record


def backup_media() -> BackupRecord:
    media = Path(settings.MEDIA_ROOT)
    target = backup_dir() / f"ledgerguard-media-{timezone.now():%Y%m%dT%H%M%S}.tar.gz"
    with tarfile.open(target, "w:gz") as tar:
        if media.exists():
            tar.add(media, arcname="media")
    os.chmod(target, 0o600)
    record = BackupRecord.objects.create(
        kind=BackupRecord.Kind.MEDIA,
        path=str(target),
        size=target.stat().st_size,
        sha256=_sha256(target),
        status=BackupRecord.Status.CREATED,
    )
    log_activity("system.media_backup_created", details={"path": str(target), "sha256": record.sha256})
    return record


def verify_backup(record: BackupRecord) -> BackupRecord:
    """Check the checksum and that the archive can be read (pg_restore --list / tar listing)."""
    path = Path(record.path)
    try:
        if not path.exists():
            raise BackupError("Backup file is missing.")
        if _sha256(path) != record.sha256:
            raise BackupError("Checksum mismatch – the backup file has changed or is corrupt.")
        if record.kind == BackupRecord.Kind.DATABASE:
            pg_restore = shutil.which("pg_restore")
            if pg_restore is None:
                raise BackupError("pg_restore is not installed on this host.")
            result = subprocess.run(  # noqa: S603
                [pg_restore, "--list", str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0 or "TABLE DATA" not in result.stdout:
                raise BackupError("pg_restore could not read the archive: " + result.stderr[-500:])
        else:
            with tarfile.open(path, "r:gz") as tar:
                tar.getmembers()
    except (BackupError, tarfile.TarError, OSError) as exc:
        record.status = BackupRecord.Status.FAILED
        record.message = str(exc)
        record.save()
        raise BackupError(str(exc)) from exc
    record.status = BackupRecord.Status.VERIFIED
    record.verified_at = timezone.now()
    record.save()
    return record


def prune_backups(days: int | None = None) -> int:
    days = days if days is not None else settings.LEDGERGUARD["BACKUP_RETENTION_DAYS"]
    cutoff = timezone.now() - timedelta(days=days)
    pruned = 0
    for record in BackupRecord.objects.filter(created_at__lt=cutoff).exclude(status=BackupRecord.Status.PRUNED):
        Path(record.path).unlink(missing_ok=True)
        record.status = BackupRecord.Status.PRUNED
        record.save(update_fields=["status"])
        pruned += 1
    return pruned


def check_integrity(institution) -> IntegrityCheck:
    """
    Independent checks of the posted ledger:
    * every posted/reversed journal has one ledger entry per line and balances in the ledger;
    * no ledger entry exists for a journal that is not posted;
    * ledger amounts equal journal line base amounts;
    * the trial balance balances.
    """
    findings = []
    posted = Journal.objects.filter(
        institution=institution, status__in=[Journal.Status.POSTED, Journal.Status.REVERSED]
    )
    line_counts = dict(JournalLine.objects.filter(journal__in=posted).values_list("journal").annotate(n=Count("id")))
    entry_stats = {
        row["journal"]: row
        for row in LedgerEntry.objects.filter(institution=institution)
        .values("journal")
        .annotate(n=Count("id"), dr=Sum("debit"), cr=Sum("credit"))
    }
    for journal_id, n_lines in line_counts.items():
        stats = entry_stats.get(journal_id)
        if stats is None or stats["n"] != n_lines:
            findings.append(f"Journal {journal_id}: {n_lines} lines but {stats['n'] if stats else 0} ledger entries.")
        elif stats["dr"] != stats["cr"]:
            findings.append(f"Journal {journal_id}: ledger debits {stats['dr']} ≠ credits {stats['cr']}.")
    orphan = (
        LedgerEntry.objects.filter(institution=institution)
        .exclude(journal__in=posted)
        .values_list("journal_id", flat=True)
        .distinct()
    )
    for journal_id in orphan:
        findings.append(f"Journal {journal_id} has ledger entries but is not in posted status.")
    mismatched = (
        LedgerEntry.objects.filter(institution=institution)
        .filter(~Q(debit=F("line__base_debit")) | ~Q(credit=F("line__base_credit")) | ~Q(account=F("line__account")))
        .count()
    )
    if mismatched:
        findings.append(f"{mismatched} ledger entr(ies) differ from their journal lines.")
    tb = trial_balance(institution, timezone.localdate() + timedelta(days=36500))
    if not tb.is_balanced:
        findings.append(f"Trial balance out of balance: {tb.total_debit} vs {tb.total_credit}.")
    check = IntegrityCheck.objects.create(institution=institution, passed=not findings, findings=findings)
    log_activity(
        "system.integrity_check", institution=institution, details={"passed": check.passed, "findings": findings[:20]}
    )
    return check


def health() -> dict:
    status = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        status["database"] = "ok"
        executor = MigrationExecutor(connection)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
        status["migrations"] = "ok" if not pending else f"{len(pending)} pending"
    except Exception as exc:  # noqa: BLE001
        status["database"] = f"error: {exc}"
    try:
        cache.set("lg:health", "1", 10)
        status["cache"] = "ok" if cache.get("lg:health") == "1" else "not persisting"
    except Exception as exc:  # noqa: BLE001
        status["cache"] = f"error: {exc}"
    try:
        from config.celery import app

        replies = app.control.ping(timeout=1.0)
        status["workers"] = f"{len(replies)} responding" if replies else "none responding"
    except Exception as exc:  # noqa: BLE001
        status["workers"] = f"unavailable ({exc.__class__.__name__})"
    for label, path in (("media", settings.MEDIA_ROOT), ("backups", settings.LEDGERGUARD["BACKUP_DIR"])):
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
            usage = shutil.disk_usage(path)
            status[f"disk_{label}"] = f"{usage.free / 1024**3:.1f} GB free of {usage.total / 1024**3:.1f} GB"
        except OSError as exc:
            status[f"disk_{label}"] = f"error: {exc}"
    last = (
        BackupRecord.objects.filter(kind=BackupRecord.Kind.DATABASE).exclude(status=BackupRecord.Status.FAILED).first()
    )
    status["last_database_backup"] = (
        f"{last.created_at:%Y-%m-%d %H:%M} ({last.get_status_display()})" if last else "never"
    )
    return status
