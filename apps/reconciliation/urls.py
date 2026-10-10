from django.urls import path

from . import views

app_name = "reconciliation"

urlpatterns = [
    path("", views.ReconListView.as_view(), name="list"),
    path("new/", views.ReconCreateView.as_view(), name="create"),
    path("<int:pk>/", views.ReconDetailView.as_view(), name="detail"),
    path("<int:pk>/action/", views.ReconActionView.as_view(), name="action"),
    path("rules/", views.RuleListView.as_view(), name="rules"),
    path("rules/new/", views.RuleEditView.as_view(), name="rule-create"),
    path("rules/<int:pk>/", views.RuleEditView.as_view(), name="rule-edit"),
]
