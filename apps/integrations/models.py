from django.conf import settings
from django.db import models

from apps.core.models import InstitutionScopedModel


class IntegrationEndpoint(InstitutionScopedModel):
    """
    An authorised inbound data interface. Each endpoint feeds one import profile
    and acts as a named service user, so its uploads go through the same
    validation, duplicate checks and audit trail as manual imports.
    """

    class Kind(models.TextChoices):
        API_UPLOAD = "API_UPLOAD", "HTTPS file upload API"
        DROP_FOLDER = "DROP_FOLDER", "Server drop folder (e.g. SFTP target)"

    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=12, choices=Kind.choices)
    import_profile = models.ForeignKey("imports.ImportProfile", on_delete=models.PROTECT, related_name="+")
    service_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="Uploads are recorded as this user; it needs the 'import.upload' permission.",
    )
    key_prefix = models.CharField(max_length=16, blank=True, db_index=True)
    key_hash = models.CharField(max_length=64, blank=True)
    allowed_ips = models.JSONField(default=list, blank=True, help_text="Empty: any address (not recommended)")
    folder = models.CharField(max_length=120, blank=True, help_text="Sub-folder of LEDGERGUARD_INGEST_ROOT")
    auto_commit = models.BooleanField(
        default=False, help_text="Commit automatically when validation finds no invalid rows"
    )
    is_active = models.BooleanField(default=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["institution", "name"], name="uniq_endpoint_name")]

    def __str__(self):
        return self.name


class IntegrationLog(models.Model):
    class Status(models.TextChoices):
        SUCCESS = "SUCCESS", "Accepted"
        REJECTED = "REJECTED", "Rejected"
        ERROR = "ERROR", "Error"

    endpoint = models.ForeignKey(IntegrationEndpoint, on_delete=models.CASCADE, related_name="logs")
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    status = models.CharField(max_length=10, choices=Status.choices)
    message = models.TextField(blank=True)
    filename = models.CharField(max_length=255, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    remote_ip = models.GenericIPAddressField(null=True, blank=True)
    batch = models.ForeignKey("imports.ImportBatch", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-timestamp"]
