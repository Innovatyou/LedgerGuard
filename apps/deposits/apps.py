from django.apps import AppConfig


class DepositsConfig(AppConfig):
    name = "apps.deposits"
    label = "deposits"

    def ready(self):
        from apps.approvals.changes import register

        from .models import DepositProduct

        register("deposit_product", "deposit product mapping", DepositProduct, "portfolio.manage")
