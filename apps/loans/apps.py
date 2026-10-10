from django.apps import AppConfig


class LoansConfig(AppConfig):
    name = "apps.loans"
    label = "loans"

    def ready(self):
        from apps.approvals.changes import register

        from .models import ClassificationBand

        register("classification_band", "loan classification band", ClassificationBand, "portfolio.manage")
