from decimal import Decimal

from django import template

from apps.core.attachments import attachments_for
from apps.core.money import format_money

register = template.Library()


@register.filter
def money(value):
    if value in (None, ""):
        return ""
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    text = format_money(abs(value))
    return f"({text})" if value < 0 else text


@register.filter
def has(perms, code):
    return code in (perms or ())


@register.filter
def get_item(mapping, key):
    try:
        return mapping.get(key)
    except AttributeError:
        return None


@register.filter
def pct(value, total):
    try:
        return int(Decimal(value) * 100 / Decimal(total)) if total else 0
    except Exception:  # noqa: BLE001
        return 0


@register.simple_tag
def evidence_for(obj, label):
    return attachments_for(obj, label)


@register.filter
def status_class(status):
    s = str(status).upper()
    if s in {
        "POSTED",
        "APPROVED",
        "COMMITTED",
        "RESOLVED",
        "CLOSED",
        "ACCEPTED",
        "VALID",
        "REVIEWED",
        "VERIFIED",
        "EXACT",
        "COMPLETED",
        "OPEN_PERIOD",
    }:
        return "ok"
    if s in {"REJECTED", "INVALID", "FAILED", "REVERSED", "HIGH", "DUPLICATE"}:
        return "bad"
    if s in {
        "SUBMITTED",
        "PENDING_APPROVAL",
        "IN_REVIEW",
        "PROPOSED",
        "MANUAL_REVIEW",
        "WARNING",
        "VALIDATED",
        "MEDIUM",
        "PENDING_VERIFICATION",
        "HIGH_CONFIDENCE",
        "REOPENED",
        "INVESTIGATING",
        "PROCESSING",
    }:
        return "warn"
    return "neutral"
