from django.apps import AppConfig


class AccountingConfig(AppConfig):
    name = "apps.accounting"
    label = "accounting"

    def ready(self):
        from apps.approvals.changes import register

        from .models import Account

        register("account", "account", Account, "coa.manage")
