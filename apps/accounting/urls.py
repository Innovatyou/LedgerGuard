from django.urls import path

from . import views

app_name = "accounting"

urlpatterns = [
    path("accounts/", views.AccountListView.as_view(), name="accounts"),
    path("accounts/new/", views.AccountEditView.as_view(), name="account-create"),
    path("accounts/<int:pk>/", views.AccountEditView.as_view(), name="account-edit"),
    path("accounts/apply-template/", views.ApplyTemplateView.as_view(), name="apply-template"),
    path("periods/", views.PeriodListView.as_view(), name="periods"),
    path("journals/", views.JournalListView.as_view(), name="journals"),
    path("journals/new/", views.JournalEditView.as_view(), name="journal-create"),
    path("journals/<int:pk>/", views.JournalDetailView.as_view(), name="journal-detail"),
    path("journals/<int:pk>/edit/", views.JournalEditView.as_view(), name="journal-edit"),
    path("journals/<int:pk>/action/", views.JournalActionView.as_view(), name="journal-action"),
]
