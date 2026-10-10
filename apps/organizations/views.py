from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.generic import ListView, View

from apps.core.activity import log_activity
from apps.core.views_mixins import InstitutionRequiredMixin, scoped_get

from .forms import BranchForm, DepartmentForm, InstitutionSettingsForm
from .models import Branch, Department


class InstitutionSettingsView(InstitutionRequiredMixin, View):
    permission_required = "org.manage"

    def get(self, request):
        return render(
            request, "organizations/settings.html", {"form": InstitutionSettingsForm(instance=self.institution)}
        )

    def post(self, request):
        before = InstitutionSettingsForm(instance=self.institution).initial
        form = InstitutionSettingsForm(request.POST, instance=self.institution)
        if form.is_valid():
            form.save()
            changes = {k: [str(before.get(k)), str(form.cleaned_data[k])] for k in form.changed_data}
            log_activity(
                "org.settings_changed",
                user=request.user,
                institution=self.institution,
                obj=self.institution,
                details=changes,
            )
            messages.success(request, "Institution settings saved.")
            return redirect("organizations:settings")
        return render(request, "organizations/settings.html", {"form": form})


class BranchListView(InstitutionRequiredMixin, ListView):
    permission_required = "coa.view"
    template_name = "organizations/branch_list.html"

    def get_queryset(self):
        return Branch.objects.filter(institution=self.institution)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["departments"] = Department.objects.filter(institution=self.institution).select_related("branch")
        return ctx


class BranchEditView(InstitutionRequiredMixin, View):
    permission_required = "org.manage"

    def get(self, request, pk=None):
        branch = scoped_get(request, Branch, pk=pk) if pk else None
        return render(request, "organizations/form.html", {"form": BranchForm(instance=branch), "title": "Branch"})

    def post(self, request, pk=None):
        branch = scoped_get(request, Branch, pk=pk) if pk else Branch(institution=self.institution)
        form = BranchForm(request.POST, instance=branch)
        if form.is_valid():
            form.save()
            log_activity("org.branch_saved", user=request.user, obj=form.instance)
            messages.success(request, "Branch saved.")
            return redirect("organizations:branches")
        return render(request, "organizations/form.html", {"form": form, "title": "Branch"})


class DepartmentEditView(InstitutionRequiredMixin, View):
    permission_required = "org.manage"

    def get(self, request, pk=None):
        dept = scoped_get(request, Department, pk=pk) if pk else None
        form = DepartmentForm(instance=dept, institution=self.institution)
        return render(request, "organizations/form.html", {"form": form, "title": "Department / cost centre"})

    def post(self, request, pk=None):
        dept = scoped_get(request, Department, pk=pk) if pk else Department(institution=self.institution)
        form = DepartmentForm(request.POST, instance=dept, institution=self.institution)
        if form.is_valid():
            form.save()
            log_activity("org.department_saved", user=request.user, obj=form.instance)
            messages.success(request, "Department saved.")
            return redirect("organizations:branches")
        return render(request, "organizations/form.html", {"form": form, "title": "Department / cost centre"})
