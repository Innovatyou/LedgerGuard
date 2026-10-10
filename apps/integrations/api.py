from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from apps.core.context import current_ip
from apps.core.exceptions import BusinessRuleError

from . import services


class IntegrationThrottle(AnonRateThrottle):
    rate = "120/hour"


class InboundFileView(APIView):
    """
    POST a source file with ``Authorization: Bearer lgk_<prefix>.<secret>`` and a
    multipart ``file`` field. The file is validated (and committed if the
    endpoint allows it) exactly like a manual import.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]
    parser_classes = [MultiPartParser]
    throttle_classes = [IntegrationThrottle]

    def post(self, request):
        ip = current_ip() or request.META.get("REMOTE_ADDR")
        try:
            endpoint = services.authenticate(request.META.get("HTTP_AUTHORIZATION", ""), ip)
        except BusinessRuleError as exc:
            code = status.HTTP_401_UNAUTHORIZED if exc.code == "unauthorised" else status.HTTP_403_FORBIDDEN
            return Response({"detail": exc.message}, status=code)
        upload = request.FILES.get("file")
        if upload is None:
            return Response({"detail": "A multipart 'file' field is required."}, status=400)
        try:
            summary = services.ingest(endpoint, upload.name, upload.read(), ip=ip)
        except BusinessRuleError as exc:
            return Response({"detail": exc.message, "code": exc.code}, status=422)
        return Response(summary, status=201)
