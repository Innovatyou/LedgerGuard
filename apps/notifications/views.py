from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django.views.generic import ListView

from apps.core.views_mixins import InstitutionRequiredMixin

from .models import Notification


class NotificationListView(InstitutionRequiredMixin, ListView):
    template_name = "notifications/list.html"
    paginate_by = 50

    def get_queryset(self):
        return Notification.objects.filter(user=self.request.user, institution=self.institution)


@require_POST
def mark_read(request, pk):
    notification = get_object_or_404(Notification, pk=pk, user=request.user)
    if notification.read_at is None:
        notification.read_at = timezone.now()
        notification.save(update_fields=["read_at"])
    if notification.link and url_has_allowed_host_and_scheme(notification.link, allowed_hosts={request.get_host()}):
        return redirect(notification.link)
    return redirect("notifications:list")
