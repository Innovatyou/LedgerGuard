from django.contrib.auth.decorators import login_required
from django.urls import path

from . import views

app_name = "notifications"

urlpatterns = [
    path("", views.NotificationListView.as_view(), name="list"),
    path("<int:pk>/read/", login_required(views.mark_read), name="read"),
]
