from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import View

from apps.accounts.models import User
from apps.core.activity import log_activity
from apps.core.exceptions import BusinessRuleError
from apps.core.views_mixins import InstitutionRequiredMixin, scoped_get
from apps.imports.models import ImportProfile

from . import services
from .models import IntegrationEndpoint, IntegrationLog


class IntegrationsView(InstitutionRequiredMixin, View):
    permission_required = "integration.view"

    def get(self, request):
        return render(
            request,
            "integrations/index.html",
            {
                "endpoints": IntegrationEndpoint.objects.filter(institution=self.institution).select_related(
                    "import_profile", "service_user"
                ),
                "logs": IntegrationLog.objects.filter(endpoint__institution=self.institution).select_related(
                    "endpoint"
                )[:100],
                "profiles": ImportProfile.objects.filter(institution=self.institution, is_active=True),
                "users": User.objects.filter(
                    memberships__institution=self.institution, memberships__is_active=True, is_active=True
                ).distinct(),
                "kinds": IntegrationEndpoint.Kind.choices,
            },
        )

    def post(self, request):
        if not self.can("integration.manage"):
            messages.error(request, "Permission required: integration.manage")
            return redirect("integrations:index")
        action = request.POST.get("action")
        key = None
        try:
            if action == "create":
                profile = scoped_get(request, ImportProfile, pk=request.POST.get("profile"))
                service_user = get_object_or_404(
                    User, pk=request.POST.get("service_user"), memberships__institution=self.institution
                )
                ips = [p.strip() for p in request.POST.get("allowed_ips", "").split(",") if p.strip()]
                try:
                    _, key = services.create_endpoint(
                        self.institution,
                        request.user,
                        name=request.POST.get("name", ""),
                        kind=request.POST.get("kind"),
                        import_profile=profile,
                        service_user=service_user,
                        allowed_ips=ips,
                        folder=request.POST.get("folder", ""),
                        auto_commit=request.POST.get("auto_commit") == "on",
                    )
                except ValueError as exc:
                    raise BusinessRuleError(f"Invalid IP address or network: {exc}") from exc
                messages.success(request, "Endpoint created.")
            else:
                endpoint = scoped_get(request, IntegrationEndpoint, pk=request.POST.get("endpoint"))
                if action == "rotate":
                    key = services.rotate_key(endpoint, request.user)
                elif action == "toggle":
                    endpoint.is_active = not endpoint.is_active
                    endpoint.save(update_fields=["is_active", "updated_at"])
                    log_activity(
                        "integration.endpoint_toggled",
                        user=request.user,
                        obj=endpoint,
                        details={"active": endpoint.is_active},
                    )
        except BusinessRuleError as exc:
            messages.error(request, exc.message)
        if key:
            messages.warning(request, f"Integration key (shown once – store it in the sending system's vault): {key}")
        return redirect(reverse("integrations:index"))
