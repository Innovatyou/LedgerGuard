import json
import re

from django import forms

from .models import STANDARD_FIELDS, ImportProfile


class ImportProfileForm(forms.ModelForm):
    date_formats_text = forms.CharField(
        required=False,
        label="Date formats",
        help_text="Comma-separated strftime formats, e.g. %d/%m/%Y, %d-%b-%Y. Blank: common formats.",
    )
    indicator_debit_text = forms.CharField(required=False, label="Debit indicator values", help_text="e.g. DR, D")
    indicator_credit_text = forms.CharField(required=False, label="Credit indicator values", help_text="e.g. CR, C")

    class Meta:
        model = ImportProfile
        fields = [
            "name",
            "source_type",
            "source_system",
            "channel",
            "header_row",
            "sheet_name",
            "amount_mode",
            "signed_positive_is",
            "default_currency",
            "default_account_reference",
            "time_from_reference",
            "reference_time_pattern",
            "is_active",
        ]

    def clean_reference_time_pattern(self):
        pattern = self.cleaned_data.get("reference_time_pattern", "").strip()
        if pattern:
            try:
                compiled = re.compile(pattern)
            except re.error as exc:
                raise forms.ValidationError(f"Invalid regular expression: {exc}") from exc
            if not {"HH", "MM"} <= set(compiled.groupindex):
                raise forms.ValidationError("The pattern needs named groups HH and MM, e.g. (?P<HH>\\d{2}).")
        return pattern

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("time_from_reference") == "PATTERN" and not cleaned.get("reference_time_pattern"):
            self.add_error("reference_time_pattern", "Enter a pattern or choose another time source.")
        return cleaned

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        mapping = self.instance.column_mapping or {}
        for key, label in STANDARD_FIELDS.items():
            self.fields[f"map_{key}"] = forms.CharField(
                required=False, label=f"Column for: {label}", initial=mapping.get(key, "")
            )
        self.fields["date_formats_text"].initial = ", ".join(self.instance.date_formats or [])
        self.fields["indicator_debit_text"].initial = ", ".join(self.instance.indicator_debit_values or [])
        self.fields["indicator_credit_text"].initial = ", ".join(self.instance.indicator_credit_values or [])

    @staticmethod
    def _split(text):
        return [p.strip() for p in (text or "").split(",") if p.strip()]

    def mapping_fields(self):
        return [self[f"map_{key}"] for key in STANDARD_FIELDS]

    def save(self, commit=True):
        profile = super().save(commit=False)
        profile.column_mapping = {
            key: self.cleaned_data[f"map_{key}"].strip()
            for key in STANDARD_FIELDS
            if self.cleaned_data.get(f"map_{key}", "").strip()
        }
        profile.date_formats = self._split(self.cleaned_data.get("date_formats_text"))
        profile.indicator_debit_values = self._split(self.cleaned_data.get("indicator_debit_text"))
        profile.indicator_credit_values = self._split(self.cleaned_data.get("indicator_credit_text"))
        if commit:
            profile.save()
        return profile


class UploadForm(forms.Form):
    profile = forms.ModelChoiceField(queryset=ImportProfile.objects.none())
    file = forms.FileField(help_text="Accepted: .csv, .xlsx. Macro-enabled workbooks are rejected.")

    def __init__(self, *args, institution, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["profile"].queryset = ImportProfile.objects.filter(institution=institution, is_active=True)


class RowCorrectionForm(forms.Form):
    changes = forms.CharField(widget=forms.HiddenInput)
    note = forms.CharField(max_length=500)

    def clean_changes(self):
        try:
            value = json.loads(self.cleaned_data["changes"])
        except json.JSONDecodeError as exc:
            raise forms.ValidationError("Invalid changes") from exc
        if not isinstance(value, dict):
            raise forms.ValidationError("Invalid changes")
        return {str(k): str(v) for k, v in value.items()}
