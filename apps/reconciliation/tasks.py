import logging

from celery import shared_task

from apps.accounts.models import User
from apps.core.exceptions import BusinessRuleError

from .models import Reconciliation
from .services import run_reconciliation

logger = logging.getLogger(__name__)


@shared_task
def run_reconciliation_task(recon_id: int, user_id: int):
    recon = Reconciliation.objects.get(pk=recon_id)
    user = User.objects.get(pk=user_id)
    try:
        return run_reconciliation(recon, user)
    except BusinessRuleError as exc:
        logger.warning("Reconciliation %s run failed: %s", recon_id, exc.message)
        return {"status": "failed", "error": exc.message}
