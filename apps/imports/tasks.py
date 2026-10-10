import logging

from celery import shared_task

from apps.accounts.models import User
from apps.core.exceptions import BusinessRuleError

from .models import ImportBatch
from .services import validate_batch

logger = logging.getLogger(__name__)


@shared_task
def validate_batch_task(batch_id: int, user_id: int):
    batch = ImportBatch.objects.get(pk=batch_id)
    user = User.objects.get(pk=user_id)
    try:
        validate_batch(batch, user)
    except BusinessRuleError as exc:
        logger.warning("Import batch %s validation failed: %s", batch_id, exc.message)
        return {"status": "failed", "error": exc.message}
    return {"status": "validated", "batch": batch_id}
