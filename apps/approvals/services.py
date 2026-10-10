from apps.core.activity import log_activity
from apps.core.exceptions import SegregationOfDutiesError

from .models import ApprovalRecord


def assert_independent(institution, preparer, actor, what="this item"):
    """Maker-checker: the actor must not be the preparer when the institution requires it."""
    if institution.require_independent_approval and preparer is not None and preparer.pk == actor.pk:
        raise SegregationOfDutiesError(
            f"Segregation of duties: you prepared {what} and cannot approve or post it yourself."
        )


def record_decision(obj, decision, user, comment=""):
    record = ApprovalRecord.objects.create(
        institution=obj.institution,
        object_type=obj.__class__.__name__,
        object_id=str(obj.pk),
        decision=decision,
        user=user,
        comment=comment or "",
    )
    log_activity(
        f"{obj.__class__.__name__.lower()}.{decision.lower()}",
        user=user,
        obj=obj,
        details={"comment": comment} if comment else None,
    )
    return record


def history_for(obj):
    return ApprovalRecord.objects.filter(
        institution=obj.institution, object_type=obj.__class__.__name__, object_id=str(obj.pk)
    ).select_related("user")
