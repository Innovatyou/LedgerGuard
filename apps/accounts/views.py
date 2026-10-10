from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django.views.generic import ListView, View
from rest_framework.authtoken.models import Token

from apps.core.activity import log_activity
from apps.core.context import current_ip
from apps.core.tenancy import SESSION_KEY, resolve_for_user
from apps.core.views_mixins import InstitutionRequiredMixin

from . import mfa, throttle
from .forms import LoginForm, MembershipForm, MFACodeForm, RoleForm, UserCreateForm
from .models import Membership, Role, User
from .services import clear_permission_cache

PENDING_MFA_KEY = "pending_mfa_user_id"
PENDING_NEXT_KEY = "pending_next"


def _client_ip(request):
    return current_ip() or request.META.get("REMOTE_ADDR")


def _safe_next(request, value):
    if value and url_has_allowed_host_and_scheme(
        value, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return value
    return reverse("reporting:dashboard")


def login_view(request):
    if request.user.is_authenticated:
        return redirect("reporting:dashboard")
    form = LoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        username = form.cleaned_data["username"]
        ip = _client_ip(request)
        if throttle.is_locked(username, ip):
            log_activity("auth.login_locked", details={"username": username})
            messages.error(request, "Too many failed attempts. Try again later or contact your administrator.")
        else:
            user = authenticate(request, username=username, password=form.cleaned_data["password"])
            if user is None:
                throttle.register_failure(username, ip)
                log_activity("auth.login_failed", details={"username": username})
                messages.error(request, "Invalid username or password.")
            else:
                throttle.reset(username, ip)
                next_url = _safe_next(request, request.GET.get("next"))
                if user.mfa_enabled:
                    request.session.cycle_key()
                    request.session[PENDING_MFA_KEY] = user.pk
                    request.session[PENDING_NEXT_KEY] = next_url
                    return redirect("accounts:mfa-verify")
                login(request, user)
                log_activity("auth.login", user=user)
                return redirect(next_url)
    return render(request, "accounts/login.html", {"form": form})


def mfa_verify(request):
    user_id = request.session.get(PENDING_MFA_KEY)
    if not user_id:
        return redirect("accounts:login")
    user = get_object_or_404(User, pk=user_id, is_active=True)
    form = MFACodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        ip = _client_ip(request)
        key = f"mfa:{user.username}"
        if throttle.is_locked(key, ip):
            messages.error(request, "Too many failed attempts. Try again later.")
        else:
            step = mfa.verify(user.mfa_secret, form.cleaned_data["code"], last_used_step=user.mfa_last_used_step)
            if step is None:
                throttle.register_failure(key, ip)
                log_activity("auth.mfa_failed", user=user)
                messages.error(request, "Invalid authentication code.")
            else:
                throttle.reset(key, ip)
                user.mfa_last_used_step = step
                user.save(update_fields=["mfa_last_used_step"])
                next_url = request.session.get(PENDING_NEXT_KEY) or reverse("reporting:dashboard")
                login(request, user, backend="django.contrib.auth.backends.ModelBackend")
                log_activity("auth.login", user=user, details={"mfa": True})
                return redirect(next_url)
    return render(request, "accounts/mfa_verify.html", {"form": form})


@login_required
def mfa_setup(request):
    user = request.user
    secret = request.session.get("mfa_setup_secret")
    if not secret:
        secret = mfa.generate_secret()
        request.session["mfa_setup_secret"] = secret
    form = MFACodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        step = mfa.verify(secret, form.cleaned_data["code"])
        if step is None:
            messages.error(request, "That code is not valid. Check your authenticator app's time and try again.")
        else:
            user.mfa_secret = secret
            user.mfa_enabled = True
            user.mfa_last_used_step = step
            user.save(update_fields=["mfa_secret", "mfa_enabled", "mfa_last_used_step"])
            del request.session["mfa_setup_secret"]
            log_activity("auth.mfa_enabled", user=user)
            messages.success(request, "Multifactor authentication is now enabled.")
            return redirect("accounts:profile")
    return render(
        request,
        "accounts/mfa_setup.html",
        {
            "form": form,
            "secret": secret,
            "uri": mfa.provisioning_uri(secret, user.email or user.username),
        },
    )


@require_POST
def logout_view(request):
    if request.user.is_authenticated:
        log_activity("auth.logout", user=request.user)
    logout(request)
    return redirect("accounts:login")


@login_required
@require_POST
def switch_institution(request):
    institution = resolve_for_user(request.user, request.POST.get("institution"))
    if institution is None:
        messages.error(request, "You do not have access to that institution.")
    else:
        request.session[SESSION_KEY] = institution.pk
        log_activity("auth.institution_switched", user=request.user, institution=institution)
    return redirect("reporting:dashboard")


@login_required
def profile(request):
    token = Token.objects.filter(user=request.user).first()
    if request.method == "POST" and request.POST.get("action") == "token":
        Token.objects.filter(user=request.user).delete()
        token = Token.objects.create(user=request.user)
        log_activity("auth.api_token_issued", user=request.user)
        messages.success(request, "A new API token was generated. Previous tokens are revoked.")
    return render(request, "accounts/profile.html", {"token": token})


class UserListView(InstitutionRequiredMixin, ListView):
    permission_required = "users.view"
    template_name = "accounts/user_list.html"

    def get_queryset(self):
        return (
            Membership.objects.filter(institution=self.institution)
            .select_related("user", "role", "branch")
            .order_by("user__username")
        )


class UserCreateView(InstitutionRequiredMixin, View):
    permission_required = "users.manage"

    def get(self, request):
        return render(request, "accounts/user_form.html", {"form": UserCreateForm(institution=self.institution)})

    def post(self, request):
        form = UserCreateForm(request.POST, institution=self.institution)
        if form.is_valid():
            with transaction.atomic():
                user = form.save(commit=False)
                user.set_password(form.cleaned_data["password"])
                user.save()
                Membership.objects.create(
                    user=user,
                    institution=self.institution,
                    role=form.cleaned_data["role"],
                    branch=form.cleaned_data["branch"],
                )
            log_activity(
                "users.created",
                user=request.user,
                institution=self.institution,
                obj=user,
                details={"role": form.cleaned_data["role"].code},
            )
            messages.success(request, f"User {user.username} created.")
            return redirect("accounts:users")
        return render(request, "accounts/user_form.html", {"form": form})


class MembershipEditView(InstitutionRequiredMixin, View):
    permission_required = "users.manage"

    def _membership(self, pk):
        return get_object_or_404(Membership, pk=pk, institution=self.institution)

    def get(self, request, pk):
        membership = self._membership(pk)
        form = MembershipForm(instance=membership, institution=self.institution)
        return render(request, "accounts/membership_form.html", {"form": form, "membership": membership})

    def post(self, request, pk):
        membership = self._membership(pk)
        if membership.user_id == request.user.pk:
            messages.error(request, "You cannot change your own access.")
            return redirect("accounts:users")
        form = MembershipForm(request.POST, instance=membership, institution=self.institution)
        if form.is_valid():
            form.save()
            clear_permission_cache(membership.user)
            log_activity(
                "users.membership_changed",
                user=request.user,
                institution=self.institution,
                obj=membership,
                details={"role": membership.role.code, "active": membership.is_active},
            )
            messages.success(request, "Access updated.")
            return redirect("accounts:users")
        return render(request, "accounts/membership_form.html", {"form": form, "membership": membership})


class RoleListView(InstitutionRequiredMixin, ListView):
    permission_required = "users.view"
    template_name = "accounts/role_list.html"

    def get_queryset(self):
        return Role.objects.filter(Q(institution__isnull=True) | Q(institution=self.institution)).order_by(
            "-is_system", "name"
        )


class RoleCreateView(InstitutionRequiredMixin, View):
    permission_required = "users.manage"

    def get(self, request):
        return render(request, "accounts/role_form.html", {"form": RoleForm()})

    def post(self, request):
        form = RoleForm(request.POST)
        form.instance.institution = self.institution
        if form.is_valid():
            role = form.save(commit=False)
            role.institution = self.institution
            role.full_clean()
            role.save()
            log_activity(
                "users.role_created",
                user=request.user,
                institution=self.institution,
                obj=role,
                details={"permissions": role.permissions},
            )
            messages.success(request, f"Role {role.name} created.")
            return redirect("accounts:roles")
        return render(request, "accounts/role_form.html", {"form": form})
