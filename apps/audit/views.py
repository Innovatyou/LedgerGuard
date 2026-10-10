from datetime import date

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import ListView, View

from apps.accounts.models import User
from apps.core.exceptions import BusinessRuleError
from apps.core.views_mixins import InstitutionRequiredMixin, run_action, scoped_get

from . import services
from .models import CorrectiveAction, Engagement, Finding, Workpaper


def _members(institution):
    return (
        User.objects.filter(memberships__institution=institution, memberships__is_active=True, is_active=True)
        .distinct()
        .order_by("username")
    )


class EngagementForm(forms.ModelForm):
    class Meta:
        model = Engagement
        fields = [
            "title",
            "kind",
            "scope",
            "objectives",
            "period_start",
            "period_end",
            "planned_start",
            "planned_end",
            "branch",
            "lead",
            "team",
        ]
        widgets = {
            f: forms.DateInput(attrs={"type": "date"})
            for f in ("period_start", "period_end", "planned_start", "planned_end")
        } | {
            "scope": forms.Textarea(attrs={"rows": 3}),
            "objectives": forms.Textarea(attrs={"rows": 3}),
            "team": forms.CheckboxSelectMultiple,
        }

    def __init__(self, *args, institution, **kwargs):
        super().__init__(*args, **kwargs)
        members = _members(institution)
        self.fields["lead"].queryset = members
        self.fields["team"].queryset = members
        self.fields["branch"].queryset = self.fields["branch"].queryset.filter(institution=institution)


class FindingForm(forms.ModelForm):
    class Meta:
        model = Finding
        fields = ["title", "rating", "workpaper", "condition", "criteria", "cause", "effect", "recommendation", "owner"]
        widgets = {
            f: forms.Textarea(attrs={"rows": 2}) for f in ("condition", "criteria", "cause", "effect", "recommendation")
        }

    def __init__(self, *args, engagement, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["workpaper"].queryset = engagement.workpapers.all()
        self.fields["owner"].queryset = _members(engagement.institution)


class EngagementListView(InstitutionRequiredMixin, ListView):
    permission_required = "audit.view"
    template_name = "audit/engagement_list.html"

    def get_queryset(self):
        return Engagement.objects.filter(institution=self.institution).select_related("lead")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["open_findings"] = (
            Finding.objects.filter(institution=self.institution)
            .exclude(status=Finding.Status.CLOSED)
            .select_related("engagement", "owner")
        )
        ctx["overdue_actions"] = CorrectiveAction.objects.filter(
            finding__institution=self.institution, status=CorrectiveAction.Status.OPEN, due_date__lt=date.today()
        ).select_related("finding", "owner")
        return ctx


class EngagementCreateView(InstitutionRequiredMixin, View):
    permission_required = "audit.manage"

    def get(self, request):
        return render(
            request,
            "audit/form.html",
            {"form": EngagementForm(institution=self.institution), "title": "New audit engagement"},
        )

    def post(self, request):
        form = EngagementForm(request.POST, institution=self.institution)
        if form.is_valid():
            data = dict(form.cleaned_data)
            team = data.pop("team")
            try:
                engagement = services.create_engagement(self.institution, request.user, team=team, **data)
            except BusinessRuleError as exc:
                form.add_error(None, exc.message)
            else:
                return redirect("audit:engagement", pk=engagement.pk)
        return render(request, "audit/form.html", {"form": form, "title": "New audit engagement"})


class EngagementDetailView(InstitutionRequiredMixin, View):
    permission_required = "audit.view"

    def get(self, request, pk):
        engagement = scoped_get(request, Engagement, pk=pk)
        return render(
            request,
            "audit/engagement_detail.html",
            {
                "engagement": engagement,
                "workpapers": engagement.workpapers.select_related("prepared_by", "reviewed_by"),
                "findings": engagement.findings.select_related("owner"),
                "finding_form": FindingForm(engagement=engagement),
            },
        )

    def post(self, request, pk):
        engagement = scoped_get(request, Engagement, pk=pk)
        back = reverse("audit:engagement", args=[engagement.pk])
        action = request.POST.get("action")
        user = request.user
        if action == "advance":
            return run_action(
                request, lambda: services.advance_engagement(engagement, user), "Engagement advanced.", back
            )
        if action == "workpaper":
            return run_action(
                request,
                lambda: services.create_workpaper(
                    engagement,
                    user,
                    reference=request.POST.get("reference", ""),
                    title=request.POST.get("title", ""),
                    objective=request.POST.get("objective", ""),
                    procedures=request.POST.get("procedures", ""),
                ),
                "Workpaper created.",
                back,
            )
        if action == "finding":
            form = FindingForm(request.POST, engagement=engagement)
            if not form.is_valid():
                messages.error(request, "Complete the required finding fields.")
                return redirect(back)
            return run_action(
                request, lambda: services.raise_finding(engagement, user, **form.cleaned_data), "Finding raised.", back
            )
        wp = get_object_or_404(Workpaper, pk=request.POST.get("workpaper"), engagement=engagement)
        if action == "wp_update":

            def update():
                if wp.status not in (Workpaper.Status.DRAFT, Workpaper.Status.RETURNED) or wp.prepared_by_id != user.pk:
                    raise BusinessRuleError("Only the preparer can edit a draft or returned workpaper.")
                wp.procedures = request.POST.get("procedures", wp.procedures)
                wp.conclusion = request.POST.get("conclusion", wp.conclusion)
                wp.save()

            return run_action(request, update, "Workpaper updated.", back)
        if action == "wp_submit":
            return run_action(request, lambda: services.submit_workpaper(wp, user), "Workpaper submitted.", back)
        if action in ("wp_approve", "wp_return"):
            return run_action(
                request,
                lambda: services.review_workpaper(
                    wp, user, approve=action == "wp_approve", notes=request.POST.get("notes", "")
                ),
                "Review recorded.",
                back,
            )
        messages.error(request, "Unknown action.")
        return redirect(back)


class FindingDetailView(InstitutionRequiredMixin, View):
    permission_required = "audit.view"

    def get(self, request, pk):
        finding = scoped_get(request, Finding, pk=pk)
        return render(
            request,
            "audit/finding_detail.html",
            {
                "finding": finding,
                "actions": finding.actions.select_related("owner", "verified_by"),
                "members": _members(self.institution),
            },
        )

    def post(self, request, pk):
        finding = scoped_get(request, Finding, pk=pk)
        back = reverse("audit:finding", args=[finding.pk])
        action = request.POST.get("action")
        user = request.user
        if action == "respond":
            return run_action(
                request,
                lambda: services.respond_to_finding(finding, user, request.POST.get("response", "")),
                "Response recorded.",
                back,
            )
        if action == "add_action":
            owner = _members(self.institution).filter(pk=request.POST.get("owner")).first()
            try:
                due = date.fromisoformat(request.POST.get("due_date", ""))
            except ValueError:
                messages.error(request, "Enter a valid due date.")
                return redirect(back)
            if owner is None:
                messages.error(request, "Select an action owner.")
                return redirect(back)
            return run_action(
                request,
                lambda: services.add_corrective_action(
                    finding, user, description=request.POST.get("description", ""), owner=owner, due_date=due
                ),
                "Corrective action added.",
                back,
            )
        if action == "close":
            return run_action(request, lambda: services.close_finding(finding, user), "Finding closed.", back)
        ca = get_object_or_404(CorrectiveAction, pk=request.POST.get("ca"), finding=finding)
        if action == "complete":
            return run_action(
                request,
                lambda: services.complete_action(ca, user, request.POST.get("notes", "")),
                "Action marked complete.",
                back,
            )
        if action == "verify":
            return run_action(request, lambda: services.verify_action(ca, user), "Action verified.", back)
        messages.error(request, "Unknown action.")
        return redirect(back)
