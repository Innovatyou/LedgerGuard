from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied
from django.db.models import Count, Sum
from django.shortcuts import render
from django.views.generic import View

from apps.accounting import reports
from apps.accounting.models import Account, Journal
from apps.audit.models import CorrectiveAction, Finding
from apps.core.activity import log_activity
from apps.core.models import ActivityLog
from apps.core.views_mixins import InstitutionRequiredMixin, scoped_get
from apps.imports.models import ImportBatch
from apps.reconciliation.models import MatchGroup, Reconciliation
from apps.reconciliation.services import build_statement
from apps.suspense.models import SuspenseItem
from apps.suspense.services import aging_report

from . import tables
from .exports import CONTENT_TYPES, export_response


def _date(value, default):
    try:
        return date.fromisoformat(value) if value else default
    except ValueError:
        return default


def year_start(institution, day: date) -> date:
    month = institution.financial_year_start_month
    year = day.year if day.month >= month else day.year - 1
    return date(year, month, 1)


class DashboardView(InstitutionRequiredMixin, View):
    def get(self, request):
        inst = self.institution
        today = date.today()
        month_start = today.replace(day=1)
        journals = Journal.objects.filter(institution=inst)
        recons = Reconciliation.objects.filter(institution=inst)
        open_suspense = SuspenseItem.objects.filter(institution=inst, status__in=SuspenseItem.OPEN_STATUSES)
        aging = aging_report(inst, today)
        latest_recon_for = {}
        for r in recons.filter(status__in=[Reconciliation.Status.APPROVED, Reconciliation.Status.CLOSED]).order_by(
            "period_end"
        ):
            if r.gl_account_id:
                latest_recon_for[r.gl_account_id] = r.period_end
        overdue_cutoff = today - timedelta(days=35)
        required = Account.objects.filter(
            institution=inst, reconciliation_required=True, is_active=True, account_type="POSTING"
        )
        unreconciled = [a for a in required if latest_recon_for.get(a.pk, date.min) < overdue_cutoff]
        ctx = {
            "today": today,
            "journals_pending": journals.filter(status=Journal.Status.SUBMITTED).count(),
            "journals_approved_unposted": journals.filter(status=Journal.Status.APPROVED).count(),
            "journals_posted_month": journals.filter(
                status__in=[Journal.Status.POSTED, Journal.Status.REVERSED], posted_at__date__gte=month_start
            ).count(),
            "recon_status": {
                **{s: 0 for s in Reconciliation.Status.values},
                **dict(recons.values_list("status").annotate(n=Count("id"))),
            },
            "recons_awaiting_approval": recons.filter(status=Reconciliation.Status.SUBMITTED)[:5],
            "pending_matches": MatchGroup.objects.filter(
                reconciliation__institution=inst, status=MatchGroup.Status.PROPOSED
            ).count(),
            "unreconciled_accounts": unreconciled[:10],
            "unreconciled_count": len(unreconciled),
            "suspense_count": open_suspense.count(),
            "suspense_total": open_suspense.aggregate(t=Sum("original_amount"))["t"] or Decimal("0"),
            "suspense_pending_approval": open_suspense.filter(status=SuspenseItem.Status.PENDING_APPROVAL).count(),
            "aging": list(zip(aging.labels, aging.bucket_counts, aging.bucket_totals, strict=True)),
            "aging_max": max(aging.bucket_totals) if aging.bucket_totals and max(aging.bucket_totals) else Decimal("1"),
            "imports_awaiting": ImportBatch.objects.filter(
                institution=inst, status=ImportBatch.Status.VALIDATED
            ).count(),
            "open_findings": Finding.objects.filter(institution=inst).exclude(status=Finding.Status.CLOSED).count(),
            "overdue_actions": CorrectiveAction.objects.filter(
                finding__institution=inst, due_date__lt=today, status=CorrectiveAction.Status.OPEN
            ).count(),
            "recent_activity": ActivityLog.objects.filter(institution=inst).select_related("user")[:10]
            if self.can("activity.view")
            else [],
        }
        if self.can("report.view"):
            ys = year_start(inst, today)
            ctx["income_statement"] = reports.income_statement(inst, ys, today, branches=self.branch_scope)
            ctx["tb"] = reports.trial_balance(inst, today, branches=self.branch_scope)
        return render(request, "reporting/dashboard.html", ctx)


class ReportView(InstitutionRequiredMixin, View):
    """Base for reports with on-screen rendering and Excel/CSV/PDF export."""

    permission_required = "report.view"
    template_name = ""
    report_name = ""

    def build(self, request):  # returns (context, table)
        raise NotImplementedError

    def get(self, request, *args, **kwargs):
        ctx, table = self.build(request, *args, **kwargs)
        fmt = request.GET.get("export")
        if fmt:
            if fmt not in CONTENT_TYPES:
                raise PermissionDenied("Unsupported export format")
            if not self.can("report.export"):
                raise PermissionDenied("Permission required: report.export")
            log_activity(
                "report.exported",
                user=request.user,
                institution=self.institution,
                details={"report": self.report_name, "format": fmt, "params": request.GET.dict()},
            )
            return export_response(table, fmt, f"{self.report_name}_{date.today():%Y%m%d}")
        ctx["table"] = table
        ctx["can_export"] = self.can("report.export")
        return render(request, self.template_name, ctx)


class TrialBalanceView(ReportView):
    template_name = "reporting/trial_balance.html"
    report_name = "trial_balance"

    def build(self, request):
        as_of = _date(request.GET.get("as_of"), date.today())
        tb = reports.trial_balance(self.institution, as_of, branches=self.branch_scope)
        return {"tb": tb, "as_of": as_of}, tables.trial_balance_table(tb, self.institution, request.user)


class GeneralLedgerView(ReportView):
    template_name = "reporting/general_ledger.html"
    report_name = "general_ledger"

    def build(self, request):
        accounts = Account.objects.filter(institution=self.institution, account_type="POSTING")
        account = None
        if request.GET.get("account"):
            account = scoped_get(request, Account, pk=request.GET["account"])
        today = date.today()
        date_from = _date(request.GET.get("from"), year_start(self.institution, today))
        date_to = _date(request.GET.get("to"), today)
        ctx = {"accounts": accounts, "account": account, "date_from": date_from, "date_to": date_to}
        if account is None:
            from .exports import Column, Table

            return ctx, Table("General Ledger", "Select an account", [Column("—")])
        gl = reports.general_ledger(self.institution, account, date_from, date_to, branches=self.branch_scope)
        ctx["gl"] = gl
        return ctx, tables.general_ledger_table(gl, self.institution, request.user)


class IncomeStatementView(ReportView):
    template_name = "reporting/income_statement.html"
    report_name = "income_statement"

    def build(self, request):
        today = date.today()
        date_from = _date(request.GET.get("from"), year_start(self.institution, today))
        date_to = _date(request.GET.get("to"), today)
        stmt = reports.income_statement(self.institution, date_from, date_to, branches=self.branch_scope)
        return (
            {"stmt": stmt, "date_from": date_from, "date_to": date_to},
            tables.income_statement_table(stmt, self.institution, request.user),
        )


class FinancialPositionView(ReportView):
    template_name = "reporting/financial_position.html"
    report_name = "financial_position"

    def build(self, request):
        as_of = _date(request.GET.get("as_of"), date.today())
        fp = reports.statement_of_financial_position(self.institution, as_of, branches=self.branch_scope)
        return {"fp": fp, "as_of": as_of}, tables.financial_position_table(fp, self.institution, request.user)


class ReconciliationStatementView(ReportView):
    permission_required = "recon.view"
    template_name = "reporting/table_report.html"
    report_name = "reconciliation_statement"

    def build(self, request, pk):
        recon = scoped_get(request, Reconciliation, pk=pk)
        stmt = build_statement(recon)
        return {"title": f"Reconciliation statement {recon.reference}"}, tables.reconciliation_table(
            stmt, self.institution, request.user
        )


class SuspenseAgingReportView(ReportView):
    permission_required = "suspense.view"
    template_name = "reporting/table_report.html"
    report_name = "suspense_aging"

    def build(self, request):
        as_of = _date(request.GET.get("as_of"), date.today())
        report = aging_report(self.institution, as_of)
        return {"title": "Suspense aging"}, tables.aging_table(report, self.institution, request.user)
