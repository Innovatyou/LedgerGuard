from datetime import date

from django.shortcuts import redirect, render
from django.views.generic import View

from apps.core.views_mixins import InstitutionRequiredMixin, run_action
from apps.imports.models import Channel
from apps.reconciliation.models import Reconciliation, ReconType

from . import services


def _date(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


class SettlementDashboardView(InstitutionRequiredMixin, View):
    permission_required = "settlement.view"

    def get(self, request):
        params = request.GET
        batches = services.settlement_summary(
            self.institution,
            date_from=_date(params.get("from")),
            date_to=_date(params.get("to")),
            channel=params.get("channel") or None,
            account_reference=params.get("account") or None,
        )
        totals = {
            "records": sum(b.records for b in batches),
            "gross": sum(b.gross for b in batches),
            "fees": sum(b.fees for b in batches),
            "taxes": sum(b.taxes for b in batches),
            "net": sum(b.net_computed for b in batches),
            "exceptions": sum(len(b.exceptions) for b in batches),
            "unmatched_batches": sum(1 for b in batches if not b.fully_matched),
        }
        recons = Reconciliation.objects.filter(institution=self.institution, recon_type=ReconType.SETTLEMENT)[:20]
        return render(
            request,
            "settlements/dashboard.html",
            {
                "batches": batches,
                "totals": totals,
                "channels": Channel.choices[1:],
                "recons": recons,
                "params": params,
            },
        )

    def post(self, request):
        if request.POST.get("action") == "install_rules":
            return run_action(
                request,
                lambda: services.install_default_rules(self.institution, request.user),
                "Default settlement matching rules installed.",
                "settlements:dashboard",
            )
        return redirect("settlements:dashboard")
