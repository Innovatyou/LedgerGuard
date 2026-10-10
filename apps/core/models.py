import hashlib

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from .exceptions import TenantViolationError


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class InstitutionScopedQuerySet(models.QuerySet):
    def for_institution(self, institution):
        if institution is None:
            return self.none()
        return self.filter(institution=institution)


class InstitutionScopedModel(TimeStampedModel):
    """Base class for every record that belongs to a single institution."""

    institution = models.ForeignKey(
        "organizations.Institution", on_delete=models.PROTECT, related_name="+", db_index=True
    )

    objects = InstitutionScopedQuerySet.as_manager()

    class Meta:
        abstract = True

    def assert_same_institution(self, *others):
        for other in others:
            if other is not None and getattr(other, "institution_id", None) != self.institution_id:
                raise TenantViolationError(f"{other.__class__.__name__} belongs to a different institution.")


class ActivityLog(models.Model):
    """
    Append-only record of significant user and system actions.

    On PostgreSQL, a trigger installed by migration 0002 rejects UPDATE and
    DELETE statements against this table, so the log cannot be altered through
    the application or ad-hoc SQL issued by the application role.
    """

    institution = models.ForeignKey(
        "organizations.Institution", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    action = models.CharField(max_length=80, db_index=True)
    object_type = models.CharField(max_length=80, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    object_repr = models.CharField(max_length=255, blank=True)
    details = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-timestamp", "-id"]
        indexes = [models.Index(fields=["institution", "object_type", "object_id"])]

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d %H:%M} {self.action} {self.object_repr}"

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Activity log entries are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Activity log entries cannot be deleted.")


def evidence_upload_path(instance, filename):
    return f"evidence/{instance.institution_id}/{instance.sha256[:2]}/{instance.sha256}_{filename}"


class Attachment(InstitutionScopedModel):
    """Supporting evidence (documents, schedules, screenshots) attached to any record."""

    file = models.FileField(upload_to=evidence_upload_path)
    original_name = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64, db_index=True)
    size = models.PositiveBigIntegerField()
    description = models.CharField(max_length=255, blank=True)
    content_type_label = models.CharField(max_length=80)
    object_id = models.CharField(max_length=64)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        indexes = [models.Index(fields=["institution", "content_type_label", "object_id"])]

    def __str__(self):
        return self.original_name

    @staticmethod
    def checksum(django_file) -> str:
        digest = hashlib.sha256()
        for chunk in django_file.chunks():
            digest.update(chunk)
        django_file.seek(0)
        return digest.hexdigest()


from .sequences import DocumentSequence  # noqa: E402,F401  (registered for migrations)
