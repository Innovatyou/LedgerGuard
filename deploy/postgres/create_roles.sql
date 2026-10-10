-- Run once as a PostgreSQL superuser, e.g.:
--   psql -v owner_password="'...'" -v app_password="'...'" -v dbname=ledgerguard -f deploy/postgres/create_roles.sql
-- ledgerguard_owner owns the schema and runs migrations.
-- ledgerguard_app is what the web/worker processes use: data access only, no DDL,
-- so it cannot drop or disable the append-only triggers.
\set ON_ERROR_STOP on
CREATE ROLE ledgerguard_owner LOGIN PASSWORD :owner_password;
CREATE ROLE ledgerguard_app LOGIN PASSWORD :app_password;
CREATE DATABASE :dbname OWNER ledgerguard_owner;
\connect :dbname
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO ledgerguard_owner;
GRANT USAGE ON SCHEMA public TO ledgerguard_app;
ALTER DEFAULT PRIVILEGES FOR ROLE ledgerguard_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ledgerguard_app;
ALTER DEFAULT PRIVILEGES FOR ROLE ledgerguard_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO ledgerguard_app;
