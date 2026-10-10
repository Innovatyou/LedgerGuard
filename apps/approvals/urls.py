from django.urls import path

from . import views

app_name = "approvals"

urlpatterns = [
    path("changes/", views.ChangeRequestListView.as_view(), name="changes"),
]
