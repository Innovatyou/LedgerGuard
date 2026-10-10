from django.urls import path

from . import views

app_name = "suspense"

urlpatterns = [
    path("", views.SuspenseListView.as_view(), name="list"),
    path("new/", views.SuspenseCreateView.as_view(), name="create"),
    path("aging/", views.AgingView.as_view(), name="aging"),
    path("<int:pk>/", views.SuspenseDetailView.as_view(), name="detail"),
]
