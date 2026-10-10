from django.db import migrations


def forwards(apps, schema_editor):
    from apps.accounts.roles import sync_system_roles

    sync_system_roles(apps.get_model("accounts", "Role"))


class Migration(migrations.Migration):
    dependencies = [("accounts", "0001_initial")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
