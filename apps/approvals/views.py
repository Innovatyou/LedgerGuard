from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import render
from django.urls import reverse
from django.views.generic import View

from apps.accounts.services import has_perm
from apps.core.views_mixins import InstitutionRequiredMixin, run_action, scoped_get

from . import changes
from .models import ChangeRequest

REQUEST_PERMS = ("coa.manage", "recon.rule_manage", "org.manage", "config.approve")


class ChangeRequestListView(InstitutionRequiredMixin, View):
    def get(self, request):
        if not any(self.can(p) for p in REQUEST_PERMS):
            raise PermissionDenied
        status = request.GET.get("status", "PENDING")
        qs = ChangeRequest.objects.filter(institution=self.institution).select_related("requested_by", "decided_by")
        if not self.can("config.approve"):
            qs = qs.filter(Q(requested_by=request.user))
        if status:
            qs = qs.filter(status=status)
        return render(
            request,
            "approvals/change_list.html",
            {
                "changes": qs[:200],
                "status": status,
                "statuses": ChangeRequest.Status.choices,
                "targets": {k: t.label for k, t in changes.TARGETS.items()},
            },
        )

    def post(self, request):
        cr = scoped_get(request, ChangeRequest, pk=request.POST.get("change"))
        back = reverse("approvals:changes")
        comment = request.POST.get("comment", "")
        if request.POST.get("action") == "approve":
            return run_action(
                request,
                lambda: changes.approve_change(cr, request.user, comment),
                f"{cr.reference} approved and applied.",
                back,
            )
        if cr.requested_by_id != request.user.pk and not has_perm(request.user, self.institution, "config.approve"):
            raise PermissionDenied
        return run_action(
            request, lambda: changes.reject_change(cr, request.user, comment), f"{cr.reference} closed.", back
        )
