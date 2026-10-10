from django.contrib import messages
from django.contrib.auth.mixins import UserPassesTestMixin
from django.shortcuts import redirect, render
from django.views.generic import View

from apps.organizations.models import Institution

from . import services
from .models import BackupRecord, IntegrityCheck


class SystemView(UserPassesTestMixin, View):
    """Platform operations page – restricted to platform superusers."""

    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_superuser

    def get(self, request):
        return render(
            request,
            "administration/system.html",
            {
                "health": services.health(),
                "backups": BackupRecord.objects.all()[:30],
                "checks": IntegrityCheck.objects.select_related("institution")[:30],
                "institutions": Institution.objects.filter(is_active=True),
            },
        )

    def post(self, request):
        action = request.POST.get("action")
        try:
            if action == "backup":
                record = services.verify_backup(services.backup_database())
                messages.success(request, f"Database backup created and verified: {record.path}")
            elif action == "integrity":
                for inst in Institution.objects.filter(is_active=True):
                    check = services.check_integrity(inst)
                    (messages.success if check.passed else messages.error)(
                        request, f"{inst.code}: {'passed' if check.passed else '; '.join(check.findings[:5])}"
                    )
        except services.BackupError as exc:
            messages.error(request, f"Backup failed: {exc}")
        return redirect("administration:system")
