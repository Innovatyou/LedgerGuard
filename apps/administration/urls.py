from django.urls import path

from . import views

app_name = "administration"

urlpatterns = [path("system/", views.SystemView.as_view(), name="system")]
