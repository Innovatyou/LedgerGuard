from django.urls import path

from . import api

urlpatterns = [
    path("trial-balance/", api.TrialBalanceAPI.as_view(), name="api-trial-balance"),
    path("income-statement/", api.IncomeStatementAPI.as_view(), name="api-income-statement"),
    path("financial-position/", api.FinancialPositionAPI.as_view(), name="api-financial-position"),
    path("general-ledger/<int:account_id>/", api.GeneralLedgerAPI.as_view(), name="api-general-ledger"),
]
