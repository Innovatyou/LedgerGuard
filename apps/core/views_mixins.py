from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect

from apps.accounts.services import branch_scope, has_perm

from .exceptions import BusinessRuleError


class InstitutionRequiredMixin(LoginRequiredMixin):
    """
    Require an authenticated user with an active institution and (optionally)
    one or more permission codes in that institution.
    """

    permission_required: str | tuple[str, ...] | None = None

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        self.institution = getattr(request, "institution", None)
        if self.institution is None:
            raise PermissionDenied("You are not assigned to an active institution.")
        required = self.permission_required
        if isinstance(required, str):
            required = (required,)
        for code in required or ():
            if not has_perm(request.user, self.institution, code):
                raise PermissionDenied(f"Permission required: {code}")
        return super().dispatch(request, *args, **kwargs)

    def can(self, code):
        return has_perm(self.request.user, self.institution, code)

    @property
    def branch_scope(self):
        """``None`` if the user may see every branch, else the set of permitted branch ids."""
        if not hasattr(self, "_branch_scope"):
            self._branch_scope = branch_scope(self.request.user, self.institution)
        return self._branch_scope


class ScopedQuerysetMixin(InstitutionRequiredMixin):
    """Restrict list/detail views to the active institution at query level."""

    model = None

    def get_queryset(self):
        return self.model.objects.filter(institution=self.institution)


def scoped_get(request, model, **lookup):
    """``get_object_or_404`` restricted to the request's institution."""
    return get_object_or_404(model, institution=request.institution, **lookup)


def run_action(request, func, success_message, redirect_to, success_redirect=None):
    """
    Run a service call, translating rule violations into user messages.
    ``success_redirect`` may be a callable receiving the service result.
    """
    try:
        result = func()
    except BusinessRuleError as exc:
        messages.error(request, exc.message)
        return redirect(redirect_to)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect(redirect_to)
    if success_message:
        messages.success(request, success_message)
    return redirect(success_redirect(result) if success_redirect else redirect_to)
