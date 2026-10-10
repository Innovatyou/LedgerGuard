import logging

from django.conf import settings
from django.core.mail import send_mail

from apps.accounts.models import Membership

from .models import Notification

logger = logging.getLogger(__name__)


def notify(institution, users, title, message="", link="", email=False):
    created = []
    for user in {u.pk: u for u in users if u is not None}.values():
        created.append(
            Notification.objects.create(institution=institution, user=user, title=title, message=message, link=link)
        )
        if email and user.email and getattr(settings, "EMAIL_HOST", None):
            try:
                send_mail(title, message, None, [user.email], fail_silently=False)
            except Exception:  # noqa: BLE001 - email is best-effort; the in-app notice exists
                logger.exception("Email notification failed for user %s", user.pk)
    return created


def users_with_permission(institution, code):
    memberships = Membership.objects.filter(
        institution=institution, is_active=True, user__is_active=True
    ).select_related("user", "role")
    return [m.user for m in memberships if code in m.role.permissions]
