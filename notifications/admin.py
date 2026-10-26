from django.contrib import admin
from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = [
        'title', 'recipient', 'notification_type', 'priority',
        'is_read', 'read_at', 'created_at'
    ]
    list_filter = ['notification_type', 'priority', 'is_read', 'created_at']
    search_fields = ['title', 'message', 'recipient__username']
    readonly_fields = ['created_at', 'read_at']
    ordering = ['-created_at']
    actions = ['mark_all_read']

    def mark_all_read(self, request, queryset):
        for notification in queryset:
            notification.mark_read()
        self.message_user(request, f"{queryset.count()} notification(s) marked as read.")
    mark_all_read.short_description = "Mark selected notifications as read"
