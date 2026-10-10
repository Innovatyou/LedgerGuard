from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import InstitutionScopedModel


class Engagement(InstitutionScopedModel):
    class Status(models.TextChoices):
        PLANNING = "PLANNING", "Planning"
        FIELDWORK = "FIELDWORK", "Fieldwork"
        REPORTING = "REPORTING", "Reporting"
        CLOSED = "CLOSED", "Closed"

    class Kind(models.TextChoices):
        INTERNAL = "INTERNAL", "Internal audit"
        EXTERNAL = "EXTERNAL", "External audit support"
        REGULATORY = "REGULATORY", "Regulatory examination support"

    reference = models.CharField(max_length=30, blank=True)
    title = models.CharField(max_length=200)
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.INTERNAL)
    scope = models.TextField()
    objectives = models.TextField(blank=True)
    period_start = models.DateField()
    period_end = models.DateField()
    planned_start = models.DateField(null=True, blank=True)
    planned_end = models.DateField(null=True, blank=True)
    branch = models.ForeignKey("organizations.Branch", null=True, blank=True, on_delete=models.PROTECT)
    lead = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    team = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="+")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PLANNING)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-period_end", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["institution", "reference"], condition=~Q(reference=""), name="uniq_engagement_reference"
            )
        ]

    def __str__(self):
        return f"{self.reference} {self.title}"


class Workpaper(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "In preparation"
        SUBMITTED = "SUBMITTED", "Submitted for review"
        REVIEWED = "REVIEWED", "Reviewed and signed off"
        RETURNED = "RETURNED", "Returned with review notes"

    engagement = models.ForeignKey(Engagement, on_delete=models.CASCADE, related_name="workpapers")
    reference = models.CharField(max_length=30)
    title = models.CharField(max_length=200)
    objective = models.TextField(blank=True)
    procedures = models.TextField(blank=True)
    conclusion = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    prepared_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    prepared_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["reference"]
        constraints = [models.UniqueConstraint(fields=["engagement", "reference"], name="uniq_workpaper_ref")]

    def __str__(self):
        return f"{self.reference} {self.title}"

    @property
    def institution(self):
        return self.engagement.institution


class Finding(InstitutionScopedModel):
    class Rating(models.TextChoices):
        HIGH = "HIGH", "High"
        MEDIUM = "MEDIUM", "Medium"
        LOW = "LOW", "Low"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open – awaiting management response"
        RESPONDED = "RESPONDED", "Management responded"
        IN_PROGRESS = "IN_PROGRESS", "Remediation in progress"
        PENDING_VERIFICATION = "PENDING_VERIFICATION", "Pending audit verification"
        CLOSED = "CLOSED", "Closed"

    engagement = models.ForeignKey(Engagement, on_delete=models.PROTECT, related_name="findings")
    workpaper = models.ForeignKey(Workpaper, null=True, blank=True, on_delete=models.SET_NULL, related_name="findings")
    reference = models.CharField(max_length=30, blank=True)
    title = models.CharField(max_length=200)
    rating = models.CharField(max_length=6, choices=Rating.choices)
    condition = models.TextField(help_text="What was found")
    criteria = models.TextField(blank=True, help_text="Policy, regulation or standard")
    cause = models.TextField(blank=True)
    effect = models.TextField(blank=True, help_text="Risk or impact")
    recommendation = models.TextField()
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="Management owner responsible for the response",
    )
    management_response = models.TextField(blank=True)
    responded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    status = models.CharField(max_length=22, choices=Status.choices, default=Status.OPEN)
    raised_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["institution", "reference"], condition=~Q(reference=""), name="uniq_finding_reference"
            )
        ]

    def __str__(self):
        return f"{self.reference} {self.title}"


class CorrectiveAction(models.Model):
    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        COMPLETED = "COMPLETED", "Completed by management"
        VERIFIED = "VERIFIED", "Verified by audit"

    finding = models.ForeignKey(Finding, on_delete=models.CASCADE, related_name="actions")
    description = models.TextField()
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    due_date = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    completion_notes = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["due_date"]

    @property
    def institution(self):
        return self.finding.institution
