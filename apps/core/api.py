from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.services import has_perm

from .exceptions import BusinessRuleError
from .tenancy import resolve_api_institution


class InstitutionContextMixin:
    """
    Resolve the active institution for every API request after authentication.
    Requests without an accessible institution are rejected before any query.
    """

    permission_map: dict[str, str] = {}
    default_permission: str | None = None

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.institution = resolve_api_institution(request)
        if self.institution is None:
            raise PermissionDenied("No accessible institution for this request.")
        code = self.permission_map.get(getattr(self, "action", None) or request.method.lower(), self.default_permission)
        if code and not has_perm(request.user, self.institution, code):
            raise PermissionDenied(f"Permission required: {code}")

    def handle_exception(self, exc):
        if isinstance(exc, BusinessRuleError):
            return Response({"detail": exc.message, "code": exc.code}, status=status.HTTP_400_BAD_REQUEST)
        if isinstance(exc, DjangoValidationError):
            return Response({"detail": exc.messages}, status=status.HTTP_400_BAD_REQUEST)
        return super().handle_exception(exc)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["institution"] = getattr(self, "institution", None)
        return context


class InstitutionScopedViewSet(InstitutionContextMixin, viewsets.ModelViewSet):
    model = None
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return self.model.objects.filter(institution=self.institution)

    def perform_create(self, serializer):
        serializer.save(institution=self.institution)


class InstitutionAPIView(InstitutionContextMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]
