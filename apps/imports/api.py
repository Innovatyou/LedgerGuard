from django.shortcuts import get_object_or_404
from rest_framework import mixins, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.core.api import InstitutionContextMixin, InstitutionScopedViewSet

from . import services
from .models import ImportBatch, ImportProfile, ImportRow, SourceTransaction


class ImportProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImportProfile
        fields = [
            "id",
            "name",
            "source_type",
            "source_system",
            "channel",
            "column_mapping",
            "header_row",
            "sheet_name",
            "date_formats",
            "amount_mode",
            "signed_positive_is",
            "indicator_debit_values",
            "indicator_credit_values",
            "default_currency",
            "default_account_reference",
            "is_active",
        ]

    def validate(self, attrs):
        probe = ImportProfile(
            **{
                **(
                    {}
                    if self.instance is None
                    else {f: getattr(self.instance, f) for f in self.Meta.fields if f != "id"}
                ),
                **attrs,
            }
        )
        services.validate_profile_mapping(probe)
        return attrs


class ImportProfileViewSet(InstitutionScopedViewSet):
    model = ImportProfile
    serializer_class = ImportProfileSerializer
    http_method_names = ["get", "post", "put", "patch", "head", "options"]
    permission_map = {
        "list": "import.view",
        "retrieve": "import.view",
        "create": "import.profile_manage",
        "update": "import.profile_manage",
        "partial_update": "import.profile_manage",
    }

    def perform_create(self, serializer):
        serializer.save(institution=self.institution, created_by=self.request.user)


class ImportRowSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImportRow
        fields = ["id", "row_number", "status", "raw", "normalized", "errors", "warnings", "correction_note"]


class ImportBatchSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImportBatch
        fields = [
            "id",
            "profile",
            "original_filename",
            "sha256",
            "size",
            "status",
            "total_rows",
            "valid_rows",
            "invalid_rows",
            "duplicate_rows",
            "warning_rows",
            "committed_rows",
            "summary",
            "error_message",
            "uploaded_by",
            "created_at",
            "validated_at",
            "committed_by",
            "committed_at",
        ]
        read_only_fields = fields


class ImportBatchViewSet(
    InstitutionContextMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    serializer_class = ImportBatchSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    permission_map = {
        "list": "import.view",
        "retrieve": "import.view",
        "rows": "import.view",
        "upload": "import.upload",
        "validate": "import.upload",
        "commit": "import.upload",
        "correct_row": "import.upload",
    }

    def get_queryset(self):
        return ImportBatch.objects.filter(institution=self.institution)

    @action(detail=False, methods=["post"])
    def upload(self, request):
        profile = ImportProfile.objects.filter(institution=self.institution, pk=request.data.get("profile")).first()
        upload = request.FILES.get("file")
        if profile is None or upload is None:
            return Response({"detail": "profile and file are required"}, status=400)
        batch = services.create_batch(self.institution, request.user, profile, upload.name, upload.read())
        return Response(self.get_serializer(batch).data, status=201)

    @action(detail=True, methods=["post"])
    def validate(self, request, pk=None):
        return Response(self.get_serializer(services.validate_batch(self.get_object(), request.user)).data)

    @action(detail=True, methods=["post"])
    def commit(self, request, pk=None):
        batch = services.commit_batch(
            self.get_object(), request.user, allow_partial=bool(request.data.get("allow_partial"))
        )
        return Response(self.get_serializer(batch).data)

    @action(detail=True, methods=["get"])
    def rows(self, request, pk=None):
        rows = self.get_object().rows.all()
        if request.query_params.get("status"):
            rows = rows.filter(status=request.query_params["status"])
        page = self.paginate_queryset(rows)
        return self.get_paginated_response(ImportRowSerializer(page, many=True).data)

    @action(detail=True, methods=["post"], url_path=r"rows/(?P<row_id>\d+)/correct")
    def correct_row(self, request, pk=None, row_id=None):
        row = get_object_or_404(self.get_object().rows, pk=row_id)
        row = services.correct_row(row, request.user, request.data.get("changes") or {}, request.data.get("note", ""))
        return Response(ImportRowSerializer(row).data)


class SourceTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = SourceTransaction
        exclude = ["institution", "import_row"]


class SourceTransactionViewSet(
    InstitutionContextMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    serializer_class = SourceTransactionSerializer
    default_permission = "import.view"

    def get_queryset(self):
        qs = SourceTransaction.objects.filter(institution=self.institution)
        for param in ("source_type", "account_reference", "batch", "currency"):
            value = self.request.query_params.get(param)
            if value:
                qs = qs.filter(**{param if param != "batch" else "batch_id": value})
        return qs
