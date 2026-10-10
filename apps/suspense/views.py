from datetime import date
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import ListView, View

from apps.accounting.models import Account
from apps.accounts.models import User
from apps.approvals.services import history_for
from apps.core.exceptions import BusinessRuleError
from apps.core.views_mixins import InstitutionRequiredMixin, run_action, scoped_get

from . import services
from .models import EscalationRule, SuspenseItem


class SuspenseItemForm(forms.ModelForm):
    class Meta:
        model = SuspenseItem
        fields = [
            "account",
            "transaction_reference",
            "original_date",
            "original_amount",
            "side",
            "currency",
            "source_system",
            "narration",
            "reason",
            "assigned_to",
        ]
        widgets = {
            "original_date": forms.DateInput(attrs={"type": "date"}),
            "narration": forms.Textarea(attrs={"rows": 2}),
            "reason": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, institution, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = Account.objects.filter(
            institution=institution, is_suspense_account=True, is_active=True
        )
        self.fields["assigned_to"].queryset = User.objects.filter(
            memberships__institution=institution, memberships__is_active=True
        ).distinct()
        self.fields["currency"].initial = institution.base_currency


def _members(institution):
    return (
        User.objects.filter(memberships__institution=institution, memberships__is_active=True, is_active=True)
        .distinct()
        .order_by("username")
    )


class SuspenseListView(InstitutionRequiredMixin, ListView):
    permission_required = "suspense.view"
    template_name = "suspense/list.html"
    paginate_by = 100

    def get_queryset(self):
        qs = SuspenseItem.objects.filter(institution=self.institution).select_related("account", "assigned_to")
        status = self.request.GET.get("status", "open")
        if status == "open":
            qs = qs.filter(status__in=SuspenseItem.OPEN_STATUSES)
        elif status:
            qs = qs.filter(status=status)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["statuses"] = SuspenseItem.Status.choices
        ctx["status"] = self.request.GET.get("status", "open")
        today = date.today()
        for item in ctx["object_list"]:
            item.age = item.age_days(today)
        return ctx


class SuspenseCreateView(InstitutionRequiredMixin, View):
    permission_required = "suspense.manage"

    def get(self, request):
        return render(request, "suspense/form.html", {"form": SuspenseItemForm(institution=self.institution)})

    def post(self, request):
        form = SuspenseItemForm(request.POST, institution=self.institution)
        if form.is_valid():
            try:
                item = services.create_item(self.institution, request.user, **form.cleaned_data)
            except BusinessRuleError as exc:
                form.add_error(None, exc.message)
            else:
                messages.success(request, f"Suspense item {item.reference} created.")
                return redirect("suspense:detail", pk=item.pk)
        return render(request, "suspense/form.html", {"form": form})


class SuspenseDetailView(InstitutionRequiredMixin, View):
    permission_required = "suspense.view"

    def get(self, request, pk):
        item = scoped_get(request, SuspenseItem, pk=pk)
        return render(
            request,
            "suspense/detail.html",
            {
                "item": item,
                "age": item.age_days(),
                "notes": item.notes.select_related("user"),
                "history": history_for(item),
                "members": _members(self.institution),
                "resolutions": SuspenseItem.Resolution.choices,
                "contra_accounts": Account.objects.filter(
                    institution=self.institution, is_active=True, account_type="POSTING"
                ).exclude(pk=item.account_id),
            },
        )

    def post(self, request, pk):
        item = scoped_get(request, SuspenseItem, pk=pk)
        action = request.POST.get("action")
        back = reverse("suspense:detail", args=[item.pk])
        user = request.user
        text = request.POST.get("text", "")
        if action == "assign":
            assignee = _members(self.institution).filter(pk=request.POST.get("assignee")).first()
            return run_action(request, lambda: services.assign(item, user, assignee), "Item assigned.", back)
        if action == "note":
            return run_action(request, lambda: services.add_note(item, user, text), "Note added.", back)
        if action == "journal":
            contra = get_object_or_404(Account, pk=request.POST.get("contra"), institution=self.institution)
            return run_action(
                request,
                lambda: services.prepare_clearing_journal(item, user, contra_account=contra),
                "Draft clearing journal prepared; submit it for approval.",
                back,
            )
        if action == "propose":
            return run_action(
                request,
                lambda: services.propose_resolution(item, user, request.POST.get("resolution"), text),
                "Resolution submitted for approval.",
                back,
            )
        if action == "approve":
            return run_action(
                request, lambda: services.approve_resolution(item, user, text), "Resolution approved.", back
            )
        if action == "reject":
            return run_action(
                request, lambda: services.reject_resolution(item, user, text), "Resolution rejected.", back
            )
        messages.error(request, "Unknown action.")
        return redirect(back)


class AgingView(InstitutionRequiredMixin, View):
    permission_required = "suspense.view"

    def get(self, request):
        try:
            as_of = date.fromisoformat(request.GET["as_of"]) if request.GET.get("as_of") else date.today()
        except ValueError:
            as_of = date.today()
        report = services.aging_report(self.institution, as_of)
        return render(
            request,
            "suspense/aging.html",
            {
                "report": report,
                "bounds": services.aging_bounds(self.institution),
                "rules": EscalationRule.objects.filter(institution=self.institution),
                "bucket_rows": [
                    (a, list(zip(report.labels, v, strict=True)), sum(v, Decimal("0")))
                    for a, v in report.by_account.items()
                ],
            },
        )

    def post(self, request):
        back = reverse("suspense:aging")
        action = request.POST.get("action")
        if action == "buckets":
            bounds = [b for b in request.POST.get("bounds", "").replace(" ", "").split(",") if b]
            return run_action(
                request,
                lambda: services.set_aging_bounds(self.institution, request.user, bounds),
                "Aging buckets updated.",
                back,
            )
        if action == "rule":
            from apps.accounts.services import require_perm

            def save_rule():
                require_perm(request.user, self.institution, "suspense.configure")
                try:
                    days = int(request.POST.get("min_age_days", ""))
                except ValueError as exc:
                    raise BusinessRuleError("Enter the minimum age in days.") from exc
                return EscalationRule.objects.create(
                    institution=self.institution,
                    name=request.POST.get("name") or f"Over {days} days",
                    min_age_days=days,
                    notify_permission=request.POST.get("notify_permission") or "suspense.approve",
                )

            return run_action(request, save_rule, "Escalation rule added.", back)
        messages.error(request, "Unknown action.")
        return redirect(back)
