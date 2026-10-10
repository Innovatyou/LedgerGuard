#!/bin/sh
# Container entrypoint. When MIGRATION_DATABASE_URL is set, migrations run with the
# schema-owner role and privileges are re-applied for the restricted application role.
set -eu
if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  if [ -n "${MIGRATION_DATABASE_URL:-}" ]; then
    DATABASE_URL="$MIGRATION_DATABASE_URL" python manage.py migrate --noinput
    DATABASE_URL="$MIGRATION_DATABASE_URL" python manage.py harden_database --app-role "${APP_DB_ROLE:-ledgerguard_app}"
  else
    python manage.py migrate --noinput
  fi
fi
exec "$@"
