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
