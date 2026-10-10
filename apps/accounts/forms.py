from django import forms
from django.contrib.auth.password_validation import validate_password

from .models import Membership, Role, User
from .permissions import PERMISSIONS


class LoginForm(forms.Form):
    username = forms.CharField(
        max_length=150, widget=forms.TextInput(attrs={"autofocus": True, "autocomplete": "username"})
    )
    password = forms.CharField(widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}))


class MFACodeForm(forms.Form):
    code = forms.CharField(
        max_length=8,
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "inputmode": "numeric", "autofocus": True}),
    )


class UserCreateForm(forms.ModelForm):
    password = forms.CharField(widget=forms.PasswordInput, help_text="Minimum 12 characters.")
    role = forms.ModelChoiceField(queryset=Role.objects.none())
    branch = forms.ModelChoiceField(queryset=None, required=False, help_text="Leave blank for all branches.")

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "job_title", "phone"]

    def __init__(self, *args, institution, **kwargs):
        super().__init__(*args, **kwargs)
        from django.db.models import Q

        from apps.organizations.models import Branch

        self.fields["role"].queryset = Role.objects.filter(
            Q(institution__isnull=True) | Q(institution=institution)
        ).exclude(code="super_admin")
        self.fields["branch"].queryset = Branch.objects.filter(institution=institution)

    def clean_password(self):
        password = self.cleaned_data["password"]
        validate_password(password)
        return password


class MembershipForm(forms.ModelForm):
    class Meta:
        model = Membership
        fields = ["role", "branch", "is_active"]

    def __init__(self, *args, institution, **kwargs):
        super().__init__(*args, **kwargs)
        from django.db.models import Q

        from apps.organizations.models import Branch

        self.fields["role"].queryset = Role.objects.filter(
            Q(institution__isnull=True) | Q(institution=institution)
        ).exclude(code="super_admin")
        self.fields["branch"].queryset = Branch.objects.filter(institution=institution)


class RoleForm(forms.ModelForm):
    permissions = forms.MultipleChoiceField(
        choices=sorted(PERMISSIONS.items()), widget=forms.CheckboxSelectMultiple, required=False
    )

    class Meta:
        model = Role
        fields = ["code", "name", "description", "permissions"]
