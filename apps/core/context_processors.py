from apps.accounts.services import get_permissions

from .tenancy import institutions_for_user


def ledgerguard(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    institution = getattr(request, "institution", None)
    unread = 0
    if institution is not None:
        from apps.notifications.models import Notification

        unread = Notification.objects.filter(user=user, institution=institution, read_at__isnull=True).count()
    return {
        "active_institution": institution,
        "available_institutions": institutions_for_user(user).order_by("name"),
        "perms_lg": get_permissions(user, institution),
        "unread_notifications": unread,
    }
