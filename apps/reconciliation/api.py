from decimal import Decimal

from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.approvals.api import ChangeControlledMixin
from apps.core.api import InstitutionScopedViewSet

from . import services
from .models import MatchGroup, MatchGroupItem, MatchingRule, Reconciliation


class MatchingRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = MatchingRule
        exclude = ["institution", "updated_by", "created_at", "updated_at"]


class MatchingRuleViewSet(ChangeControlledMixin, InstitutionScopedViewSet):
    change_target = "matching_rule"
    model = MatchingRule
    serializer_class = MatchingRuleSerializer
    http_method_names = ["get", "post", "put", "patch", "head", "options"]
    permission_map = {
        "list": "recon.view",
        "retrieve": "recon.view",
        "create": "recon.rule_manage",
        "update": "recon.rule_manage",
        "partial_update": "recon.rule_manage",
    }

    def perform_create(self, serializer):
        serializer.save(institution=self.institution, updated_by=self.request.user)

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)


class ReconciliationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Reconciliation
        exclude = ["institution"]
        read_only_fields = [
            "reference",
            "status",
            "prepared_by",
            "submitted_at",
            "approved_by",
            "approved_at",
            "last_run_at",
            "run_summary",
            "previous",
        ]

    def validate(self, attrs):
        institution = self.context["institution"]
        gl = attrs.get("gl_account")
        if gl is not None and gl.institution_id != institution.pk:
            raise serializers.ValidationError({"gl_account": "Belongs to a different institution."})
        for rule in attrs.get("rules", []):
            if rule.institution_id != institution.pk:
                raise serializers.ValidationError({"rules": "Rule belongs to a different institution."})
        return attrs


class MatchItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = MatchGroupItem
        fields = ["transaction", "side", "flow_amount", "active"]


class MatchGroupSerializer(serializers.ModelSerializer):
    items = MatchItemSerializer(many=True)

    class Meta:
        model = MatchGroup
        fields = [
            "id",
            "rule",
            "rule_snapshot",
            "kind",
            "category",
            "status",
            "left_total",
            "right_total",
            "difference",
            "reason",
            "note",
            "created_at",
            "decided_by",
            "decided_at",
            "items",
        ]


def _money(v):
    return str(v)


def statement_payload(stmt):
    def items(rows):
        return [
            {
                "transaction": t.pk,
                "date": t.transaction_date,
                "reference": t.transaction_id or t.external_reference,
                "narration": t.narration,
                "flow": _money(f),
            }
            for t, f in rows
        ]

    return {
        "left": {
            "label": stmt.left.label,
            "opening": _money(stmt.left.opening),
            "inflows": _money(stmt.left.inflows),
            "outflows": _money(stmt.left.outflows),
            "computed_closing": _money(stmt.left.computed_closing),
            "reported_closing": _money(stmt.left.reported_closing) if stmt.left.reported_closing is not None else None,
        },
        "right": {
            "label": stmt.right.label,
            "opening": _money(stmt.right.opening),
            "inflows": _money(stmt.right.inflows),
            "outflows": _money(stmt.right.outflows),
            "computed_closing": _money(stmt.right.computed_closing),
            "reported_closing": _money(stmt.right.reported_closing)
            if stmt.right.reported_closing is not None
            else None,
        },
        "matched_groups": stmt.matched_groups,
        "outstanding_deposits": items(stmt.outstanding_deposits),
        "outstanding_payments": items(stmt.outstanding_payments),
        "unrecorded_credits": items(stmt.unrecorded_credits),
        "unrecorded_charges": items(stmt.unrecorded_charges),
        "excluded_left": items(stmt.excluded_left),
        "excluded_right": items(stmt.excluded_right),
        "suspected_duplicates": items(stmt.duplicates),
        "adjustments": [
            {"side": a.side, "description": a.description, "amount": _money(a.amount), "reference": a.reference}
            for a in stmt.adjustments
        ],
        "adjusted_left_balance": _money(stmt.adjusted_left_balance),
        "adjusted_right_balance": _money(stmt.adjusted_right_balance),
        "final_difference": _money(stmt.final_difference),
        "pending_review": stmt.pending_review,
        "validation_errors": stmt.validation_errors(stmt.recon.institution.reconciliation_tolerance),
    }


class ReconciliationViewSet(InstitutionScopedViewSet):
    model = Reconciliation
    serializer_class = ReconciliationSerializer
    http_method_names = ["get", "post", "head", "options"]
    permission_map = {
        "list": "recon.view",
        "retrieve": "recon.view",
        "statement": "recon.view",
        "matches": "recon.view",
        "create": "recon.run",
        "run": "recon.run",
        "submit": "recon.run",
        "accept_match": "recon.investigate",
        "reject_match": "recon.investigate",
        "manual_match": "recon.investigate",
        "exclude": "recon.investigate",
        "adjust": "recon.investigate",
        "approve": "recon.approve",
        "reopen": "recon.reopen",
        "close": "recon.approve",
    }

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = dict(ser.validated_data)
        recon = services.create_reconciliation(self.institution, request.user, **data)
        return Response(self.get_serializer(recon).data, status=201)

    @action(detail=True, methods=["post"])
    def run(self, request, pk=None):
        return Response(services.run_reconciliation(self.get_object(), request.user))

    @action(detail=True, methods=["get"])
    def statement(self, request, pk=None):
        return Response(statement_payload(services.build_statement(self.get_object())))

    @action(detail=True, methods=["get"])
    def matches(self, request, pk=None):
        groups = self.get_object().match_groups.prefetch_related("items")
        if request.query_params.get("status"):
            groups = groups.filter(status=request.query_params["status"])
        page = self.paginate_queryset(groups)
        return self.get_paginated_response(MatchGroupSerializer(page, many=True).data)

    def _group(self, recon, request):
        return get_object_or_404(MatchGroup, pk=request.data.get("group"), reconciliation=recon)

    @action(detail=True, methods=["post"])
    def accept_match(self, request, pk=None):
        recon = self.get_object()
        group = services.accept_match(self._group(recon, request), request.user, request.data.get("note", ""))
        return Response(MatchGroupSerializer(group).data)

    @action(detail=True, methods=["post"])
    def reject_match(self, request, pk=None):
        recon = self.get_object()
        group = services.reject_match(self._group(recon, request), request.user, request.data.get("note", ""))
        return Response(MatchGroupSerializer(group).data)

    @action(detail=True, methods=["post"])
    def manual_match(self, request, pk=None):
        group = services.manual_match(
            self.get_object(),
            request.user,
            request.data.get("left", []),
            request.data.get("right", []),
            request.data.get("note", ""),
        )
        return Response(MatchGroupSerializer(group).data, status=201)

    @action(detail=True, methods=["post"])
    def exclude(self, request, pk=None):
        services.exclude_transaction(
            self.get_object(), request.user, int(request.data.get("transaction")), request.data.get("reason", "")
        )
        return Response({"status": "excluded"})

    @action(detail=True, methods=["post"])
    def adjust(self, request, pk=None):
        adj = services.add_adjustment(
            self.get_object(),
            request.user,
            side=request.data.get("side"),
            description=request.data.get("description", ""),
            amount=Decimal(str(request.data.get("amount"))),
            reference=request.data.get("reference", ""),
        )
        return Response({"id": adj.pk}, status=201)

    def _transition(self, func, *args):
        recon = func(self.get_object(), self.request.user, *args)
        return Response(self.get_serializer(recon).data)

    @action(detail=True, methods=["post"])
    def submit(self, request, pk=None):
        return self._transition(services.submit_reconciliation, request.data.get("comment", ""))

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        return self._transition(services.approve_reconciliation, request.data.get("comment", ""))

    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        return self._transition(services.reopen_reconciliation, request.data.get("reason", ""))

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        return self._transition(services.close_reconciliation, request.data.get("comment", ""))
