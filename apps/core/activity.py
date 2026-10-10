from decimal import Decimal

from .context import current_ip
from .models import ActivityLog


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def log_activity(action, *, user=None, institution=None, obj=None, details=None) -> ActivityLog:
    if institution is None and obj is not None:
        institution = getattr(obj, "institution", None)
    return ActivityLog.objects.create(
        institution=institution,
        user=user if (user is not None and user.is_authenticated) else None,
        action=action,
        object_type=obj.__class__.__name__ if obj is not None else "",
        object_id=str(obj.pk) if obj is not None and obj.pk is not None else "",
        object_repr=str(obj)[:255] if obj is not None else "",
        details=_jsonable(details or {}),
        ip_address=current_ip(),
    )
