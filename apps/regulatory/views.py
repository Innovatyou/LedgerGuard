from datetime import date
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import View

from apps.accounts.services import has_perm
from apps.core.activity import log_activity
from apps.core.views_mixins import InstitutionRequiredMixin, run_action, scoped_get
from apps.reporting.exports import CONTENT_TYPES, Column, Table, export_response

from . import services
from .models import RegulatoryReturn, RegulatoryTemplate, TemplateLine, ValidationRule


def _list(text):
    return [p.strip() for p in (text or "").split(",") if p.strip()]


class LineForm(forms.ModelForm):
    selectors = forms.CharField(required=False, help_text="Subcategories, comma-separated (e.g. CASH, DUE_FROM_BANKS)")
    reg_codes = forms.CharField(required=False, label="Regulatory code prefixes")
    acct_codes = forms.CharField(required=False, label="Account code prefixes")
    cats = forms.CharField(required=False, label="Categories", help_text="e.g. ASSET, LIABILITY")

    class Meta:
        model = TemplateLine
        fields = ["code", "label", "order", "kind", "basis", "sign", "formula", "is_total"]


class TemplateListView(InstitutionRequiredMixin, View):
    permission_required = "regulatory.view"

    def get(self, request):
        return render(
            request,
            "regulatory/index.html",
            {
                "templates": RegulatoryTemplate.objects.filter(institution=self.institution),
                "returns": RegulatoryReturn.objects.filter(institution=self.institution).select_related(
                    "template", "prepared_by", "reviewed_by"
                )[:50],
            },
        )

    def post(self, request):
        return run_action(
            request,
            lambda: services.install_starter_template(self.institution, request.user),
            "Starter template installed. Review every line before use.",
            reverse("regulatory:index"),
        )


class TemplateDetailView(InstitutionRequiredMixin, View):
    permission_required = "regulatory.view"

    def get(self, request, pk):
        template = scoped_get(request, RegulatoryTemplate, pk=pk)
        return render(request, "regulatory/template.html", {"template": template, "line_form": LineForm()})

    def post(self, request, pk):
        template = scoped_get(request, RegulatoryTemplate, pk=pk)
        back = reverse("regulatory:template", args=[template.pk])
        if not has_perm(request.user, self.institution, "regulatory.manage"):
            messages.error(request, "Permission required: regulatory.manage")
            return redirect(back)
        action = request.POST.get("action")
        if action == "line":
            form = LineForm(request.POST)
            if form.is_valid():
                line = form.save(commit=False)
                line.template = template
                line.subcategories = _list(form.cleaned_data["selectors"])
                line.regulatory_codes = _list(form.cleaned_data["reg_codes"])
                line.account_codes = _list(form.cleaned_data["acct_codes"])
                line.categories = _list(form.cleaned_data["cats"])
                line.save()
                log_activity("regulatory.line_added", user=request.user, obj=template, details={"line": line.code})
                messages.success(request, f"Line {line.code} added.")
            else:
                messages.error(request, f"Invalid line: {form.errors.as_text()}")
        elif action == "delete_line":
            line = get_object_or_404(TemplateLine, pk=request.POST.get("line"), template=template)
            log_activity("regulatory.line_deleted", user=request.user, obj=template, details={"line": line.code})
            line.delete()
        elif action == "rule":
            ValidationRule.objects.create(
                template=template,
                name=request.POST.get("name", "Rule"),
                expression=request.POST.get("expression", ""),
                tolerance=Decimal(request.POST.get("tolerance") or "0"),
            )
            log_activity("regulatory.rule_added", user=request.user, obj=template)
        return redirect(back)


class ReturnView(InstitutionRequiredMixin, View):
    permission_required = "regulatory.view"

    def get(self, request, pk):
        ret = scoped_get(request, RegulatoryReturn, pk=pk)
        lines = list(ret.template.lines.all())
        table = Table(
            title=f"{ret.template.code} – {ret.template.name}",
            subtitle=f"Period {ret.period_start:%d %b %Y} to {ret.period_end:%d %b %Y} · {ret.get_status_display()}",
            columns=[Column("Line", width=10), Column("Description", width=50), Column("Amount", "money")],
            institution=self.institution.name,
            generated_by=str(request.user),
            notes=[ret.template.description]
            + [
                f"{'PASS' if r['passed'] else 'FAIL'}: {r['rule']} ({r.get('detail') or r.get('difference')})"
                for r in ret.validation_results
            ],
        )
        for line in lines:
            value = ret.values.get(line.code)
            table.rows.append([line.code, line.label, Decimal(value) if value is not None else None])
        fmt = request.GET.get("export")
        if fmt in CONTENT_TYPES and has_perm(request.user, self.institution, "report.export"):
            log_activity(
                "report.exported",
                user=request.user,
                institution=self.institution,
                details={"report": "regulatory", "return": ret.pk, "format": fmt},
            )
            return export_response(table, fmt, f"{ret.template.code}_{ret.period_end:%Y%m%d}")
        return render(
            request,
            "regulatory/return.html",
            {
                "ret": ret,
                "lines": [(ln, ret.values.get(ln.code)) for ln in lines],
                "manual_lines": [ln for ln in lines if ln.kind == "MANUAL"],
                "can_export": has_perm(request.user, self.institution, "report.export"),
            },
        )

    def post(self, request, pk):
        ret = scoped_get(request, RegulatoryReturn, pk=pk)
        back = reverse("regulatory:return", args=[ret.pk])
        if request.POST.get("action") == "review":
            return run_action(
                request,
                lambda: services.review(ret, request.user, request.POST.get("comment", "")),
                "Workpaper signed off.",
                back,
            )
        manual = {k[7:]: v for k, v in request.POST.items() if k.startswith("manual_")}
        return run_action(
            request,
            lambda: services.prepare(
                self.institution, request.user, ret.template, ret.period_start, ret.period_end, manual
            ),
            "Workpaper regenerated.",
            back,
        )


class PrepareView(InstitutionRequiredMixin, View):
    permission_required = "regulatory.manage"

    def post(self, request):
        template = scoped_get(request, RegulatoryTemplate, pk=request.POST.get("template"))
        try:
            start = date.fromisoformat(request.POST.get("period_start", ""))
            end = date.fromisoformat(request.POST.get("period_end", ""))
        except ValueError:
            messages.error(request, "Enter the reporting period.")
            return redirect("regulatory:index")
        return run_action(
            request,
            lambda: services.prepare(self.institution, request.user, template, start, end),
            "Workpaper prepared.",
            reverse("regulatory:index"),
            success_redirect=lambda r: reverse("regulatory:return", args=[r.pk]),
        )
