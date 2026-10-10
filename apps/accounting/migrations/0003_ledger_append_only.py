from django.db import migrations

from apps.core.db import append_only


class Migration(migrations.Migration):
    dependencies = [("accounting", "0002_initial"), ("core", "0002_activity_log_append_only")]

    operations = [append_only("accounting_ledgerentry")]
