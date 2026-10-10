from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import Membership, Role, User


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    inlines = [MembershipInline]
    list_display = ["username", "email", "first_name", "last_name", "mfa_enabled", "is_active", "is_superuser"]
    fieldsets = BaseUserAdmin.fieldsets + (("LedgerGuard", {"fields": ("phone", "job_title", "mfa_enabled")}),)


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "institution", "is_system"]
    list_filter = ["is_system", "institution"]
