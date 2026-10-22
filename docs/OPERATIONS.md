# Operations runbook

## Architecture in production

```
users ──HTTPS──► nginx (TLS, HSTS) ──► gunicorn (web)  ─┐
core banking ─SFTP/HTTPS─► drop folder / inbound API ───┤──► PostgreSQL 17 (owner + app roles)
                                       celery worker ───┤──► Redis (broker + cache)
                                       celery beat  ────┘──► media, backups, ingest volumes
```

Run at least two web containers behind the proxy for availability; one beat process only.

## First deployment

1. Provision PostgreSQL 17 and run `deploy/postgres/create_roles.sql` as a superuser (separate owner and app passwords).
2. Provision Redis 7 (persistence on) and three volumes: media, backups (preferably a separate disk or replicated
   storage), ingest.
3. Fill `.env` from `.env.example` using your secrets vault. `DJANGO_DEBUG=False`.
4. Build the image (`docker build -t ledgerguard:<version> .`) and start `web`, `worker`, `beat`. The entrypoint
   migrates with `MIGRATION_DATABASE_URL` and re-applies privileges (`harden_database`).
5. Put nginx (or the institution's load balancer) in front using `deploy/nginx.conf`; then set
   `DJANGO_SECURE_SSL_REDIRECT=True` and `DJANGO_HSTS_SECONDS=31536000`.
6. `docker compose exec web python manage.py check --deploy` must report no errors.
7. Create the platform operator: `python manage.py createsuperuser` (store credentials in the vault; enable MFA).
8. Create the institution in `/django-admin/`, then the institution administrator; everything else is done in the
   application (institution settings, branches, users, chart of accounts, periods, import profiles, rules).

## Upgrades

1. Take and verify a backup: `python manage.py backup`.
2. Deploy the new image to one web container; the entrypoint applies migrations (they are forward-only).
3. Roll the remaining containers. Run `python manage.py check_integrity`.
4. Rollback: redeploy the previous image. If a migration changed data, restore the pre-upgrade backup.

## Scheduled jobs (Celery beat)

| Job | Frequency | Command equivalent |
|---|---|---|
| Recurring journals, suspense escalations, drop folders, retention purge | hourly | `python manage.py run_scheduled_jobs` |
| Ledger integrity checks | daily | `python manage.py check_integrity` (non-zero exit on findings) |
| Database + media backup with verification and retention | daily | `python manage.py backup` |

Without Celery, run the commands from cron or a Kubernetes CronJob.

## Backups and restore

* `python manage.py backup` writes `pg_dump -Fc` and a media archive to `LEDGERGUARD_BACKUP_DIR` (mode 0600), records
  SHA-256 checksums, verifies them (checksum + `pg_restore --list`) and prunes after `LEDGERGUARD_BACKUP_RETENTION_DAYS`.
* Copy backups off-site (object storage with retention lock or the bank's backup system). The application does not
  do this for you.
* Database-level point-in-time recovery (WAL archiving / managed PITR) is recommended in addition.

Restore drill (do this before go-live and quarterly):

```bash
createdb -O ledgerguard_owner ledgerguard_restore
pg_restore --exit-on-error --no-owner --role=ledgerguard_owner -d ledgerguard_restore /backups/ledgerguard-db-YYYYMMDDTHHMMSS.dump
DATABASE_URL=postgres://ledgerguard_owner:...@host/ledgerguard_restore python manage.py harden_database
DATABASE_URL=postgres://ledgerguard_owner:...@host/ledgerguard_restore python manage.py check_integrity
tar -xzf /backups/ledgerguard-media-YYYYMMDDTHHMMSS.tar.gz -C /var/lib/ledgerguard/
```

Record the restore time to confirm the recovery-time objective. Compare row counts and ledger debit/credit totals
between the live and restored databases; the activity log has one more entry in the live database (the backup
records its own completion after the dump).

**Version match.** The image ships PostgreSQL 17 client tools (Debian 13), so run the database on PostgreSQL 17. A
pg_dump newer than the server it is restored into writes settings that server rejects (e.g. `transaction_timeout`
from a 17 dump restored into 16), and `--exit-on-error` will stop the restore. Verified on the container stack:
backup, restore into a new database, `harden_database`, `check_integrity` (OK), identical ledger totals, and the
application role refused UPDATE/DELETE/TRUNCATE on the restored copy, in about 7 seconds for the demo dataset.

## Monitoring

* `/health/` (liveness) and `/health/ready/` (database) for load balancers.
* `/administration/system/` (superusers): database, migrations, cache, workers, disk, last backup, integrity checks.
* Logs are JSON lines with `request_id`; ship them to the SIEM. Alert on: `auth.login_locked`, `period.reopened`,
  `reconciliation.reopened`, `journal.reversed`, `system.backup_failed`, `system.integrity_check` with failures,
  `integration.rejected`, HTTP 5xx rates.

## Incident checklist

1. Preserve logs and the activity log (append-only in the database).
2. If credentials may be compromised: deactivate memberships, rotate integration keys, force password resets,
   rotate `DJANGO_SECRET_KEY` (logs everyone out) and database passwords.
3. Run `check_integrity` for every institution.
4. If data restoration is needed, restore into a separate database first and compare.
