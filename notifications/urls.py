from django.urls import path
from . import views

urlpatterns = [
    # Full Notification Center
    path('notifications/', views.notification_list_view, name='notification_list'),

    # Bell API (AJAX GET)
    path('api/notifications/bell/', views.notification_bell_api, name='api_notification_bell'),

    # Mark actions (AJAX POST)
    path('api/notifications/read-all/', views.mark_all_read_api, name='api_mark_notifications_read'),
    path('api/notifications/<int:notification_id>/read/', views.mark_notification_read_api, name='api_mark_notification_read'),
    path('api/notifications/<int:notification_id>/archive/', views.archive_notification_api, name='api_archive_notification'),
]
