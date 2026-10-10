from datetime import date

from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.api import InstitutionScopedViewSet

from . import services
from .models import Account, FinancialPeriod, Journal, JournalLine


class AccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = Account
        fields = [
            "id",
            "code",
            "name",
            "parent",
            "category",
            "subcategory",
            "account_type",
            "normal_balance",
            "currency",
            "branch",
            "department",
            "is_active",
            "effective_from",
            "effective_to",
            "reconciliation_required",
            "is_control_account",
            "is_suspense_account",
            "cash_flow_class",
            "fs_line",
            "regulatory_code",
        ]
        extra_kwargs = {"normal_balance": {"required": False, "allow_blank": True}}

    def validate(self, attrs):
        institution = self.context["institution"]
        for key in ("parent", "branch", "department"):
            related = attrs.get(key)
            if related is not None and related.institution_id != institution.pk:
                raise serializers.ValidationError({key: "Belongs to a different institution."})
        return attrs

    def save(self, **kwargs):
        from django.core.exceptions import ValidationError

        try:
            return super().save(**kwargs)
        except ValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc


class AccountViewSet(InstitutionScopedViewSet):
    model = Account
    serializer_class = AccountSerializer
    http_method_names = ["get", "post", "put", "patch", "head", "options"]
    permission_map = {
        "list": "coa.view",
        "retrieve": "coa.view",
        "create": "coa.manage",
        "update": "coa.manage",
        "partial_update": "coa.manage",
    }

    def perform_create(self, serializer):
        serializer.save(institution=self.institution, created_by=self.request.user)


class PeriodSerializer(serializers.ModelSerializer):
    class Meta:
        model = FinancialPeriod
        fields = ["id", "name", "start_date", "end_date", "status", "is_year_end", "closed_at", "reopened_at"]
        read_only_fields = ["status", "closed_at", "reopened_at"]


class FinancialPeriodViewSet(InstitutionScopedViewSet):
    model = FinancialPeriod
    serializer_class = PeriodSerializer
    http_method_names = ["get", "post", "head", "options"]
    permission_map = {
        "list": "journal.view",
        "retrieve": "journal.view",
        "create": "period.manage",
        "close": "period.close",
        "reopen": "period.reopen",
    }

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        period = services.create_period(self.institution, request.user, **ser.validated_data)
        return Response(self.get_serializer(period).data, status=201)

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        return Response(self.get_serializer(services.close_period(self.get_object(), request.user)).data)

    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        period = services.reopen_period(self.get_object(), request.user, request.data.get("reason", ""))
        return Response(self.get_serializer(period).data)


class JournalLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = JournalLine
        fields = [
            "line_no",
            "account",
            "description",
            "debit",
            "credit",
            "base_debit",
            "base_credit",
            "branch",
            "department",
        ]
        read_only_fields = ["line_no", "base_debit", "base_credit"]


class JournalSerializer(serializers.ModelSerializer):
    lines = JournalLineSerializer(many=True)

    class Meta:
        model = Journal
        fields = [
            "id",
            "number",
            "journal_type",
            "transaction_date",
            "accounting_date",
            "period",
            "description",
            "source_reference",
            "supporting_reference",
            "currency",
            "exchange_rate",
            "branch",
            "status",
            "prepared_by",
            "approved_by",
            "approved_at",
            "posted_by",
            "posted_at",
            "reversal_of",
            "rejection_reason",
            "idempotency_key",
            "lines",
        ]
        read_only_fields = [
            "number",
            "period",
            "status",
            "prepared_by",
            "approved_by",
            "approved_at",
            "posted_by",
            "posted_at",
            "reversal_of",
            "rejection_reason",
        ]

    def validate_lines(self, lines):
        institution = self.context["institution"]
        for line in lines:
            for key in ("account", "branch", "department"):
                related = line.get(key)
                if related is not None and related.institution_id != institution.pk:
                    raise serializers.ValidationError(f"Line {key} belongs to a different institution.")
        return lines


def _line_inputs(lines):
    return [
        services.LineInput(
            account=ln["account"],
            debit=ln.get("debit") or 0,
            credit=ln.get("credit") or 0,
            description=ln.get("description", ""),
            branch=ln.get("branch"),
            department=ln.get("department"),
        )
        for ln in lines
    ]


class JournalViewSet(InstitutionScopedViewSet):
    model = Journal
    serializer_class = JournalSerializer
    http_method_names = ["get", "post", "put", "head", "options"]
    permission_map = {
        "list": "journal.view",
        "retrieve": "journal.view",
        "create": "journal.create",
        "update": "journal.create",
        "submit": "journal.create",
        "approve": "journal.approve",
        "reject": "journal.approve",
        "post_to_ledger": "journal.post",
        "reverse": "journal.reverse",
    }

    def get_queryset(self):
        qs = super().get_queryset().prefetch_related("lines")
        if self.branch_scope is not None:
            qs = qs.filter(branch_id__in=self.branch_scope)
        status = self.request.query_params.get("status")
        return qs.filter(status=status) if status else qs

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = dict(ser.validated_data)
        lines = _line_inputs(data.pop("lines"))
        journal = services.create_journal(self.institution, request.user, lines=lines, **data)
        return Response(self.get_serializer(journal).data, status=201)

    def update(self, request, *args, **kwargs):
        journal = self.get_object()
        ser = self.get_serializer(journal, data=request.data)
        ser.is_valid(raise_exception=True)
        data = dict(ser.validated_data)
        data.pop("idempotency_key", None)
        lines = _line_inputs(data.pop("lines"))
        journal = services.update_journal(journal, request.user, lines=lines, **data)
        return Response(self.get_serializer(journal).data)

    def _act(self, func, *args):
        journal = func(self.get_object(), self.request.user, *args)
        return Response(self.get_serializer(journal).data)

    @action(detail=True, methods=["post"])
    def submit(self, request, pk=None):
        return self._act(services.submit_journal, request.data.get("comment", ""))

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        return self._act(services.approve_journal, request.data.get("comment", ""))

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        return self._act(services.reject_journal, request.data.get("reason", ""))

    @action(detail=True, methods=["post"], url_path="post")
    def post_to_ledger(self, request, pk=None):
        return self._act(services.post_journal, request.data.get("comment", ""))

    @action(detail=True, methods=["post"])
    def reverse(self, request, pk=None):
        reversal_date = request.data.get("reversal_date")
        reversal = services.request_reversal(
            self.get_object(),
            request.user,
            request.data.get("reason", ""),
            date.fromisoformat(reversal_date) if reversal_date else None,
        )
        return Response(self.get_serializer(reversal).data, status=201)
