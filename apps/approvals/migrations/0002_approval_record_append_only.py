from django.db import migrations

from apps.core.db import append_only


class Migration(migrations.Migration):
    dependencies = [("approvals", "0001_initial"), ("core", "0002_activity_log_append_only")]

    operations = [append_only("approvals_approvalrecord")]
