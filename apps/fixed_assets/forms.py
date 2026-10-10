from django import forms

from apps.accounting.models import Account, AccountType
from apps.organizations.models import Branch, Department

from .models import AssetCategory, FixedAsset


def _posting_accounts(institution):
    return Account.objects.filter(institution=institution, is_active=True, account_type=AccountType.POSTING)


class AssetCategoryForm(forms.ModelForm):
    class Meta:
        model = AssetCategory
        fields = [
            "code",
            "name",
            "cost_account",
            "accumulated_depreciation_account",
            "depreciation_expense_account",
            "disposal_account",
            "method",
            "useful_life_months",
            "annual_rate",
            "is_active",
        ]

    def __init__(self, *args, institution, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.institution = institution
        for name in (
            "cost_account",
            "accumulated_depreciation_account",
            "depreciation_expense_account",
            "disposal_account",
        ):
            self.fields[name].queryset = _posting_accounts(institution)


class FixedAssetForm(forms.ModelForm):
    credit_account = forms.ModelChoiceField(
        queryset=Account.objects.none(),
        required=False,
        help_text="Optional: prepare a capitalisation journal crediting this account (e.g. payables or bank).",
    )

    class Meta:
        model = FixedAsset
        fields = [
            "tag",
            "name",
            "description",
            "category",
            "branch",
            "department",
            "location",
            "supplier",
            "invoice_reference",
            "acquisition_date",
            "in_service_date",
            "cost",
            "residual_value",
            "useful_life_months",
            "method",
            "annual_rate",
            "opening_accumulated_depreciation",
        ]
        widgets = {
            "acquisition_date": forms.DateInput(attrs={"type": "date"}),
            "in_service_date": forms.DateInput(attrs={"type": "date"}),
            "description": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, institution, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.institution = institution
        self.fields["category"].queryset = AssetCategory.objects.filter(institution=institution, is_active=True)
        self.fields["branch"].queryset = Branch.objects.filter(institution=institution)
        self.fields["department"].queryset = Department.objects.filter(institution=institution)
        self.fields["credit_account"].queryset = _posting_accounts(institution)
        for name in ("useful_life_months", "method", "annual_rate"):
            self.fields[name].required = False
            self.fields[name].help_text = "Blank: use the category default."
