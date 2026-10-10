from rest_framework.response import Response

from apps.core.api import InstitutionAPIView

from . import services
from .views import _date


class SettlementSummaryView(InstitutionAPIView):
    default_permission = "settlement.view"

    def get(self, request):
        params = request.query_params
        batches = services.settlement_summary(
            self.institution,
            date_from=_date(params.get("from")),
            date_to=_date(params.get("to")),
            channel=params.get("channel") or None,
            account_reference=params.get("account") or None,
        )
        return Response(
            [
                {
                    "batch_ref": b.batch_ref,
                    "channel": b.channel,
                    "settlement_date": b.settlement_date,
                    "records": b.records,
                    "reversals": b.reversals,
                    "gross": str(b.gross),
                    "fees": str(b.fees),
                    "taxes": str(b.taxes),
                    "net_reported": str(b.net_reported),
                    "net_computed": str(b.net_computed),
                    "matched_records": b.matched_records,
                    "fully_matched": b.fully_matched,
                    "exceptions": b.exceptions,
                }
                for b in batches
            ]
        )
