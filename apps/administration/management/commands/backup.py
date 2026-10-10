from django.core.management.base import BaseCommand, CommandError

from apps.administration import services


class Command(BaseCommand):
    help = "Back up the database (pg_dump) and uploaded files, verify the backups and apply retention."

    def add_arguments(self, parser):
        parser.add_argument("--skip-media", action="store_true")
        parser.add_argument("--no-prune", action="store_true")

    def handle(self, *args, skip_media=False, no_prune=False, **options):
        try:
            db = services.verify_backup(services.backup_database())
            self.stdout.write(
                self.style.SUCCESS(f"Database backup verified: {db.path} ({db.size} bytes) sha256={db.sha256}")
            )
            if not skip_media:
                media = services.verify_backup(services.backup_media())
                self.stdout.write(self.style.SUCCESS(f"Media backup verified: {media.path}"))
        except services.BackupError as exc:
            raise CommandError(f"Backup failed: {exc}") from exc
        if not no_prune:
            self.stdout.write(f"Pruned {services.prune_backups()} expired backup(s).")
