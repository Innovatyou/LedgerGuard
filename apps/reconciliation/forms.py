from django import forms

from apps.accounting.models import Account

from .models import COMPOSITE_FIELDS, REFERENCE_FIELDS, MatchingRule, Reconciliation

REF_CHOICES = [(k, k.replace("_", " ").capitalize()) for k in REFERENCE_FIELDS] + [
    ("narration_contains", "Narration contains the other side's reference")
]


class MatchingRuleForm(forms.ModelForm):
    left_reference_field = forms.ChoiceField(choices=REF_CHOICES)
    right_reference_field = forms.ChoiceField(choices=REF_CHOICES[:-1])
    composite_fields = forms.MultipleChoiceField(
        choices=[(f, f) for f in sorted(COMPOSITE_FIELDS)], required=False, widget=forms.CheckboxSelectMultiple
    )

    class Meta:
        model = MatchingRule
        fields = [
            "name",
            "strategy",
            "priority",
            "is_active",
            "recon_type",
            "date_tolerance_days",
            "amount_tolerance",
            "auto_accept",
            "left_reference_field",
            "right_reference_field",
            "composite_fields",
            "max_group_size",
            "similarity_threshold",
            "description",
        ]


class ReconciliationForm(forms.ModelForm):
    class Meta:
        model = Reconciliation
        fields = [
            "name",
            "recon_type",
            "currency",
            "period_start",
            "period_end",
            "gl_account",
            "left_label",
            "left_source_type",
            "left_source_system",
            "left_account_reference",
            "left_inflow_side",
            "opening_left_balance",
            "closing_left_balance",
            "right_label",
            "right_source_type",
            "right_source_system",
            "right_account_reference",
            "right_inflow_side",
            "opening_right_balance",
            "closing_right_balance",
            "rules",
        ]
        widgets = {
            "period_start": forms.DateInput(attrs={"type": "date"}),
            "period_end": forms.DateInput(attrs={"type": "date"}),
            "rules": forms.CheckboxSelectMultiple,
        }
        help_texts = {
            "opening_left_balance": "Inflow-positive: a credit balance on a bank statement is positive.",
            "opening_right_balance": "Inflow-positive: a debit balance on a GL asset account is positive.",
        }

    def __init__(self, *args, institution, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["gl_account"].queryset = Account.objects.filter(
            institution=institution, reconciliation_required=True
        )
        self.fields["rules"].queryset = MatchingRule.objects.filter(institution=institution, is_active=True)
        self.fields["currency"].initial = institution.base_currency


class ManualMatchForm(forms.Form):
    left = forms.CharField(required=False)
    right = forms.CharField(required=False)
    note = forms.CharField(max_length=500)

    @staticmethod
    def _ids(text):
        return [int(p) for p in (text or "").replace(" ", "").split(",") if p.isdigit()]

    def left_ids(self):
        return self._ids(self.cleaned_data.get("left"))

    def right_ids(self):
        return self._ids(self.cleaned_data.get("right"))
