from datetime import date

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import InstitutionScopedModel

MONEY = {"max_digits": 20, "decimal_places": 2}
DEFAULT_BUCKETS = [7, 30, 60, 90]


class AgingPolicy(InstitutionScopedModel):
    """Upper bounds (in days) of each aging bucket; the last bucket is 'over N days'."""

    bucket_bounds = models.JSONField(default=list)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["institution"], name="uniq_aging_policy")]

    def bounds(self):
        return sorted(int(b) for b in (self.bucket_bounds or DEFAULT_BUCKETS))


def bucket_labels(bounds):
    labels, lower = [], 0
    for upper in bounds:
        labels.append(f"{lower}–{upper} days")
        lower = upper + 1
    labels.append(f"Over {bounds[-1]} days")
    return labels


def bucket_index(age_days: int, bounds) -> int:
    for idx, upper in enumerate(bounds):
        if age_days <= upper:
            return idx
    return len(bounds)


class EscalationRule(InstitutionScopedModel):
    name = models.CharField(max_length=120)
    min_age_days = models.PositiveIntegerField()
    notify_permission = models.CharField(
        max_length=40, default="suspense.approve", help_text="Users holding this permission are notified."
    )
    notify_assignee = models.BooleanField(default=True)
    repeat_every_days = models.PositiveIntegerField(default=7)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["min_age_days"]

    def __str__(self):
        return self.name


class SuspenseItem(InstitutionScopedModel):
    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        INVESTIGATING = "INVESTIGATING", "Under investigation"
        PENDING_APPROVAL = "PENDING_APPROVAL", "Resolution pending approval"
        RESOLVED = "RESOLVED", "Resolved"

    class Resolution(models.TextChoices):
        CLEARED = "CLEARED", "Cleared to correct account"
        REFUNDED = "REFUNDED", "Refunded to originator"
        REVERSED = "REVERSED", "Reversed"
        MATCHED = "MATCHED", "Matched to counter-entry"
        WRITTEN_OFF = "WRITTEN_OFF", "Written off (approved)"
        OTHER = "OTHER", "Other"

    class Side(models.TextChoices):
        DEBIT = "DEBIT", "Debit"
        CREDIT = "CREDIT", "Credit"

    OPEN_STATUSES = (Status.OPEN, Status.INVESTIGATING, Status.PENDING_APPROVAL)

    reference = models.CharField(max_length=30, blank=True)
    account = models.ForeignKey("accounting.Account", on_delete=models.PROTECT, related_name="suspense_items")
    transaction_reference = models.CharField(max_length=120, blank=True)
    original_date = models.DateField()
    original_amount = models.DecimalField(**MONEY)
    side = models.CharField(max_length=6, choices=Side.choices)
    currency = models.CharField(max_length=3)
    source_system = models.CharField(max_length=60, blank=True)
    narration = models.TextField(blank=True)
    reason = models.TextField(help_text="Reason the item is in suspense")
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True)
    resolution_action = models.CharField(max_length=15, choices=Resolution.choices, blank=True)
    resolution_notes = models.TextField(blank=True)
    related_journal = models.ForeignKey(
        "accounting.Journal", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    source_transaction = models.ForeignKey(
        "imports.SourceTransaction", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reconciliation = models.ForeignKey(
        "reconciliation.Reconciliation", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    proposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    proposed_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    date_resolved = models.DateField(null=True, blank=True)
    last_escalated_at = models.DateField(null=True, blank=True)
    escalation_level = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["original_date", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["institution", "reference"], condition=~Q(reference=""), name="uniq_suspense_reference"
            ),
            models.CheckConstraint(condition=Q(original_amount__gt=0), name="suspense_amount_positive"),
        ]

    def __str__(self):
        return f"{self.reference} {self.transaction_reference}".strip()

    def age_days(self, as_of: date | None = None) -> int:
        end = self.date_resolved or as_of or date.today()
        return max((end - self.original_date).days, 0)


class SuspenseNote(models.Model):
    item = models.ForeignKey(SuspenseItem, on_delete=models.CASCADE, related_name="notes")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
