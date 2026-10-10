from rest_framework import serializers

from apps.core.api import InstitutionScopedViewSet

from . import services
from .models import FixedAsset


class FixedAssetSerializer(serializers.ModelSerializer):
    accumulated_depreciation = serializers.SerializerMethodField()
    net_book_value = serializers.SerializerMethodField()

    class Meta:
        model = FixedAsset
        exclude = ["institution"]

    def get_accumulated_depreciation(self, obj):
        return str(services.accumulated_depreciation(obj))

    def get_net_book_value(self, obj):
        return str(services.net_book_value(obj))


class FixedAssetViewSet(InstitutionScopedViewSet):
    """Read-only register API; changes go through the controlled web workflow."""

    model = FixedAsset
    serializer_class = FixedAssetSerializer
    http_method_names = ["get", "head", "options"]
    default_permission = "asset.view"

    def get_queryset(self):
        qs = super().get_queryset()
        return qs if self.branch_scope is None else qs.filter(branch_id__in=self.branch_scope)
