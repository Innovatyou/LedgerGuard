"""Database-level protections installed by migrations (PostgreSQL only)."""

from django.db import migrations

_FUNCTION = """
CREATE OR REPLACE FUNCTION ledgerguard_block_modification() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'LedgerGuard: % on % is not permitted (append-only table)', TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;
"""


def append_only(table: str):
    """Migration operation making ``table`` append-only (UPDATE/DELETE/TRUNCATE rejected)."""

    def forwards(apps, schema_editor):
        if schema_editor.connection.vendor != "postgresql":
            return
        schema_editor.execute(_FUNCTION, params=None)
        schema_editor.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION ledgerguard_block_modification();",
            params=None,
        )
        schema_editor.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION ledgerguard_block_modification();",
            params=None,
        )

    def backwards(apps, schema_editor):
        if schema_editor.connection.vendor != "postgresql":
            return
        schema_editor.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};", params=None)
        schema_editor.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table};", params=None)

    return migrations.RunPython(forwards, backwards)
