from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import View

from apps.accounting.models import Account
from apps.approvals.changes import request_change, requires_approval
from apps.core.views_mixins import InstitutionRequiredMixin, run_action, scoped_get

from . import services
from .models import ClassificationBand, LoanSnapshot


def _accounts(institution):
    return Account.objects.filter(institution=institution, is_active=True, account_type="POSTING")


class LoanPortfolioView(InstitutionRequiredMixin, View):
    permission_required = "portfolio.view"

    def get(self, request):
        return render(
            request,
            "loans/index.html",
            {
                "snapshots": LoanSnapshot.objects.filter(institution=self.institution).select_related(
                    "uploaded_by", "reviewed_by"
                ),
                "bands": services.bands(self.institution),
                "accounts": _accounts(self.institution),
            },
        )

    def post(self, request):
        back = reverse("loans:index")
        action = request.POST.get("action")
        if action == "upload":
            upload = request.FILES.get("file")
            try:
                as_of = date.fromisoformat(request.POST.get("as_of", ""))
            except ValueError:
                messages.error(request, "Enter the portfolio date.")
                return redirect(back)
            if upload is None:
                messages.error(request, "Choose the loan listing file.")
                return redirect(back)

            def acct(name):
                pk = request.POST.get(name)
                return get_object_or_404(Account, pk=pk, institution=self.institution) if pk else None

            return run_action(
                request,
                lambda: services.load_snapshot(
                    self.institution,
                    request.user,
                    filename=upload.name,
                    data=upload.read(),
                    as_of=as_of,
                    principal_account=acct("principal_account"),
                    interest_account=acct("interest_account"),
                    provision_account=acct("provision_account"),
                ),
                "Loan listing loaded.",
                back,
                success_redirect=lambda s: reverse("loans:snapshot", args=[s.pk]),
            )
        if action == "band":
            band = scoped_get(request, ClassificationBand, pk=request.POST.get("band"))
            try:
                payload = {"provision_rate": Decimal(request.POST.get("provision_rate", ""))}
            except InvalidOperation:
                messages.error(request, "Enter a valid rate.")
                return redirect(back)
            if requires_approval(self.institution):
                return run_action(
                    request,
                    lambda: request_change(
                        self.institution,
                        request.user,
                        "classification_band",
                        payload,
                        target_obj=band,
                        summary=f"Change provision rate for {band.name}",
                    ),
                    "Change submitted for independent approval.",
                    back,
                )

            def apply():
                from apps.accounts.services import require_perm
                from apps.core.activity import log_activity

                require_perm(request.user, self.institution, "portfolio.manage")
                band.provision_rate = payload["provision_rate"]
                band.save()
                log_activity("loans.band_changed", user=request.user, obj=band, details=payload)

            return run_action(request, apply, "Band updated.", back)
        messages.error(request, "Unknown action.")
        return redirect(back)


class LoanSnapshotView(InstitutionRequiredMixin, View):
    permission_required = "portfolio.view"

    def get(self, request, pk):
        snapshot = scoped_get(request, LoanSnapshot, pk=pk)
        records = snapshot.records.select_related("branch")
        if self.branch_scope is not None:
            records = records.filter(branch_id__in=self.branch_scope)
        if request.GET.get("classification"):
            records = records.filter(classification=request.GET["classification"])
        return render(
            request,
            "loans/snapshot.html",
            {
                "snapshot": snapshot,
                "control": services.control(snapshot, self.branch_scope),
                "page": Paginator(records, 100).get_page(request.GET.get("page")),
                "accounts": _accounts(self.institution),
            },
        )

    def post(self, request, pk):
        snapshot = scoped_get(request, LoanSnapshot, pk=pk)
        back = reverse("loans:snapshot", args=[snapshot.pk])
        if request.POST.get("action") == "review":
            return run_action(
                request,
                lambda: services.review(snapshot, request.user, request.POST.get("comment", "")),
                "Schedule signed off.",
                back,
            )
        if request.POST.get("action") == "provision":
            account = get_object_or_404(Account, pk=request.POST.get("charge_account"), institution=self.institution)
            return run_action(
                request,
                lambda: services.prepare_provision_journal(snapshot, request.user, account),
                "Provision journal prepared and submitted for approval.",
                back,
            )
        messages.error(request, "Unknown action.")
        return redirect(back)
