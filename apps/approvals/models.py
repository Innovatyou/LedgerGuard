from django.conf import settings
from django.db import models


class ApprovalRecord(models.Model):
    """
    Immutable evidence of a workflow decision (submit, approve, reject, post,
    reopen ...) on any controlled record.
    """

    class Decision(models.TextChoices):
        SUBMITTED = "SUBMITTED", "Submitted"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        POSTED = "POSTED", "Posted"
        REOPENED = "REOPENED", "Reopened"
        CLOSED = "CLOSED", "Closed"
        REVERSAL_REQUESTED = "REVERSAL_REQUESTED", "Reversal requested"

    institution = models.ForeignKey("organizations.Institution", on_delete=models.PROTECT, related_name="+")
    object_type = models.CharField(max_length=60)
    object_id = models.CharField(max_length=64)
    decision = models.CharField(max_length=30, choices=Decision.choices)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    comment = models.TextField(blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["timestamp", "id"]
        indexes = [models.Index(fields=["institution", "object_type", "object_id"])]

    def __str__(self):
        return f"{self.object_type}#{self.object_id} {self.decision} by {self.user}"


class ChangeRequest(models.Model):
    """
    A proposed change to controlled configuration (chart of accounts, matching
    rules). The change is applied only when a second, authorised user approves it.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending approval"
        APPROVED = "APPROVED", "Approved and applied"
        REJECTED = "REJECTED", "Rejected"
        CANCELLED = "CANCELLED", "Withdrawn"

    institution = models.ForeignKey("organizations.Institution", on_delete=models.PROTECT, related_name="+")
    reference = models.CharField(max_length=30)
    target_type = models.CharField(max_length=40)
    target_id = models.CharField(max_length=64, blank=True, help_text="Blank when the change creates a new record")
    summary = models.CharField(max_length=255)
    payload = models.JSONField(default=dict)
    before = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    requested_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_comment = models.TextField(blank=True)
    applied_object_id = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["-requested_at"]
        constraints = [
            models.UniqueConstraint(fields=["institution", "reference"], name="uniq_change_request_reference")
        ]

    def __str__(self):
        return f"{self.reference} {self.summary}"
