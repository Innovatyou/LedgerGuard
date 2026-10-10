from rest_framework import serializers
from rest_framework.response import Response

from apps.accounts.services import get_permissions
from apps.core.api import InstitutionAPIView, InstitutionScopedViewSet

from .models import Branch, Department


class BranchSerializer(serializers.ModelSerializer):
    class Meta:
        model = Branch
        fields = ["id", "code", "name", "state", "address", "is_head_office", "is_active"]


class DepartmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Department
        fields = ["id", "code", "name", "kind", "branch", "is_active"]

    def validate_branch(self, branch):
        if branch is not None and branch.institution_id != self.context["institution"].pk:
            raise serializers.ValidationError("Branch belongs to a different institution.")
        return branch


_ORG_PERMS = {
    "list": "coa.view",
    "retrieve": "coa.view",
    "create": "org.manage",
    "update": "org.manage",
    "partial_update": "org.manage",
    "destroy": "org.manage",
}


class BranchViewSet(InstitutionScopedViewSet):
    model = Branch
    serializer_class = BranchSerializer
    permission_map = _ORG_PERMS
    http_method_names = ["get", "post", "put", "patch", "head", "options"]


class DepartmentViewSet(InstitutionScopedViewSet):
    model = Department
    serializer_class = DepartmentSerializer
    permission_map = _ORG_PERMS
    http_method_names = ["get", "post", "put", "patch", "head", "options"]


class MeView(InstitutionAPIView):
    def get(self, request):
        return Response(
            {
                "username": request.user.username,
                "institution": {
                    "id": self.institution.pk,
                    "code": self.institution.code,
                    "name": self.institution.name,
                },
                "permissions": sorted(get_permissions(request.user, self.institution)),
            }
        )
