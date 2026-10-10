from django.urls import path

from . import views

app_name = "integrations"

urlpatterns = [path("", views.IntegrationsView.as_view(), name="index")]
