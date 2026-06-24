from django.contrib import admin

from users.models import User


# Register your models here.
@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    exclude = ("password",)
    sortable_by = ("date_joined", "username")
    list_display = ("username", "date_joined", "is_discord_linked")

    fields = ("username", "uuid", "date_joined", "is_discord_linked", "discord_profile")
    readonly_fields = ("uuid", "is_discord_linked", "discord_profile")
    pass
