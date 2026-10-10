from django.apps import AppConfig


class OrganizationsConfig(AppConfig):
    name = "apps.organizations"
    label = "organizations"

    def ready(self):
        from apps.approvals.changes import register

        from .models import Institution

        register("institution_settings", "institution settings", Institution, "org.manage")
