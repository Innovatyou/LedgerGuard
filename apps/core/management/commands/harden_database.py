import re

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

APPEND_ONLY_TABLES = ["accounting_ledgerentry", "core_activitylog", "approvals_approvalrecord"]


class Command(BaseCommand):
    help = (
        "Grant the application role data access to every table and remove UPDATE/DELETE on append-only tables. "
        "Run after each migration, connected as the schema owner."
    )

    def add_arguments(self, parser):
        parser.add_argument("--app-role", default="ledgerguard_app")

    def handle(self, *args, app_role, **options):
        if connection.vendor != "postgresql":
            raise CommandError("PostgreSQL only.")
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", app_role):
            raise CommandError("Invalid role name.")
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [app_role])
            if cursor.fetchone() is None:
                raise CommandError(f"Role {app_role} does not exist.")
            cursor.execute(f'GRANT USAGE ON SCHEMA public TO "{app_role}"')
            cursor.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "{app_role}"')
            cursor.execute(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{app_role}"')
            for table in APPEND_ONLY_TABLES:
                cursor.execute(f'REVOKE UPDATE, DELETE, TRUNCATE ON "{table}" FROM "{app_role}"')
        self.stdout.write(self.style.SUCCESS(f"Privileges applied for {app_role}."))
