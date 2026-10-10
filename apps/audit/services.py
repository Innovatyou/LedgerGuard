from django.db import transaction
from django.utils import timezone

from apps.accounts.services import has_perm, require_perm
from apps.core.activity import log_activity
from apps.core.exceptions import BusinessRuleError, PermissionDeniedError, SegregationOfDutiesError
from apps.core.sequences import next_number

from .models import CorrectiveAction, Engagement, Finding, Workpaper

_ENGAGEMENT_FLOW = [
    Engagement.Status.PLANNING,
    Engagement.Status.FIELDWORK,
    Engagement.Status.REPORTING,
    Engagement.Status.CLOSED,
]


def create_engagement(institution, user, *, team=(), **fields) -> Engagement:
    require_perm(user, institution, "audit.manage")
    if fields["period_end"] < fields["period_start"]:
        raise BusinessRuleError("Audit period end precedes start.")
    with transaction.atomic():
        engagement = Engagement.objects.create(
            institution=institution,
            created_by=user,
            reference=next_number(institution, "engagement", "AUD-"),
            **fields,
        )
        engagement.team.set(team)
    log_activity("audit.engagement_created", user=user, obj=engagement)
    return engagement


def advance_engagement(engagement: Engagement, user) -> Engagement:
    require_perm(user, engagement.institution, "audit.review")
    idx = _ENGAGEMENT_FLOW.index(engagement.status)
    if idx == len(_ENGAGEMENT_FLOW) - 1:
        raise BusinessRuleError("Engagement is already closed.")
    target = _ENGAGEMENT_FLOW[idx + 1]
    if target == Engagement.Status.CLOSED:
        open_wp = engagement.workpapers.exclude(status=Workpaper.Status.REVIEWED).count()
        if open_wp:
            raise BusinessRuleError(f"{open_wp} workpaper(s) are not yet reviewed and signed off.")
    engagement.status = target
    engagement.save(update_fields=["status", "updated_at"])
    log_activity("audit.engagement_advanced", user=user, obj=engagement, details={"status": target})
    return engagement


def create_workpaper(engagement, user, **fields) -> Workpaper:
    require_perm(user, engagement.institution, "audit.manage")
    if engagement.status == Engagement.Status.CLOSED:
        raise BusinessRuleError("Engagement is closed.")
    wp = Workpaper.objects.create(engagement=engagement, prepared_by=user, **fields)
    log_activity("audit.workpaper_created", user=user, institution=engagement.institution, obj=wp)
    return wp


def submit_workpaper(wp: Workpaper, user) -> Workpaper:
    require_perm(user, wp.institution, "audit.manage")
    if wp.status not in (Workpaper.Status.DRAFT, Workpaper.Status.RETURNED):
        raise BusinessRuleError("Only draft or returned workpapers can be submitted.")
    if not wp.conclusion.strip():
        raise BusinessRuleError("Record a conclusion before submitting the workpaper.")
    wp.status = Workpaper.Status.SUBMITTED
    wp.prepared_at = timezone.now()
    wp.save()
    log_activity("audit.workpaper_submitted", user=user, institution=wp.institution, obj=wp)
    return wp


def review_workpaper(wp: Workpaper, user, *, approve: bool, notes="") -> Workpaper:
    require_perm(user, wp.institution, "audit.review")
    if wp.status != Workpaper.Status.SUBMITTED:
        raise BusinessRuleError("Only submitted workpapers can be reviewed.")
    if wp.prepared_by_id == user.pk:
        raise SegregationOfDutiesError("A workpaper must be reviewed by someone other than its preparer.")
    if not approve and not notes.strip():
        raise BusinessRuleError("Review notes are required when returning a workpaper.")
    wp.status = Workpaper.Status.REVIEWED if approve else Workpaper.Status.RETURNED
    wp.reviewed_by = user
    wp.reviewed_at = timezone.now()
    wp.review_notes = notes
    wp.save()
    log_activity(
        "audit.workpaper_reviewed",
        user=user,
        institution=wp.institution,
        obj=wp,
        details={"approved": approve, "notes": notes},
    )
    return wp


def raise_finding(engagement, user, **fields) -> Finding:
    require_perm(user, engagement.institution, "audit.manage")
    if engagement.status == Engagement.Status.CLOSED:
        raise BusinessRuleError("Engagement is closed.")
    workpaper = fields.get("workpaper")
    if workpaper is not None and workpaper.engagement_id != engagement.pk:
        raise BusinessRuleError("Workpaper belongs to a different engagement.")
    finding = Finding.objects.create(
        institution=engagement.institution,
        engagement=engagement,
        raised_by=user,
        reference=next_number(engagement.institution, "finding", "FND-"),
        **fields,
    )
    log_activity("audit.finding_raised", user=user, obj=finding)
    return finding


def respond_to_finding(finding: Finding, user, response: str) -> Finding:
    require_perm(user, finding.institution, "audit.respond")
    if finding.status not in (Finding.Status.OPEN, Finding.Status.RESPONDED):
        raise BusinessRuleError("This finding is no longer awaiting a management response.")
    if not response.strip():
        raise BusinessRuleError("A response is required.")
    finding.management_response = response
    finding.responded_by = user
    finding.status = Finding.Status.RESPONDED
    finding.save()
    log_activity("audit.finding_responded", user=user, obj=finding)
    return finding


def add_corrective_action(finding: Finding, user, *, description, owner, due_date) -> CorrectiveAction:
    if not has_any(user, finding.institution, "audit.respond", "audit.manage"):
        raise PermissionDeniedError("Permission required: audit.respond or audit.manage")
    if finding.status == Finding.Status.CLOSED:
        raise BusinessRuleError("Finding is closed.")
    action = CorrectiveAction.objects.create(finding=finding, description=description, owner=owner, due_date=due_date)
    if finding.status in (Finding.Status.OPEN, Finding.Status.RESPONDED):
        finding.status = Finding.Status.IN_PROGRESS
        finding.save(update_fields=["status", "updated_at"])
    log_activity("audit.action_added", user=user, obj=finding, details={"action": action.pk})
    return action


def complete_action(action: CorrectiveAction, user, notes: str) -> CorrectiveAction:
    require_perm(user, action.institution, "audit.respond")
    if action.status != CorrectiveAction.Status.OPEN:
        raise BusinessRuleError("Action is not open.")
    if not notes.strip():
        raise BusinessRuleError("Completion notes are required.")
    action.status = CorrectiveAction.Status.COMPLETED
    action.completion_notes = notes
    action.completed_at = timezone.now()
    action.save()
    finding = action.finding
    if not finding.actions.filter(status=CorrectiveAction.Status.OPEN).exists():
        finding.status = Finding.Status.PENDING_VERIFICATION
        finding.save(update_fields=["status", "updated_at"])
    log_activity("audit.action_completed", user=user, obj=finding, details={"action": action.pk})
    return action


def verify_action(action: CorrectiveAction, user) -> CorrectiveAction:
    require_perm(user, action.institution, "audit.review")
    if action.status != CorrectiveAction.Status.COMPLETED:
        raise BusinessRuleError("Only completed actions can be verified.")
    if action.owner_id == user.pk:
        raise SegregationOfDutiesError("The action owner cannot verify their own action.")
    action.status = CorrectiveAction.Status.VERIFIED
    action.verified_by = user
    action.verified_at = timezone.now()
    action.save()
    log_activity("audit.action_verified", user=user, obj=action.finding, details={"action": action.pk})
    return action


def close_finding(finding: Finding, user) -> Finding:
    require_perm(user, finding.institution, "audit.review")
    if finding.status == Finding.Status.CLOSED:
        raise BusinessRuleError("Finding is already closed.")
    if not finding.actions.exists():
        raise BusinessRuleError("Record at least one corrective action before closing.")
    if finding.actions.exclude(status=CorrectiveAction.Status.VERIFIED).exists():
        raise BusinessRuleError("All corrective actions must be verified by audit before closing.")
    finding.status = Finding.Status.CLOSED
    finding.closed_by = user
    finding.closed_at = timezone.now()
    finding.save()
    log_activity("audit.finding_closed", user=user, obj=finding)
    return finding


def has_any(user, institution, *codes):
    return any(has_perm(user, institution, c) for c in codes)
