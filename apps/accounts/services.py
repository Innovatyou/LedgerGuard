from apps.core.exceptions import PermissionDeniedError

from .models import Membership
from .permissions import ALL

_CACHE_ATTR = "_lg_perm_cache"


def get_permissions(user, institution) -> frozenset:
    """Effective permission codes for ``user`` in ``institution`` (cached per user object)."""
    if user is None or not user.is_authenticated or not user.is_active or institution is None:
        return frozenset()
    if user.is_superuser:
        return frozenset(ALL)
    cache = getattr(user, _CACHE_ATTR, None)
    if cache is None:
        cache = {}
        setattr(user, _CACHE_ATTR, cache)
    if institution.pk not in cache:
        codes = set()
        memberships = Membership.objects.filter(user=user, institution=institution, is_active=True).select_related(
            "role"
        )
        for membership in memberships:
            codes.update(membership.role.permissions)
        cache[institution.pk] = frozenset(codes)
    return cache[institution.pk]


def clear_permission_cache(user):
    if hasattr(user, _CACHE_ATTR):
        delattr(user, _CACHE_ATTR)


def has_perm(user, institution, code: str) -> bool:
    return code in get_permissions(user, institution)


def require_perm(user, institution, code: str):
    if not has_perm(user, institution, code):
        raise PermissionDeniedError(f"Permission required: {code}")


def branch_scope(user, institution):
    """
    Return ``None`` when the user may see all branches, otherwise the set of
    branch ids their memberships are restricted to.
    """
    if user.is_superuser:
        return None
    branches = set()
    for membership in Membership.objects.filter(user=user, institution=institution, is_active=True):
        if membership.branch_id is None:
            return None
        branches.add(membership.branch_id)
    return branches
