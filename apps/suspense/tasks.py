from celery import shared_task

from apps.organizations.models import Institution

from .services import run_escalations as _run


@shared_task
def run_escalations():
    total = 0
    for institution in Institution.objects.filter(is_active=True):
        total += _run(institution)
    return total
