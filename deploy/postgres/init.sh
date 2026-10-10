#!/bin/sh
# Docker postgres init hook: create the owner/app roles and database from environment variables.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v owner_password="'$LEDGERGUARD_OWNER_PASSWORD'" -v app_password="'$LEDGERGUARD_APP_PASSWORD'" \
  -v dbname=ledgerguard -f /docker-entrypoint-initdb.d/create_roles.sql.in
