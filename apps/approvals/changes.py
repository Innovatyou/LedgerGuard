"""
Maker-checker control over configuration changes.

Apps register *targets* (e.g. chart-of-accounts accounts, matching rules,
institution settings). A change is captured as a serialised payload and only
applied when a second user holding ``config.approve`` approves it.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from apps.accounts.services import require_perm
from apps.core.activity import log_activity
from apps.core.exceptions import BusinessRuleError
from apps.core.sequences import next_number

from .models import ChangeRequest
from .services import assert_independent


@dataclass
class Target:
    key: str
    label: str
    model: type
    request_permission: str
    exclude: tuple = ()


TARGETS: dict[str, Target] = {}


def register(key, label, model, request_permission, exclude=()):
    TARGETS[key] = Target(key, label, model, request_permission, tuple(exclude))


def _jsonable(value):
    if isinstance(value, models.Model):
        return value.pk
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date | datetime):
        return value.isoformat()
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if hasattr(value, "all") and callable(value.all):  # querysets / m2m
        return [v.pk for v in value]
    return value


def serialize(data: dict) -> dict:
    return {k: _jsonable(v) for k, v in data.items()}


def snapshot(obj, fields) -> dict:
    out = {}
    for name in fields:
        field = obj._meta.get_field(name)
        out[name] = getattr(obj, field.attname)
    return serialize(out)


def build(target: Target, institution, target_id, payload, requested_by):
    """Instantiate (but do not save) the object the change would produce."""
    model = target.model
    if target_id:
        if model._meta.model_name == "institution":
            obj = model.objects.get(pk=institution.pk)
        else:
            obj = model.objects.get(institution=institution, pk=target_id)
    else:
        obj = model(institution=institution)
        if hasattr(obj, "created_by_id") and obj.created_by_id is None:
            obj.created_by = requested_by
    for name, value in payload.items():
        field = model._meta.get_field(name)
        if field.is_relation:
            setattr(obj, field.attname, value)
        else:
            setattr(obj, name, field.to_python(value))
    return obj


def _validate(obj):
    try:
        obj.full_clean(exclude=["created_by", "approved_by", "updated_by"])
    except ValidationError as exc:
        raise BusinessRuleError("; ".join(f"{k}: {', '.join(v)}" for k, v in exc.message_dict.items())) from exc


def request_change(institution, user, target_key, payload: dict, target_obj=None, summary="") -> ChangeRequest:
    target = TARGETS[target_key]
    require_perm(user, institution, target.request_permission)
    payload = {k: v for k, v in serialize(payload).items() if k not in target.exclude}
    if target_obj is not None:
        # Model forms write cleaned values onto their instance; compare against the stored record instead.
        target_obj = type(target_obj).objects.get(pk=target_obj.pk)
    target_id = str(target_obj.pk) if target_obj is not None else ""
    before = snapshot(target_obj, payload.keys()) if target_obj is not None else {}
    if target_obj is not None:
        payload = {k: v for k, v in payload.items() if before.get(k) != v}
        if not payload:
            raise BusinessRuleError("No changes were made.")
    if (
        ChangeRequest.objects.filter(
            institution=institution, target_type=target_key, target_id=target_id, status=ChangeRequest.Status.PENDING
        )
        .exclude(target_id="")
        .exists()
    ):
        raise BusinessRuleError("A change to this record is already awaiting approval.")
    _validate(build(target, institution, target_id, payload, user))  # fail early on invalid changes
    with transaction.atomic():
        cr = ChangeRequest.objects.create(
            institution=institution,
            reference=next_number(institution, "change", "CHG-"),
            target_type=target_key,
            target_id=target_id,
            summary=(summary or f"{'Edit' if target_id else 'Create'} {target.label}")[:255],
            payload=payload,
            before={k: before.get(k) for k in payload} if before else {},
            requested_by=user,
        )
    log_activity("config.change_requested", user=user, institution=institution, obj=cr, details={"payload": payload})
    return cr


def approve_change(cr: ChangeRequest, user, comment=""):
    require_perm(user, cr.institution, "config.approve")
    with transaction.atomic():
        cr = ChangeRequest.objects.select_for_update().select_related("institution").get(pk=cr.pk)
        if cr.status != ChangeRequest.Status.PENDING:
            raise BusinessRuleError("This change request is no longer pending.")
        assert_independent(cr.institution, cr.requested_by, user, "this change")
        target = TARGETS[cr.target_type]
        obj = build(target, cr.institution, cr.target_id, cr.payload, cr.requested_by)
        if hasattr(obj, "approved_by_id"):
            obj.approved_by = user
            obj.approved_at = timezone.now()
        if hasattr(obj, "updated_by_id"):
            obj.updated_by = cr.requested_by
        _validate(obj)
        try:
            with transaction.atomic():
                obj.save()
        except (IntegrityError, ValidationError) as exc:
            raise BusinessRuleError(f"The change could not be applied: {exc}") from exc
        cr.status = ChangeRequest.Status.APPROVED
        cr.decided_by = user
        cr.decided_at = timezone.now()
        cr.decision_comment = comment
        cr.applied_object_id = str(obj.pk)
        cr.save()
    log_activity(
        "config.change_approved",
        user=user,
        institution=cr.institution,
        obj=cr,
        details={"target": cr.target_type, "object": obj.pk, "payload": cr.payload},
    )
    return obj


def reject_change(cr: ChangeRequest, user, comment: str):
    if not comment.strip():
        raise BusinessRuleError("A reason is required.")
    with transaction.atomic():
        cr = ChangeRequest.objects.select_for_update().get(pk=cr.pk)
        if cr.status != ChangeRequest.Status.PENDING:
            raise BusinessRuleError("This change request is no longer pending.")
        if cr.requested_by_id == user.pk:
            cr.status = ChangeRequest.Status.CANCELLED
        else:
            require_perm(user, cr.institution, "config.approve")
            cr.status = ChangeRequest.Status.REJECTED
        cr.decided_by = user
        cr.decided_at = timezone.now()
        cr.decision_comment = comment
        cr.save()
    log_activity(
        f"config.change_{cr.status.lower()}",
        user=user,
        institution=cr.institution,
        obj=cr,
        details={"comment": comment},
    )
    return cr


def requires_approval(institution) -> bool:
    return institution.require_config_approval
