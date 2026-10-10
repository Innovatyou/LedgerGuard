from django.urls import path

from . import views

app_name = "loans"

urlpatterns = [
    path("", views.LoanPortfolioView.as_view(), name="index"),
    path("<int:pk>/", views.LoanSnapshotView.as_view(), name="snapshot"),
]
