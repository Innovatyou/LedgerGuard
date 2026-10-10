from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models

from apps.core.models import TimeStampedModel

from .permissions import validate_codes


class User(AbstractUser):
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=30, blank=True)
    job_title = models.CharField(max_length=120, blank=True)
    mfa_enabled = models.BooleanField(default=False)
    mfa_secret = models.CharField(max_length=64, blank=True)
    mfa_last_used_step = models.BigIntegerField(null=True, blank=True)
    must_change_password = models.BooleanField(default=False)
    password_changed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.get_full_name() or self.username


class Role(TimeStampedModel):
    """
    A named bundle of permission codes. System roles have no institution and are
    available everywhere; institutions can add their own roles.
    """

    institution = models.ForeignKey(
        "organizations.Institution", null=True, blank=True, on_delete=models.CASCADE, related_name="roles"
    )
    code = models.SlugField(max_length=50)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    permissions = models.JSONField(default=list)
    is_system = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["institution", "code"], name="uniq_role_code_per_institution"),
            models.UniqueConstraint(
                fields=["code"], condition=models.Q(institution__isnull=True), name="uniq_system_role_code"
            ),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        try:
            self.permissions = validate_codes(self.permissions)
        except ValueError as exc:
            raise ValidationError({"permissions": str(exc)}) from exc


class Membership(TimeStampedModel):
    """Grants a user a role within an institution, optionally limited to one branch."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    institution = models.ForeignKey("organizations.Institution", on_delete=models.CASCADE, related_name="memberships")
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="memberships")
    branch = models.ForeignKey(
        "organizations.Branch",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        help_text="Leave blank for access to all branches.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "institution", "role"], name="uniq_membership_role"),
        ]

    def __str__(self):
        return f"{self.user} @ {self.institution} ({self.role})"

    def clean(self):
        if self.role_id and self.role.institution_id not in (None, self.institution_id):
            raise ValidationError({"role": "Role belongs to a different institution."})
        if self.branch_id and self.branch.institution_id != self.institution_id:
            raise ValidationError({"branch": "Branch belongs to a different institution."})
