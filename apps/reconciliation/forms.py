from django import forms

from apps.accounting.models import Account

from .models import COMPOSITE_FIELDS, REFERENCE_FIELDS, MatchingRule, Reconciliation

REF_CHOICES = [(k, k.replace("_", " ").capitalize()) for k in REFERENCE_FIELDS] + [
    ("narration_contains", 'Narration (reference found in narration; reversals: "Reversal of …")')
]


class MatchingRuleForm(forms.ModelForm):
    left_reference_field = forms.ChoiceField(choices=REF_CHOICES)
    right_reference_field = forms.ChoiceField(choices=REF_CHOICES)
    composite_fields = forms.MultipleChoiceField(
        choices=[(f, f) for f in sorted(COMPOSITE_FIELDS)], required=False, widget=forms.CheckboxSelectMultiple
    )

    session_cutoffs_text = forms.CharField(
        required=False,
        label="Session cut-off times",
        help_text="Session bulk: comma-separated HH:MM times in increasing order, e.g. 06:00, 09:00, …, 21:00, 24:00 "
        "(24:00 = end of day).",
    )
    fee_keywords_text = forms.CharField(
        required=False,
        label="Fee keywords",
        help_text="Session bulk: comma-separated narration words for charge lines, e.g. CHARGE, FEE, VAT.",
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
            "bulk_side",
            "session_boundary_minutes",
            "session_label_format",
            "session_label_pattern",
            "description",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["session_cutoffs_text"].initial = ", ".join(self.instance.session_cutoffs or [])
        self.fields["fee_keywords_text"].initial = ", ".join(self.instance.fee_keywords or [])
        self.fields["bulk_side"].required = False
        self.fields["session_boundary_minutes"].required = False
        self.fields["session_label_format"].required = False

    @staticmethod
    def _split(text):
        return [p.strip() for p in (text or "").split(",") if p.strip()]

    def clean(self):
        cleaned = super().clean()
        cleaned["bulk_side"] = cleaned.get("bulk_side") or "LEFT"
        cleaned["session_label_format"] = cleaned.get("session_label_format") or "NONE"
        if cleaned.get("session_boundary_minutes") is None:
            cleaned["session_boundary_minutes"] = 0
        cutoffs = self._split(cleaned.pop("session_cutoffs_text", ""))
        cleaned["session_cutoffs"] = [c.zfill(5) if len(c) == 4 else c for c in cutoffs]
        cleaned["fee_keywords"] = [k.upper() for k in self._split(cleaned.pop("fee_keywords_text", ""))]
        return cleaned

    def _post_clean(self):
        # The list fields are not in Meta.fields; copy them onto the instance before model validation.
        self.instance.session_cutoffs = self.cleaned_data.get("session_cutoffs", [])
        self.instance.fee_keywords = self.cleaned_data.get("fee_keywords", [])
        super()._post_clean()

    def _update_errors(self, errors):
        # Model validation reports the list fields by model name; show them on the text inputs.
        if hasattr(errors, "error_dict"):
            for name in ("session_cutoffs", "fee_keywords"):
                if name in errors.error_dict:
                    errors.error_dict[f"{name}_text"] = errors.error_dict.pop(name)
        super()._update_errors(errors)


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
