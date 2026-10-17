from django.urls import path

from . import views

app_name = "reporting"

urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),
    path("reports/trial-balance/", views.TrialBalanceView.as_view(), name="trial-balance"),
    path("reports/general-ledger/", views.GeneralLedgerView.as_view(), name="general-ledger"),
    path("reports/income-statement/", views.IncomeStatementView.as_view(), name="income-statement"),
    path("reports/financial-position/", views.FinancialPositionView.as_view(), name="financial-position"),
    path("reports/reconciliation/<int:pk>/", views.ReconciliationStatementView.as_view(), name="recon-statement"),
    path(
        "reports/reconciliation/<int:pk>/sessions/",
        views.SettlementSessionReportView.as_view(),
        name="recon-sessions",
    ),
    path("reports/suspense-aging/", views.SuspenseAgingReportView.as_view(), name="suspense-aging"),
]
