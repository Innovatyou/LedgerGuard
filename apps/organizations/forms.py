from django import forms

from .models import Branch, Department, Institution


class InstitutionSettingsForm(forms.ModelForm):
    class Meta:
        model = Institution
        fields = [
            "name",
            "licence_number",
            "rc_number",
            "address",
            "base_currency",
            "amount_decimal_places",
            "rounding_mode",
            "financial_year_start_month",
            "backdate_limit_days",
            "reconciliation_tolerance",
            "require_independent_approval",
            "require_config_approval",
            "require_mfa",
            "document_retention_years",
        ]
        widgets = {"address": forms.Textarea(attrs={"rows": 2})}


class BranchForm(forms.ModelForm):
    class Meta:
        model = Branch
        fields = ["code", "name", "state", "address", "is_head_office", "is_active"]
        widgets = {"address": forms.Textarea(attrs={"rows": 2})}


class DepartmentForm(forms.ModelForm):
    class Meta:
        model = Department
        fields = ["code", "name", "kind", "branch", "is_active"]

    def __init__(self, *args, institution, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["branch"].queryset = Branch.objects.filter(institution=institution)
