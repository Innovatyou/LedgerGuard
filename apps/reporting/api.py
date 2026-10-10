from datetime import date

from django.shortcuts import get_object_or_404
from rest_framework.response import Response

from apps.accounting import reports
from apps.accounting.models import Account
from apps.core.api import InstitutionAPIView

from .views import _date, year_start


def _section(section):
    return {
        "title": section.title,
        "total": str(section.total),
        "groups": [
            {
                "group": g,
                "total": str(t),
                "accounts": [{"code": a.code, "name": a.name, "amount": str(v)} for a, v in items],
            }
            for g, t, items in section.group_totals()
        ],
    }


class TrialBalanceAPI(InstitutionAPIView):
    default_permission = "report.view"

    def get(self, request):
        tb = reports.trial_balance(self.institution, _date(request.query_params.get("as_of"), date.today()))
        return Response(
            {
                "as_of": tb.as_of,
                "balanced": tb.is_balanced,
                "total_debit": str(tb.total_debit),
                "total_credit": str(tb.total_credit),
                "rows": [
                    {
                        "code": r.account.code,
                        "name": r.account.name,
                        "debit_total": str(r.debit_total),
                        "credit_total": str(r.credit_total),
                        "balance_debit": str(r.balance_debit),
                        "balance_credit": str(r.balance_credit),
                    }
                    for r in tb.rows
                ],
            }
        )


class IncomeStatementAPI(InstitutionAPIView):
    default_permission = "report.view"

    def get(self, request):
        today = date.today()
        stmt = reports.income_statement(
            self.institution,
            _date(request.query_params.get("from"), year_start(self.institution, today)),
            _date(request.query_params.get("to"), today),
        )
        return Response(
            {
                "from": stmt.date_from,
                "to": stmt.date_to,
                "income": _section(stmt.income),
                "expenses": _section(stmt.expenses),
                "net_profit": str(stmt.net_profit),
            }
        )


class FinancialPositionAPI(InstitutionAPIView):
    default_permission = "report.view"

    def get(self, request):
        fp = reports.statement_of_financial_position(
            self.institution, _date(request.query_params.get("as_of"), date.today())
        )
        return Response(
            {
                "as_of": fp.as_of,
                "assets": _section(fp.assets),
                "liabilities": _section(fp.liabilities),
                "equity": _section(fp.equity),
                "unclosed_earnings": str(fp.unclosed_earnings),
                "total_liabilities_and_equity": str(fp.total_liabilities_and_equity),
                "balanced": fp.is_balanced,
            }
        )


class GeneralLedgerAPI(InstitutionAPIView):
    default_permission = "report.view"

    def get(self, request, account_id):
        account = get_object_or_404(Account, pk=account_id, institution=self.institution)
        gl = reports.general_ledger(
            self.institution,
            account,
            _date(request.query_params.get("from"), None),
            _date(request.query_params.get("to"), None),
        )
        return Response(
            {
                "account": account.code,
                "opening_balance": str(gl.opening_balance),
                "closing_balance": str(gl.closing_balance),
                "entries": [
                    {
                        "date": ln.entry.accounting_date,
                        "journal": ln.entry.journal.number,
                        "debit": str(ln.entry.debit),
                        "credit": str(ln.entry.credit),
                        "balance": str(ln.running_balance),
                    }
                    for ln in gl.lines
                ],
            }
        )
