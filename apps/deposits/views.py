from datetime import date

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import View

from apps.accounting.models import Account
from apps.accounts.services import require_perm
from apps.approvals.changes import request_change, requires_approval
from apps.core.activity import log_activity
from apps.core.views_mixins import InstitutionRequiredMixin, run_action, scoped_get

from . import services
from .models import DepositProduct, DepositSnapshot


class DepositControlView(InstitutionRequiredMixin, View):
    permission_required = "portfolio.view"

    def get(self, request):
        return render(
            request,
            "deposits/index.html",
            {
                "snapshots": DepositSnapshot.objects.filter(institution=self.institution).select_related(
                    "uploaded_by", "reviewed_by"
                ),
                "products": DepositProduct.objects.filter(institution=self.institution).select_related("gl_account"),
                "accounts": Account.objects.filter(
                    institution=self.institution, is_active=True, account_type="POSTING", category="LIABILITY"
                ),
            },
        )

    def post(self, request):
        back = reverse("deposits:index")
        action = request.POST.get("action")
        if action == "upload":
            upload = request.FILES.get("file")
            try:
                as_of = date.fromisoformat(request.POST.get("as_of", ""))
            except ValueError:
                messages.error(request, "Enter the listing date.")
                return redirect(back)
            if upload is None:
                messages.error(request, "Choose the deposit listing file.")
                return redirect(back)
            return run_action(
                request,
                lambda: services.load_snapshot(
                    self.institution, request.user, filename=upload.name, data=upload.read(), as_of=as_of
                ),
                "Deposit listing loaded.",
                back,
                success_redirect=lambda s: reverse("deposits:snapshot", args=[s.pk]),
            )
        if action == "product":
            account = get_object_or_404(Account, pk=request.POST.get("gl_account"), institution=self.institution)
            interest_pk = request.POST.get("interest_payable_account")
            payload = {
                "code": request.POST.get("code", "").strip().upper(),
                "name": request.POST.get("name", ""),
                "gl_account": account,
                "interest_payable_account": get_object_or_404(Account, pk=interest_pk, institution=self.institution)
                if interest_pk
                else None,
            }
            if requires_approval(self.institution):
                return run_action(
                    request,
                    lambda: request_change(
                        self.institution,
                        request.user,
                        "deposit_product",
                        payload,
                        summary=f"Map deposit product {payload['code']}",
                    ),
                    "Mapping submitted for approval.",
                    back,
                )

            def apply():
                require_perm(request.user, self.institution, "portfolio.manage")
                product = DepositProduct.objects.create(institution=self.institution, **payload)
                log_activity("deposits.product_mapped", user=request.user, obj=product)

            return run_action(request, apply, "Product mapped.", back)
        messages.error(request, "Unknown action.")
        return redirect(back)


class DepositSnapshotView(InstitutionRequiredMixin, View):
    permission_required = "portfolio.view"

    def get(self, request, pk):
        snapshot = scoped_get(request, DepositSnapshot, pk=pk)
        return render(
            request,
            "deposits/snapshot.html",
            {
                "snapshot": snapshot,
                "control": services.control(snapshot, self.branch_scope),
            },
        )

    def post(self, request, pk):
        snapshot = scoped_get(request, DepositSnapshot, pk=pk)
        return run_action(
            request,
            lambda: services.review(snapshot, request.user, request.POST.get("comment", "")),
            "Listing signed off.",
            reverse("deposits:snapshot", args=[snapshot.pk]),
        )
