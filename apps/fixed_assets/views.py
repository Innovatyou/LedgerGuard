from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import View

from apps.accounting.models import Account, FinancialPeriod
from apps.core.exceptions import BusinessRuleError
from apps.core.views_mixins import InstitutionRequiredMixin, run_action, scoped_get

from . import services
from .forms import AssetCategoryForm, FixedAssetForm
from .models import AssetCategory, DepreciationRun, FixedAsset


def _date(value, default=None):
    try:
        return date.fromisoformat(value) if value else default
    except ValueError:
        return default


class AssetScopeMixin:
    def assets(self):
        qs = FixedAsset.objects.filter(institution=self.institution).select_related("category", "branch")
        return qs if self.branch_scope is None else qs.filter(branch_id__in=self.branch_scope)


class RegisterView(AssetScopeMixin, InstitutionRequiredMixin, View):
    permission_required = "asset.view"

    def get(self, request):
        services.sync_statuses(self.institution)
        as_of = _date(request.GET.get("as_of"), date.today())
        rows, totals = [], {"cost": Decimal(0), "accumulated": Decimal(0), "nbv": Decimal(0)}
        status = request.GET.get("status", "")
        assets = self.assets()
        if status:
            assets = assets.filter(status=status)
        for asset in assets:
            accumulated = services.accumulated_depreciation(asset, as_of)
            rows.append((asset, accumulated, asset.cost - accumulated))
            if asset.status != FixedAsset.Status.DISPOSED:
                totals["cost"] += asset.cost
                totals["accumulated"] += accumulated
                totals["nbv"] += asset.cost - accumulated
        return render(
            request,
            "fixed_assets/register.html",
            {
                "rows": rows,
                "totals": totals,
                "as_of": as_of,
                "status": status,
                "statuses": FixedAsset.Status.choices,
                "controls": services.register_control(self.institution, as_of) if self.branch_scope is None else None,
                "categories": AssetCategory.objects.filter(institution=self.institution),
            },
        )


class AssetCreateView(InstitutionRequiredMixin, View):
    permission_required = "asset.manage"

    def get(self, request):
        return render(
            request,
            "fixed_assets/form.html",
            {"form": FixedAssetForm(institution=self.institution), "title": "Add fixed asset"},
        )

    def post(self, request):
        form = FixedAssetForm(request.POST, institution=self.institution)
        if form.is_valid():
            data = {k: v for k, v in form.cleaned_data.items() if v not in (None, "")}
            data.setdefault("residual_value", Decimal("0"))
            credit = data.pop("credit_account", None)
            try:
                asset = services.create_asset(self.institution, request.user, credit_account=credit, **data)
            except BusinessRuleError as exc:
                form.add_error(None, exc.message)
            else:
                messages.success(request, f"Asset {asset.tag} added to the register.")
                return redirect("fixed_assets:detail", pk=asset.pk)
        return render(request, "fixed_assets/form.html", {"form": form, "title": "Add fixed asset"})


class CategoryCreateView(InstitutionRequiredMixin, View):
    permission_required = "asset.manage"

    def get(self, request):
        return render(
            request,
            "fixed_assets/form.html",
            {"form": AssetCategoryForm(institution=self.institution), "title": "Add asset category"},
        )

    def post(self, request):
        form = AssetCategoryForm(request.POST, institution=self.institution)
        if form.is_valid():
            form.save()
            messages.success(request, "Asset category saved.")
            return redirect("fixed_assets:register")
        return render(request, "fixed_assets/form.html", {"form": form, "title": "Add asset category"})


class AssetDetailView(AssetScopeMixin, InstitutionRequiredMixin, View):
    permission_required = "asset.view"

    def get(self, request, pk):
        services.sync_statuses(self.institution)
        asset = get_object_or_404(self.assets(), pk=pk)
        return render(
            request,
            "fixed_assets/detail.html",
            {
                "asset": asset,
                "accumulated": services.accumulated_depreciation(asset),
                "nbv": services.net_book_value(asset),
                "history": asset.depreciation_lines.select_related("run__period", "run__journal").order_by(
                    "run__period__start_date"
                ),
                "accounts": Account.objects.filter(
                    institution=self.institution, is_active=True, account_type="POSTING"
                ),
            },
        )

    def post(self, request, pk):
        asset = get_object_or_404(self.assets(), pk=pk)
        back = reverse("fixed_assets:detail", args=[asset.pk])
        try:
            proceeds = Decimal(request.POST.get("proceeds") or "0")
        except InvalidOperation:
            messages.error(request, "Enter valid proceeds.")
            return redirect(back)
        account = None
        if request.POST.get("proceeds_account"):
            account = get_object_or_404(Account, pk=request.POST["proceeds_account"], institution=self.institution)
        return run_action(
            request,
            lambda: services.prepare_disposal(
                asset,
                request.user,
                disposal_date=_date(request.POST.get("disposal_date"), date.today()),
                proceeds=proceeds,
                proceeds_account=account,
            ),
            "Disposal journal prepared and submitted for approval.",
            back,
        )


class DepreciationView(InstitutionRequiredMixin, View):
    permission_required = "asset.view"

    def get(self, request):
        services.sync_statuses(self.institution)
        periods = FinancialPeriod.objects.filter(institution=self.institution, status="OPEN").order_by("start_date")
        period = None
        if request.GET.get("period"):
            period = scoped_get(request, FinancialPeriod, pk=request.GET["period"])
        preview = services.preview_run(self.institution, period) if period else None
        return render(
            request,
            "fixed_assets/depreciation.html",
            {
                "periods": periods,
                "period": period,
                "preview": preview,
                "runs": DepreciationRun.objects.filter(institution=self.institution).select_related(
                    "period", "journal", "prepared_by"
                ),
            },
        )

    def post(self, request):
        back = reverse("fixed_assets:depreciation")
        if request.POST.get("action") == "run":
            period = scoped_get(request, FinancialPeriod, pk=request.POST.get("period"))
            return run_action(
                request,
                lambda: services.run_depreciation(self.institution, request.user, period),
                "Depreciation journal prepared and submitted for approval.",
                back,
            )
        run = scoped_get(request, DepreciationRun, pk=request.POST.get("run"))
        return run_action(
            request,
            lambda: services.cancel_run(run, request.user, request.POST.get("reason", "")),
            "Depreciation run cancelled.",
            back,
        )
