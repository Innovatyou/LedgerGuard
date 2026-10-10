from django.apps import AppConfig


class ReconciliationConfig(AppConfig):
    name = "apps.reconciliation"
    label = "reconciliation"

    def ready(self):
        from apps.approvals.changes import register

        from .models import MatchingRule

        register("matching_rule", "matching rule", MatchingRule, "recon.rule_manage")
