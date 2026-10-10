"""
Server-side institution (tenant) resolution.

Every institution-scoped query in views and APIs must be filtered with the
institution returned here. The requested institution (session value or the
``X-Institution`` API header) is only honoured when the user holds an active
membership in it.
"""

SESSION_KEY = "active_institution_id"
API_HEADER = "HTTP_X_INSTITUTION"


def institutions_for_user(user):
    from apps.organizations.models import Institution

    if not user or not user.is_authenticated or not user.is_active:
        return Institution.objects.none()
    if user.is_superuser:
        return Institution.objects.filter(is_active=True)
    return Institution.objects.filter(
        is_active=True,
        memberships__user=user,
        memberships__is_active=True,
    ).distinct()


def resolve_for_user(user, requested_id=None):
    allowed = institutions_for_user(user).order_by("name")
    if requested_id:
        try:
            return allowed.get(pk=int(requested_id))
        except (ValueError, TypeError, allowed.model.DoesNotExist):
            return None
    return allowed.first()


def resolve_institution(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return None
    requested = None
    session = getattr(request, "session", None)
    if session is not None:
        requested = session.get(SESSION_KEY)
    institution = resolve_for_user(user, requested) if requested else None
    if institution is None:
        institution = resolve_for_user(user)
        if session is not None and institution is not None:
            session[SESSION_KEY] = institution.pk
    return institution


def resolve_api_institution(request):
    """Resolve the institution for a DRF request (session or token auth)."""
    header = request.META.get(API_HEADER)
    if header:
        # An explicit header that the user may not access must fail closed.
        return resolve_for_user(request.user, header)
    return resolve_institution(request)
