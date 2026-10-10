import time
import uuid

from .context import reset_request_context, set_request_context
from .tenancy import resolve_institution


class InstitutionMiddleware:
    """
    Resolve the active institution for the authenticated user.

    The active institution is chosen from the user's *active memberships* only.
    A session value that points at an institution the user is not a member of
    is ignored, so tampering with the session or URL cannot widen access.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.institution = resolve_institution(request)
        return self.get_response(request)


class RequestContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        ip = forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR")
        request_id = request.META.get("HTTP_X_REQUEST_ID") or uuid.uuid4().hex
        tokens = set_request_context(ip, request_id)
        started = time.monotonic()
        try:
            response = self.get_response(request)
        finally:
            reset_request_context(tokens)
        response["X-Request-ID"] = request_id
        response["X-Response-Time-ms"] = f"{(time.monotonic() - started) * 1000:.0f}"
        return response


CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
    "frame-ancestors 'none'"
)


class SecurityHeadersMiddleware:
    """Content-Security-Policy and related headers. Scripts are only loaded from the application's own origin."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        if request.path.startswith(("/accounts/", "/api/")) or response.get("Content-Disposition"):
            response.setdefault("Cache-Control", "no-store")
        return response
