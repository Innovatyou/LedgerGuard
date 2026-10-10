from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from apps.core import views as core_views
from apps.core.attachments import AttachmentDownloadView, AttachmentUploadView

urlpatterns = [
    path("health/", core_views.health, name="health"),
    path("health/ready/", core_views.readiness, name="readiness"),
    path("django-admin/", admin.site.urls),
    path("api/v1/", include("config.api_urls")),
    path("accounts/", include("apps.accounts.urls")),
    path("organizations/", include("apps.organizations.urls")),
    path("accounting/", include("apps.accounting.urls")),
    path("imports/", include("apps.imports.urls")),
    path("reconciliation/", include("apps.reconciliation.urls")),
    path("settlements/", include("apps.settlements.urls")),
    path("suspense/", include("apps.suspense.urls")),
    path("audit/", include("apps.audit.urls")),
    path("notifications/", include("apps.notifications.urls")),
    path("evidence/<str:label>/<int:object_id>/upload/", AttachmentUploadView.as_view(), name="evidence-upload"),
    path("evidence/<int:pk>/download/", AttachmentDownloadView.as_view(), name="evidence-download"),
    path("activity/", core_views.ActivityLogListView.as_view(), name="activity-log"),
    path("", include("apps.reporting.urls")),
]

if settings.DEBUG:
    from django.conf.urls.static import static

    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
