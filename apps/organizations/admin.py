from django.contrib import admin

from .models import Branch, Department, Institution


@admin.register(Institution)
class InstitutionAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "institution_type", "base_currency", "require_mfa", "is_active"]
    search_fields = ["name", "code"]


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "institution", "is_active"]
    list_filter = ["institution"]


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "kind", "institution", "is_active"]
    list_filter = ["institution"]
