from django.core.management.base import BaseCommand, CommandError

from apps.administration import services
from apps.organizations.models import Institution


class Command(BaseCommand):
    help = "Run ledger integrity checks for every active institution; exits non-zero on any finding."

    def handle(self, *args, **options):
        failed = False
        for institution in Institution.objects.filter(is_active=True):
            check = services.check_integrity(institution)
            if check.passed:
                self.stdout.write(self.style.SUCCESS(f"{institution.code}: OK"))
            else:
                failed = True
                for finding in check.findings:
                    self.stdout.write(self.style.ERROR(f"{institution.code}: {finding}"))
        if failed:
            raise CommandError("Integrity check found problems.")
