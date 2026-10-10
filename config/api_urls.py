from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.accounting import api as accounting_api
from apps.audit import api as audit_api
from apps.fixed_assets import api as asset_api
from apps.imports import api as imports_api
from apps.integrations import api as integration_api
from apps.organizations import api as org_api
from apps.reconciliation import api as recon_api
from apps.settlements import api as settlement_api
from apps.suspense import api as suspense_api

router = DefaultRouter()
router.register("branches", org_api.BranchViewSet, basename="branch")
router.register("departments", org_api.DepartmentViewSet, basename="department")
router.register("accounts", accounting_api.AccountViewSet, basename="account")
router.register("periods", accounting_api.FinancialPeriodViewSet, basename="period")
router.register("journals", accounting_api.JournalViewSet, basename="journal")
router.register("import-profiles", imports_api.ImportProfileViewSet, basename="import-profile")
router.register("import-batches", imports_api.ImportBatchViewSet, basename="import-batch")
router.register("transactions", imports_api.SourceTransactionViewSet, basename="transaction")
router.register("matching-rules", recon_api.MatchingRuleViewSet, basename="matching-rule")
router.register("reconciliations", recon_api.ReconciliationViewSet, basename="reconciliation")
router.register("suspense-items", suspense_api.SuspenseItemViewSet, basename="suspense-item")
router.register("fixed-assets", asset_api.FixedAssetViewSet, basename="fixed-asset")
router.register("audit-engagements", audit_api.EngagementViewSet, basename="engagement")
router.register("audit-findings", audit_api.FindingViewSet, basename="finding")

urlpatterns = [
    path("", include(router.urls)),
    path("reports/", include("apps.reporting.api_urls")),
    path("settlements/summary/", settlement_api.SettlementSummaryView.as_view(), name="api-settlement-summary"),
    path("me/", org_api.MeView.as_view(), name="api-me"),
    path("integrations/inbound/", integration_api.InboundFileView.as_view(), name="api-integration-inbound"),
]
