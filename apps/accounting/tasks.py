from celery import shared_task

from apps.organizations.models import Institution

from .recurring import generate_due


@shared_task
def generate_recurring_journals():
    return sum(len(generate_due(inst)) for inst in Institution.objects.filter(is_active=True))
