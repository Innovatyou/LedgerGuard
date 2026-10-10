from django.urls import path

from . import views

app_name = "audit"

urlpatterns = [
    path("", views.EngagementListView.as_view(), name="engagements"),
    path("new/", views.EngagementCreateView.as_view(), name="engagement-create"),
    path("engagements/<int:pk>/", views.EngagementDetailView.as_view(), name="engagement"),
    path("findings/<int:pk>/", views.FindingDetailView.as_view(), name="finding"),
]
