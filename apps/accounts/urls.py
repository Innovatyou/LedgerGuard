from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("mfa/verify/", views.mfa_verify, name="mfa-verify"),
    path("mfa/setup/", views.mfa_setup, name="mfa-setup"),
    path("switch-institution/", views.switch_institution, name="switch-institution"),
    path("profile/", views.profile, name="profile"),
    path("password/change/", views.password_change, name="password-change"),
    path("users/", views.UserListView.as_view(), name="users"),
    path("users/new/", views.UserCreateView.as_view(), name="user-create"),
    path("memberships/<int:pk>/", views.MembershipEditView.as_view(), name="membership-edit"),
    path("roles/", views.RoleListView.as_view(), name="roles"),
    path("roles/new/", views.RoleCreateView.as_view(), name="role-create"),
]
