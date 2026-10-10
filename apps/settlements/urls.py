from django.urls import path

from . import views

app_name = "settlements"

urlpatterns = [
    path("", views.SettlementDashboardView.as_view(), name="dashboard"),
]
