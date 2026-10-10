from django.urls import path

from . import views

app_name = "fixed_assets"

urlpatterns = [
    path("", views.RegisterView.as_view(), name="register"),
    path("new/", views.AssetCreateView.as_view(), name="create"),
    path("categories/new/", views.CategoryCreateView.as_view(), name="category-create"),
    path("depreciation/", views.DepreciationView.as_view(), name="depreciation"),
    path("<int:pk>/", views.AssetDetailView.as_view(), name="detail"),
]
