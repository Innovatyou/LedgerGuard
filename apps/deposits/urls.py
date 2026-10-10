from django.urls import path

from . import views

app_name = "deposits"

urlpatterns = [
    path("", views.DepositControlView.as_view(), name="index"),
    path("<int:pk>/", views.DepositSnapshotView.as_view(), name="snapshot"),
]
