from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from .models import Users


# Register models that don't need a custom admin class
admin.site.register(
    [
        Users,
    ]
)


# Replace default User admin with a slightly customized version
try:
    admin.site.unregister(User)
except Exception:
    pass


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("get_full_name", "username", "email", "is_active", "is_staff")
    list_display_links = ("get_full_name",)
    search_fields = ("username", "first_name", "last_name", "email")
    list_filter = ("is_active", "is_staff", "is_superuser")

    def get_full_name(self, obj):
        return f"{obj.first_name} {obj.last_name}".strip() or obj.username

    get_full_name.short_description = "Full Name"
    get_full_name.admin_order_field = "first_name"
