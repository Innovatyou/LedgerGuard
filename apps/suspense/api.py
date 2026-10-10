from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.api import InstitutionScopedViewSet

from . import services
from .models import SuspenseItem


class SuspenseItemSerializer(serializers.ModelSerializer):
    age_days = serializers.SerializerMethodField()

    class Meta:
        model = SuspenseItem
        exclude = ["institution"]
        read_only_fields = [
            "reference",
            "status",
            "resolution_action",
            "resolution_notes",
            "related_journal",
            "source_transaction",
            "reconciliation",
            "created_by",
            "proposed_by",
            "proposed_at",
            "approved_by",
            "date_resolved",
            "last_escalated_at",
            "escalation_level",
        ]

    def get_age_days(self, obj):
        return obj.age_days()


class SuspenseItemViewSet(InstitutionScopedViewSet):
    model = SuspenseItem
    serializer_class = SuspenseItemSerializer
    http_method_names = ["get", "post", "head", "options"]
    permission_map = {
        "list": "suspense.view",
        "retrieve": "suspense.view",
        "aging": "suspense.view",
        "create": "suspense.manage",
        "propose": "suspense.manage",
        "approve": "suspense.approve",
    }

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        item = services.create_item(self.institution, request.user, **ser.validated_data)
        return Response(self.get_serializer(item).data, status=201)

    @action(detail=True, methods=["post"])
    def propose(self, request, pk=None):
        item = services.propose_resolution(
            self.get_object(), request.user, request.data.get("resolution"), request.data.get("notes", "")
        )
        return Response(self.get_serializer(item).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        item = services.approve_resolution(self.get_object(), request.user, request.data.get("comment", ""))
        return Response(self.get_serializer(item).data)

    @action(detail=False, methods=["get"])
    def aging(self, request):
        report = services.aging_report(self.institution)
        return Response(
            {
                "as_of": report.as_of,
                "buckets": [
                    {"label": label, "count": c, "total": str(t)}
                    for label, c, t in zip(report.labels, report.bucket_counts, report.bucket_totals, strict=True)
                ],
                "total": str(report.total),
            }
        )
