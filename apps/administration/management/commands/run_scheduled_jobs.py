from django.core.management.base import BaseCommand

from apps.accounting.recurring import generate_due
from apps.imports.services import purge_expired_files
from apps.integrations.services import process_drop_folders
from apps.organizations.models import Institution
from apps.suspense.services import run_escalations


class Command(BaseCommand):
    help = (
        "Run periodic jobs without Celery (e.g. from cron or a Kubernetes CronJob): recurring journals, "
        "suspense escalations, drop-folder ingestion and retention purges."
    )

    def handle(self, *args, **options):
        for institution in Institution.objects.filter(is_active=True):
            journals = generate_due(institution)
            escalations = run_escalations(institution)
            purged = purge_expired_files(institution)
            self.stdout.write(
                f"{institution.code}: {len(journals)} recurring journal(s), "
                f"{escalations} escalation(s), {purged} file(s) purged"
            )
        self.stdout.write(f"Drop folders: {process_drop_folders()}")
