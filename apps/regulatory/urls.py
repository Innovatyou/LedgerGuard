from django.urls import path

from . import views

app_name = "regulatory"

urlpatterns = [
    path("", views.TemplateListView.as_view(), name="index"),
    path("prepare/", views.PrepareView.as_view(), name="prepare"),
    path("templates/<int:pk>/", views.TemplateDetailView.as_view(), name="template"),
    path("returns/<int:pk>/", views.ReturnView.as_view(), name="return"),
]
