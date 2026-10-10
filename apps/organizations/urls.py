from django.urls import path

from . import views

app_name = "organizations"

urlpatterns = [
    path("settings/", views.InstitutionSettingsView.as_view(), name="settings"),
    path("branches/", views.BranchListView.as_view(), name="branches"),
    path("branches/new/", views.BranchEditView.as_view(), name="branch-create"),
    path("branches/<int:pk>/", views.BranchEditView.as_view(), name="branch-edit"),
    path("departments/new/", views.DepartmentEditView.as_view(), name="department-create"),
    path("departments/<int:pk>/", views.DepartmentEditView.as_view(), name="department-edit"),
]
