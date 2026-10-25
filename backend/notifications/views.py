"""
notifications/views.py
Phase 7 — Full Notification Center: list, bell API, mark read/archive
"""

import json
from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.utils import timezone
from django.db.models import Q
from django.contrib.auth.decorators import login_required

from inventory.views import admin_or_pharmacist_required
from .models import Notification


def _get_user_notifications(user):
    """Return notifications for the current user (personal + broadcasts)."""
    return Notification.objects.filter(
        Q(recipient=user) | Q(recipient__isnull=True),
        is_archived=False,
    ).order_by('-created_at')


@admin_or_pharmacist_required
def notification_list_view(request):
    """Full Notification Center with filters for type, priority, status."""
    filter_status = request.GET.get('status', 'unread')
    filter_priority = request.GET.get('priority', '')
    filter_type = request.GET.get('type', '')

    qs = _get_user_notifications(request.user)

    if filter_status == 'unread':
        qs = qs.filter(is_read=False)
    elif filter_status == 'read':
        qs = qs.filter(is_read=True)
    elif filter_status == 'archived':
        qs = Notification.objects.filter(
            Q(recipient=request.user) | Q(recipient__isnull=True),
            is_archived=True,
        ).order_by('-created_at')

    if filter_priority:
        qs = qs.filter(priority=filter_priority)
    if filter_type:
        qs = qs.filter(notification_type=filter_type)

    notifications = qs[:100]
    unread_count = _get_user_notifications(request.user).filter(is_read=False).count()

    # Priority color mapping for template
    priority_colors = {
        'CRITICAL': 'danger',
        'HIGH': 'warning',
        'MEDIUM': 'info',
        'LOW': 'secondary',
        'INFO': 'teal',
        'URGENT': 'danger',
        'NORMAL': 'secondary',
    }

    context = {
        'notifications': notifications,
        'unread_count': unread_count,
        'filter_status': filter_status,
        'filter_priority': filter_priority,
        'filter_type': filter_type,
        'priority_colors': priority_colors,
        'notification_types': Notification.NotificationType.choices,
        'priority_choices': Notification.Priority.choices,
        'user_role': request.user.role,
    }
    return render(request, 'notifications/notification_list.html', context)


@admin_or_pharmacist_required
def notification_bell_api(request):
    """AJAX GET: Return unread count + top 5 unread notifications for navbar bell."""
    qs = _get_user_notifications(request.user).filter(is_read=False)

    unread_count = qs.count()
    top_notifications = []
    for n in qs[:5]:
        top_notifications.append({
            'id': n.id,
            'title': n.title,
            'message': n.message[:100],
            'priority': n.priority,
            'type': n.notification_type,
            'created_at': n.created_at.strftime('%d %b %H:%M'),
            'action_url': n.action_url,
            'action_label': n.action_label,
        })

    return JsonResponse({
        'success': True,
        'unread_count': unread_count,
        'notifications': top_notifications,
    })


@admin_or_pharmacist_required
def mark_notification_read_api(request, notification_id):
    """AJAX POST: Mark a single notification as read."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed.'}, status=405)

    n = get_object_or_404(
        Notification,
        id=notification_id,
    )
    # Security: only allow marking if it belongs to user or is a broadcast
    if n.recipient and n.recipient != request.user:
        return JsonResponse({'success': False, 'error': 'Permission denied.'}, status=403)

    n.mark_read()
    return JsonResponse({'success': True})


@admin_or_pharmacist_required
def mark_all_read_api(request):
    """AJAX POST: Mark all unread notifications as read for the current user."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed.'}, status=405)

    updated = _get_user_notifications(request.user).filter(is_read=False).update(
        is_read=True,
        status=Notification.Status.READ,
        read_at=timezone.now(),
    )
    return JsonResponse({'success': True, 'marked_count': updated})


@admin_or_pharmacist_required
def archive_notification_api(request, notification_id):
    """AJAX POST: Archive a single notification."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed.'}, status=405)

    n = get_object_or_404(Notification, id=notification_id)
    if n.recipient and n.recipient != request.user:
        return JsonResponse({'success': False, 'error': 'Permission denied.'}, status=403)

    n.archive()
    return JsonResponse({'success': True})


# Legacy compatibility
@admin_or_pharmacist_required
def mark_notifications_read_api(request):
    """Legacy: Mark all active stock alerts as read."""
    return mark_all_read_api(request)
