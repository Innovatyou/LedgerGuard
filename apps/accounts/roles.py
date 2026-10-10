from .permissions import DEFAULT_ROLES


def sync_system_roles(Role):
    """Create or update the built-in system roles. ``Role`` may be a historical model."""
    for code, (name, codes) in DEFAULT_ROLES.items():
        Role.objects.update_or_create(
            institution=None,
            code=code,
            defaults={"name": name, "permissions": sorted(codes), "is_system": True},
        )
