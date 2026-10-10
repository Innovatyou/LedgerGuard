from celery import shared_task
from django.core.management import call_command


@shared_task
def nightly_backup():
    call_command("backup")


@shared_task
def scheduled_jobs():
    call_command("run_scheduled_jobs")


@shared_task
def integrity_checks():
    from apps.organizations.models import Institution

    from .services import check_integrity

    return {inst.code: check_integrity(inst).passed for inst in Institution.objects.filter(is_active=True)}
