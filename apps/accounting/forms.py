from decimal import Decimal

from django import forms

from apps.organizations.models import Branch, Department

from .models import Account, AccountType, FinancialPeriod, Journal


class AccountForm(forms.ModelForm):
    class Meta:
        model = Account
        fields = [
            "code",
            "name",
            "parent",
            "category",
            "subcategory",
            "account_type",
            "normal_balance",
            "currency",
            "branch",
            "department",
            "is_active",
            "effective_from",
            "effective_to",
            "reconciliation_required",
            "is_control_account",
            "is_suspense_account",
            "cash_flow_class",
            "fs_line",
            "regulatory_code",
        ]
        widgets = {
            "effective_from": forms.DateInput(attrs={"type": "date"}),
            "effective_to": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, institution, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.institution = institution
        self.fields["parent"].queryset = Account.objects.filter(
            institution=institution, account_type=AccountType.HEADER
        )
        self.fields["branch"].queryset = Branch.objects.filter(institution=institution)
        self.fields["department"].queryset = Department.objects.filter(institution=institution)
        self.fields["normal_balance"].required = False
        self.fields["normal_balance"].help_text = "Leave blank to derive from the category."


class PeriodForm(forms.ModelForm):
    class Meta:
        model = FinancialPeriod
        fields = ["name", "start_date", "end_date", "is_year_end"]
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
        }


class JournalForm(forms.ModelForm):
    class Meta:
        model = Journal
        fields = [
            "journal_type",
            "transaction_date",
            "accounting_date",
            "description",
            "source_reference",
            "supporting_reference",
            "currency",
            "exchange_rate",
            "branch",
        ]
        widgets = {
            "transaction_date": forms.DateInput(attrs={"type": "date"}),
            "accounting_date": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, institution, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["branch"].queryset = Branch.objects.filter(institution=institution)
        self.fields["journal_type"].choices = [
            c for c in self.fields["journal_type"].choices if c[0] not in ("REVERSAL", "YEAR_END")
        ]
        if not self.instance.pk:
            self.initial.setdefault("currency", institution.base_currency)


class JournalLineForm(forms.Form):
    account = forms.ModelChoiceField(queryset=Account.objects.none(), required=False)
    description = forms.CharField(max_length=255, required=False)
    debit = forms.DecimalField(max_digits=20, decimal_places=2, required=False, min_value=Decimal("0"))
    credit = forms.DecimalField(max_digits=20, decimal_places=2, required=False, min_value=Decimal("0"))
    department = forms.ModelChoiceField(queryset=Department.objects.none(), required=False)

    def __init__(self, *args, institution=None, **kwargs):
        super().__init__(*args, **kwargs)
        if institution is not None:
            self.fields["account"].queryset = Account.objects.filter(
                institution=institution, is_active=True, account_type=AccountType.POSTING
            )
            self.fields["department"].queryset = Department.objects.filter(institution=institution, is_active=True)

    def is_blank(self):
        data = getattr(self, "cleaned_data", {})
        return not data.get("account") and not data.get("debit") and not data.get("credit")


JournalLineFormSet = forms.formset_factory(JournalLineForm, extra=4, can_delete=False)


class ReasonForm(forms.Form):
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}))


class RecurringJournalForm(forms.Form):
    name = forms.CharField(max_length=150)
    description = forms.CharField(max_length=500)
    frequency = forms.ChoiceField(
        choices=[("MONTHLY", "Monthly"), ("QUARTERLY", "Quarterly"), ("ANNUALLY", "Annually")]
    )
    day_of_month = forms.IntegerField(min_value=1, max_value=31, initial=28)
    start_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    end_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
