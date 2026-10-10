from django.db import connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.generic import ListView

from .models import ActivityLog
from .views_mixins import InstitutionRequiredMixin


@never_cache
def health(request):
    return JsonResponse({"status": "ok"})


@never_cache
def readiness(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        db_ok = True
    except Exception:  # noqa: BLE001 - report any failure as not ready
        db_ok = False
    return JsonResponse({"status": "ok" if db_ok else "degraded", "database": db_ok}, status=200 if db_ok else 503)


class ActivityLogListView(InstitutionRequiredMixin, ListView):
    permission_required = "activity.view"
    template_name = "core/activity_log.html"
    paginate_by = 100

    def get_queryset(self):
        qs = ActivityLog.objects.filter(institution=self.institution).select_related("user")
        action = self.request.GET.get("action")
        if action:
            qs = qs.filter(action__icontains=action)
        object_type = self.request.GET.get("object_type")
        if object_type:
            qs = qs.filter(object_type=object_type)
        return qs
