from django.db import migrations

from apps.core.db import append_only


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]

    operations = [append_only("core_activitylog")]
