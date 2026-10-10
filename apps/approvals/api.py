from rest_framework import status
from rest_framework.response import Response

from .changes import request_change, requires_approval


class ChangeControlledMixin:
    """
    For configuration endpoints: when the institution requires configuration
    approval, create/update requests become change requests (HTTP 202) instead
    of being applied directly.
    """

    change_target: str = ""

    def _as_change_request(self, serializer, instance=None):
        if not requires_approval(self.institution):
            return None
        cr = request_change(
            self.institution,
            self.request.user,
            self.change_target,
            dict(serializer.validated_data),
            target_obj=instance,
        )
        return Response(
            {"detail": "Change submitted for independent approval.", "change_request": cr.reference, "id": cr.pk},
            status=status.HTTP_202_ACCEPTED,
        )

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        pending = self._as_change_request(serializer)
        if pending is not None:
            return pending
        self.perform_create(serializer)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        pending = self._as_change_request(serializer, instance)
        if pending is not None:
            return pending
        self.perform_update(serializer)
        return Response(serializer.data)
