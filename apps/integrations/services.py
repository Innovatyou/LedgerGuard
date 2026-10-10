import hashlib
import hmac
import ipaddress
import logging
import secrets
import shutil
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from apps.accounts.services import has_perm, require_perm
from apps.core.activity import log_activity
from apps.core.exceptions import BusinessRuleError
from apps.imports import services as imports

from .models import IntegrationEndpoint, IntegrationLog

logger = logging.getLogger(__name__)
KEY_PREFIX = "lgk_"


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _validate_endpoint(institution, profile, service_user, kind, folder):
    if profile.institution_id != institution.pk:
        raise BusinessRuleError("Import profile belongs to a different institution.")
    if not has_perm(service_user, institution, "import.upload"):
        raise BusinessRuleError("The service user needs the 'import.upload' permission in this institution.")
    if kind == IntegrationEndpoint.Kind.DROP_FOLDER:
        if not folder or Path(folder).is_absolute() or ".." in Path(folder).parts:
            raise BusinessRuleError("Drop folder must be a relative sub-folder without '..'.")


def create_endpoint(
    institution, user, *, name, kind, import_profile, service_user, allowed_ips=(), folder="", auto_commit=False
):
    """Create an endpoint. For API endpoints the returned key is shown once and only its hash is stored."""
    require_perm(user, institution, "integration.manage")
    _validate_endpoint(institution, import_profile, service_user, kind, folder)
    for ip in allowed_ips:
        ipaddress.ip_network(ip, strict=False)
    endpoint = IntegrationEndpoint(
        institution=institution,
        name=name,
        kind=kind,
        import_profile=import_profile,
        service_user=service_user,
        allowed_ips=list(allowed_ips),
        folder=folder,
        auto_commit=auto_commit,
        created_by=user,
    )
    key = None
    if kind == IntegrationEndpoint.Kind.API_UPLOAD:
        prefix = secrets.token_hex(6)
        secret = secrets.token_urlsafe(32)
        endpoint.key_prefix = prefix
        endpoint.key_hash = _hash(secret)
        key = f"{KEY_PREFIX}{prefix}.{secret}"
    endpoint.save()
    log_activity(
        "integration.endpoint_created",
        user=user,
        obj=endpoint,
        details={"kind": kind, "allowed_ips": list(allowed_ips)},
    )
    return endpoint, key


def rotate_key(endpoint, user):
    require_perm(user, endpoint.institution, "integration.manage")
    if endpoint.kind != IntegrationEndpoint.Kind.API_UPLOAD:
        raise BusinessRuleError("Only API endpoints have keys.")
    secret = secrets.token_urlsafe(32)
    endpoint.key_prefix = secrets.token_hex(6)
    endpoint.key_hash = _hash(secret)
    endpoint.save(update_fields=["key_prefix", "key_hash", "updated_at"])
    log_activity("integration.key_rotated", user=user, obj=endpoint)
    return f"{KEY_PREFIX}{endpoint.key_prefix}.{secret}"


def authenticate(header: str, ip: str | None) -> IntegrationEndpoint:
    token = (header or "").removeprefix("Bearer ").strip()
    if not token.startswith(KEY_PREFIX) or "." not in token:
        raise BusinessRuleError("Missing or malformed integration key.", code="unauthorised")
    prefix, secret = token[len(KEY_PREFIX) :].split(".", 1)
    endpoint = (
        IntegrationEndpoint.objects.filter(key_prefix=prefix, kind=IntegrationEndpoint.Kind.API_UPLOAD)
        .select_related("institution", "import_profile", "service_user")
        .first()
    )
    if endpoint is None or not hmac.compare_digest(endpoint.key_hash, _hash(secret)):
        raise BusinessRuleError("Invalid integration key.", code="unauthorised")
    if not endpoint.is_active or not endpoint.institution.is_active:
        _log(endpoint, IntegrationLog.Status.REJECTED, "Endpoint disabled", ip=ip)
        raise BusinessRuleError("Endpoint is disabled.", code="forbidden")
    if endpoint.allowed_ips and not _ip_allowed(ip, endpoint.allowed_ips):
        _log(endpoint, IntegrationLog.Status.REJECTED, f"Address {ip} not on allow-list", ip=ip)
        raise BusinessRuleError("Source address not permitted.", code="forbidden")
    return endpoint


def _ip_allowed(ip, networks):
    try:
        address = ipaddress.ip_address(ip)
    except (TypeError, ValueError):
        return False
    return any(address in ipaddress.ip_network(n, strict=False) for n in networks)


def _log(endpoint, status, message, *, filename="", data=b"", ip=None, batch=None):
    return IntegrationLog.objects.create(
        endpoint=endpoint,
        status=status,
        message=message[:2000],
        filename=filename[:255],
        sha256=hashlib.sha256(data).hexdigest() if data else "",
        remote_ip=ip,
        batch=batch,
    )


def ingest(endpoint: IntegrationEndpoint, filename: str, data: bytes, ip=None) -> dict:
    """Run an inbound file through upload → validate → (optional) commit and log the outcome."""
    user, institution = endpoint.service_user, endpoint.institution
    try:
        batch = imports.create_batch(institution, user, endpoint.import_profile, filename, data)
        batch = imports.validate_batch(batch, user)
        batch.refresh_from_db()
        committed = False
        if endpoint.auto_commit and batch.invalid_rows == 0:
            batch = imports.commit_batch(batch, user)
            committed = True
    except BusinessRuleError as exc:
        _log(endpoint, IntegrationLog.Status.REJECTED, exc.message, filename=filename, data=data, ip=ip)
        log_activity(
            "integration.rejected",
            institution=institution,
            obj=endpoint,
            details={"file": filename, "reason": exc.message},
        )
        raise
    endpoint.last_used_at = timezone.now()
    endpoint.save(update_fields=["last_used_at", "updated_at"])
    summary = {
        "batch": batch.pk,
        "status": batch.status,
        "rows": batch.total_rows,
        "valid": batch.valid_rows,
        "invalid": batch.invalid_rows,
        "duplicates": batch.duplicate_rows,
        "committed": committed,
    }
    _log(endpoint, IntegrationLog.Status.SUCCESS, str(summary), filename=filename, data=data, ip=ip, batch=batch)
    log_activity("integration.received", institution=institution, obj=endpoint, details=summary)
    return summary


def ingest_root() -> Path:
    return Path(settings.LEDGERGUARD["INGEST_ROOT"])


def process_drop_folders() -> dict:
    """Process *.csv/*.xlsx files in every active drop-folder endpoint; move them to processed/ or failed/."""
    root = ingest_root().resolve()
    counts = {"processed": 0, "failed": 0}
    for endpoint in IntegrationEndpoint.objects.filter(
        kind=IntegrationEndpoint.Kind.DROP_FOLDER, is_active=True
    ).select_related("institution", "import_profile", "service_user"):
        folder = (root / endpoint.institution.code / endpoint.folder).resolve()
        if root not in folder.parents:
            logger.error("Drop folder for endpoint %s escapes the ingest root", endpoint.pk)
            continue
        folder.mkdir(parents=True, exist_ok=True)
        for sub in ("processed", "failed"):
            (folder / sub).mkdir(exist_ok=True)
        for path in sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in (".csv", ".xlsx")):
            stamp = timezone.now().strftime("%Y%m%dT%H%M%S")
            try:
                ingest(endpoint, path.name, path.read_bytes())
                target = folder / "processed" / f"{stamp}_{path.name}"
                counts["processed"] += 1
            except BusinessRuleError:
                target = folder / "failed" / f"{stamp}_{path.name}"
                counts["failed"] += 1
            shutil.move(str(path), target)
    return counts
