from django.shortcuts import redirect
from django.urls import reverse

_EXEMPT_PREFIXES = ("/accounts/mfa/", "/accounts/logout/", "/accounts/password/", "/static/", "/health/")


class MFAEnforcementMiddleware:
    """
    Users of an institution that requires MFA must enrol before they can use
    the application. (Users with MFA enabled are only logged in after passing
    the second factor; see ``accounts.views.LoginView``.)
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and user.must_change_password
            and not request.path.startswith(_EXEMPT_PREFIXES)
        ):
            return redirect(reverse("accounts:password-change"))
        if (
            user is not None
            and user.is_authenticated
            and not user.mfa_enabled
            and not request.path.startswith(_EXEMPT_PREFIXES)
            and user.memberships.filter(is_active=True, institution__require_mfa=True).exists()
        ):
            return redirect(reverse("accounts:mfa-setup"))
        return self.get_response(request)
