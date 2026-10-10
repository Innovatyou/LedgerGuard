from django.urls import path

from . import views

app_name = "imports"

urlpatterns = [
    path("", views.BatchListView.as_view(), name="batches"),
    path("upload/", views.UploadView.as_view(), name="upload"),
    path("batches/<int:pk>/", views.BatchDetailView.as_view(), name="batch-detail"),
    path("batches/<int:pk>/action/", views.BatchActionView.as_view(), name="batch-action"),
    path("profiles/", views.ProfileListView.as_view(), name="profiles"),
    path("profiles/new/", views.ProfileEditView.as_view(), name="profile-create"),
    path("profiles/<int:pk>/", views.ProfileEditView.as_view(), name="profile-edit"),
    path("transactions/", views.TransactionListView.as_view(), name="transactions"),
]
