from django.core.management.base import BaseCommand

from apps.accounts.models import Role
from apps.accounts.roles import sync_system_roles


class Command(BaseCommand):
    help = "Create or update the built-in system roles and their default permissions."

    def handle(self, *args, **options):
        sync_system_roles(Role)
        self.stdout.write(self.style.SUCCESS("System roles synchronised."))
