from rest_framework import serializers

from apps.core.api import InstitutionScopedViewSet

from .models import CorrectiveAction, Engagement, Finding


class EngagementSerializer(serializers.ModelSerializer):
    class Meta:
        model = Engagement
        exclude = ["institution"]


class CorrectiveActionSerializer(serializers.ModelSerializer):
    class Meta:
        model = CorrectiveAction
        exclude = ["finding"]


class FindingSerializer(serializers.ModelSerializer):
    actions = CorrectiveActionSerializer(many=True, read_only=True)

    class Meta:
        model = Finding
        exclude = ["institution"]


class EngagementViewSet(InstitutionScopedViewSet):
    """Read-only API; changes go through the controlled web workflow."""

    model = Engagement
    serializer_class = EngagementSerializer
    http_method_names = ["get", "head", "options"]
    default_permission = "audit.view"


class FindingViewSet(InstitutionScopedViewSet):
    model = Finding
    serializer_class = FindingSerializer
    http_method_names = ["get", "head", "options"]
    default_permission = "audit.view"
