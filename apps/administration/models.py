from django.db import models


class BackupRecord(models.Model):
    """Platform-level record of a database or media backup (not institution-scoped)."""

    class Kind(models.TextChoices):
        DATABASE = "DATABASE", "Database"
        MEDIA = "MEDIA", "Uploaded files"

    class Status(models.TextChoices):
        CREATED = "CREATED", "Created"
        VERIFIED = "VERIFIED", "Verified"
        FAILED = "FAILED", "Failed"
        PRUNED = "PRUNED", "Removed by retention"

    kind = models.CharField(max_length=10, choices=Kind.choices)
    path = models.CharField(max_length=500)
    size = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices)
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class IntegrityCheck(models.Model):
    """Result of an automated ledger integrity check."""

    institution = models.ForeignKey("organizations.Institution", on_delete=models.CASCADE, related_name="+")
    ran_at = models.DateTimeField(auto_now_add=True)
    passed = models.BooleanField()
    findings = models.JSONField(default=list)

    class Meta:
        ordering = ["-ran_at"]
