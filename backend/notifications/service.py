"""
notifications/service.py
Phase 7 — Centralized Notification Engine

Single source for creating all MediVault notifications.
Deduplicates via fingerprint. Optionally sends email alerts for critical events.
"""

import logging
from datetime import timedelta, date

from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings
from django.template.loader import render_to_string
from django.db.models import F, Q, Sum

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# CORE NOTIFICATION FACTORY
# ─────────────────────────────────────────────────────────────────────────────

def notify(notification_type, title, message, priority='MEDIUM',
           recipient=None, role=None, action_url='', action_label='',
           related_object_type='', related_object_id=None,
           fingerprint='', send_email_alert=False, cooldown_seconds=None, **kwargs):
    """
    Central notification dispatcher.
    - recipient=None + role=None → broadcast to all
    - recipient=<User>           → single user notification
    - role='ADMIN'/'PHARMACIST'  → notify all users of that role
    Returns list of created Notification instances.
    """
    from .models import Notification

    created = []

    try:
        if recipient is not None:
            n = Notification.create_for_user(
                user=recipient,
                notification_type=notification_type,
                title=title, message=message, priority=priority,
                action_url=action_url,
                action_label=action_label,
                related_object_type=related_object_type,
                related_object_id=related_object_id,
                fingerprint=fingerprint,
                cooldown_seconds=cooldown_seconds,
            )
            if n:
                created.append(n)

        elif role is not None:
            created = Notification.create_for_role(
                role=role,
                notification_type=notification_type,
                title=title, message=message, priority=priority,
                action_url=action_url,
                action_label=action_label,
                related_object_type=related_object_type,
                related_object_id=related_object_id,
                fingerprint=fingerprint,
                cooldown_seconds=cooldown_seconds,
            )

        else:
            n = Notification.create_broadcast(
                notification_type=notification_type,
                title=title, message=message, priority=priority,
                action_url=action_url,
                action_label=action_label,
                related_object_type=related_object_type,
                related_object_id=related_object_id,
                fingerprint=fingerprint,
                cooldown_seconds=cooldown_seconds,
            )
            if n:
                created.append(n)

        # Optionally send email for critical notifications
        if send_email_alert and created:
            _send_email_alert_for_notifications(created, title, message, priority)

    except Exception as e:
        logger.error(f"[notify] Failed to create notification '{title}': {e}")

    return created


def _send_email_alert_for_notifications(notifications, title, message, priority):
    """Send email alerts for critical notifications to authorized active recipients."""
    from .models import Notification
    from accounts.models import User
    from accounts.email_service import get_from_email

    site_name = getattr(settings, 'SANALOGX_SITE_NAME', getattr(settings, 'MEDIVAULT_SITE_NAME', 'Sanalogx'))
    from_email = get_from_email()

    for n in notifications:
        if not n or n.email_sent:
            continue

        # Determine authorized recipient email(s)
        recipient_emails = []
        if n.recipient:
            if n.recipient.is_active and n.recipient.status == User.Status.APPROVED and n.recipient.email:
                recipient_emails.append(n.recipient.email.strip())
        else:
            # Broadcast notification -> send to authorized active ADMIN staff
            admin_emails = list(
                User.objects.filter(
                    role=User.Role.ADMIN, is_active=True, status=User.Status.APPROVED
                ).exclude(email='').values_list('email', flat=True)
            )
            recipient_emails.extend([e.strip() for e in admin_emails if e.strip()])

        if not recipient_emails:
            continue

        try:
            html_body = render_to_string('emails/notification_alert.html', {
                'title': title,
                'message': message,
                'priority': priority,
                'action_url': n.action_url,
                'site_url': getattr(settings, 'SANALOGX_BASE_URL', getattr(settings, 'MEDIVAULT_BASE_URL', 'http://127.0.0.1:8000')),
            })

            unique_recipients = sorted(list(set(recipient_emails)))
            any_sent = False

            for recipient_email in unique_recipients:
                try:
                    sent_count = send_mail(
                        subject=f"[{site_name} {priority}] {title}",
                        message=message,
                        from_email=from_email,
                        recipient_list=[recipient_email],
                        html_message=html_body,
                        fail_silently=False,
                    )
                    if sent_count > 0:
                        any_sent = True
                        logger.info(f"[notify email] Successfully delivered alert email '{title}' to {recipient_email}")
                except Exception as send_err:
                    logger.error(f"[notify email] Delivery failure for alert email '{title}' to {recipient_email}: {send_err}")
                    if len(unique_recipients) == 1:
                        raise send_err

            if any_sent:
                n.email_sent = True
                n.save(update_fields=['email_sent'])
            else:
                n.email_sent = False
                n.save(update_fields=['email_sent'])

        except Exception as e:
            n.email_sent = False
            try:
                n.save(update_fields=['email_sent'])
            except Exception:
                pass
            logger.error(f"[notify email] Delivery failure for alert email '{title}' to {recipient_emails}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# STOCK & INVENTORY NOTIFICATIONS
# ─────────────────────────────────────────────────────────────────────────────

def check_and_notify_low_stock():
    """
    Check inventories and generate smart LOW_STOCK / STOCKOUT_RISK / OUT_OF_STOCK notifications.
    Considers:
      - Current stock vs reorder level & safety stock
      - Forecast demand rate and Days of Stock Remaining
      - Supplier delivery lead time (alerts before stock runs out during lead time)
      - Pending Open PO quantities (avoids redundant panic when order is already in flight)
    Deduplicated weekly via fingerprint.
    """
    from inventory.models import Inventory
    from purchases.models import PurchaseOrderItem, PurchaseOrder
    from ai_engine.intelligence_service import get_demand_forecast, get_best_supplier
    from .models import Notification

    today = timezone.now().date()
    week_key = today.isocalendar()[1]  # ISO week number

    low_items = Inventory.objects.filter(
        Q(stock_status__in=['LOW_STOCK', 'OUT_OF_STOCK']) |
        Q(current_stock__lte=F('reorder_level'))
    ).select_related('medicine')

    for inv in low_items:
        med = inv.medicine
        current_stock = inv.current_stock
        reorder_level = inv.reorder_level

        # Check pending open PO quantity
        open_po_qty = PurchaseOrderItem.objects.filter(
            medicine=med,
            purchase_order__status__in=[
                PurchaseOrder.POStatus.APPROVED,
                PurchaseOrder.POStatus.ORDERED,
                PurchaseOrder.POStatus.PARTIAL_RECEIPT,
            ]
        ).aggregate(s=Sum('quantity_pending'))['s'] or 0

        # Calculate demand rate & lead-time stockout risk
        forecast = get_demand_forecast(med)
        fc_30d = forecast.get('forecast_30d', 0)
        daily_rate = fc_30d / 30.0 if fc_30d > 0 else 0
        days_remaining = int(current_stock / daily_rate) if daily_rate > 0 else 9999

        best_sm = get_best_supplier(med)
        supplier = best_sm.supplier if best_sm else None
        lead_days = supplier.average_delivery_days if supplier else 7
        stockout_gap = lead_days - days_remaining  # positive = will run out before delivery

        is_out = current_stock == 0
        is_lead_risk = stockout_gap > 0 and open_po_qty == 0

        if is_out:
            n_type = Notification.NotificationType.OUT_OF_STOCK
            priority = Notification.Priority.CRITICAL
            title = f"Out of Stock: {med.medicine_name}"
            message = (
                f"{med.medicine_name} is completely out of stock! "
                f"Immediate replenishment required."
            )
        elif is_lead_risk:
            n_type = Notification.NotificationType.CRITICAL_STOCK
            priority = Notification.Priority.CRITICAL
            title = f"Stockout Risk ({days_remaining}d stock vs {lead_days}d lead): {med.medicine_name}"
            message = (
                f"High-priority stockout risk: {med.medicine_name} has only ~{days_remaining} days of stock remaining, "
                f"but supplier lead time is {lead_days} days (+{stockout_gap}d gap). "
                f"Order now to prevent complete stockout before arrival."
            )
        else:
            n_type = Notification.NotificationType.LOW_STOCK
            priority = Notification.Priority.HIGH
            po_note = f" ({open_po_qty} units already on order)." if open_po_qty > 0 else "."
            title = f"Low Stock: {med.medicine_name}"
            message = (
                f"{med.medicine_name} is below reorder level. "
                f"Current: {current_stock} units, Reorder: {reorder_level} units{po_note}"
            )

        fingerprint = f"{n_type}:{med.id}:{today.year}:{week_key}"

        notify(
            notification_type=n_type,
            title=title, message=message,
            priority=priority,
            role='ADMIN',
            action_url=f'/inventory/medicine/{med.id}/',
            action_label='View Inventory',
            related_object_type='Medicine',
            related_object_id=med.id,
            fingerprint=fingerprint,
            send_email_alert=(is_out or is_lead_risk),
        )


def check_and_notify_expiry(days_threshold=30):
    """
    Check batches expiring within days_threshold and create NEAR_EXPIRY notifications.
    Fingerprint: 'NEAR_EXPIRY:{batch_id}:{threshold_days}' to deduplicate.
    """
    from medicines.models import MedicineBatch
    from .models import Notification

    today = timezone.now().date()
    cutoff = today + timedelta(days=days_threshold)

    batches = MedicineBatch.objects.filter(
        expiry_date__gt=today,
        expiry_date__lte=cutoff,
        quantity_available__gt=0,
    ).exclude(status__in=['EXPIRED', 'RETURNED', 'QUARANTINED']).select_related('medicine')

    for batch in batches:
        days_remaining = (batch.expiry_date - today).days
        threshold_key = '7' if days_threshold <= 7 else '30'
        fingerprint = f"NEAR_EXPIRY:{batch.id}:{threshold_key}"

        priority = Notification.Priority.CRITICAL if days_remaining <= 7 else Notification.Priority.HIGH

        title = f"Expiry Alert: {batch.medicine.medicine_name} ({days_remaining} days)"
        message = (
            f"Batch {batch.batch_number} of {batch.medicine.medicine_name} expires on "
            f"{batch.expiry_date.strftime('%d %b %Y')} ({days_remaining} days remaining). "
            f"Available quantity: {batch.quantity_available} units. Prioritize FEFO dispensing."
        )

        notify(
            notification_type=Notification.NotificationType.NEAR_EXPIRY,
            title=title, message=message,
            priority=priority,
            role='PHARMACIST',
            action_url='/inventory/expiry/',
            action_label='View Expiry Management',
            related_object_type='MedicineBatch',
            related_object_id=batch.id,
            fingerprint=fingerprint,
            send_email_alert=(days_remaining <= 7),
        )


def check_and_notify_expired_batches():
    """Create EXPIRED_BATCH notifications for batches that crossed expiry today."""
    from medicines.models import MedicineBatch
    from .models import Notification

    today = timezone.now().date()

    expired = MedicineBatch.objects.filter(
        expiry_date__lt=today,
        quantity_available__gt=0,
        status__in=['ACTIVE'],
    ).select_related('medicine')

    for batch in expired:
        fingerprint = f"EXPIRED:{batch.id}:{today.year}"
        title = f"Expired Stock: {batch.medicine.medicine_name} (Batch {batch.batch_number})"
        message = (
            f"Batch {batch.batch_number} of {batch.medicine.medicine_name} expired on "
            f"{batch.expiry_date.strftime('%d %b %Y')}. "
            f"Quantity remaining: {batch.quantity_available} units. Immediate write-off required."
        )
        notify(
            notification_type=Notification.NotificationType.EXPIRED_BATCH,
            title=title, message=message,
            priority=Notification.Priority.CRITICAL,
            role='ADMIN',
            action_url='/inventory/expiry/',
            action_label='View Expiry Management',
            related_object_type='MedicineBatch',
            related_object_id=batch.id,
            fingerprint=fingerprint,
            send_email_alert=True,
        )


def check_and_notify_overdue_orders():
    """Create PO_OVERDUE notifications for purchase orders past expected delivery date."""
    from purchases.models import PurchaseOrder
    from .models import Notification

    today = timezone.now().date()

    overdue_pos = PurchaseOrder.objects.filter(
        status__in=[
            PurchaseOrder.POStatus.APPROVED,
            PurchaseOrder.POStatus.ORDERED,
            PurchaseOrder.POStatus.PARTIAL_RECEIPT,
        ],
        expected_delivery_date__lt=today,
    ).select_related('supplier')

    for po in overdue_pos:
        days_overdue = (today - po.expected_delivery_date).days
        fingerprint = f"PO_OVERDUE:{po.id}:{today.isocalendar()[1]}"

        title = f"PO Overdue: {po.po_number} ({days_overdue} days late)"
        message = (
            f"Purchase Order {po.po_number} from {po.supplier.company_name} is "
            f"{days_overdue} days overdue. Expected delivery: {po.expected_delivery_date.strftime('%d %b %Y')}. "
            f"Follow up with supplier."
        )
        notify(
            notification_type=Notification.NotificationType.PO_OVERDUE,
            title=title, message=message,
            priority=Notification.Priority.HIGH,
            role='ADMIN',
            action_url=f'/purchases/{po.id}/',
            action_label='View Purchase Order',
            related_object_type='PurchaseOrder',
            related_object_id=po.id,
            fingerprint=fingerprint,
        )


def check_and_notify_dead_stock():
    """Create AI_DEAD_STOCK notifications for medicines with no movement in 90+ days."""
    from ai_engine.intelligence_service import get_dead_stock
    from .models import Notification

    today = timezone.now().date()
    week_key = today.isocalendar()[1]

    dead_list = get_dead_stock(days_threshold=90)

    for item in dead_list[:20]:  # Cap at 20 per run
        med = item['medicine']
        fingerprint = f"DEAD_STOCK:{med.id}:{today.year}:{week_key}"

        title = f"Dead Stock: {med.medicine_name} ({item['days_inactive']} days inactive)"
        message = (
            f"{med.medicine_name} has {item['current_stock']} units in stock with no sales or "
            f"dispensing for {item['days_inactive']} days. "
            f"Estimated inventory value: ₹{item['inventory_value']}. Consider redistribution."
        )
        notify(
            notification_type=Notification.NotificationType.AI_DEAD_STOCK,
            title=title, message=message,
            priority=Notification.Priority.MEDIUM,
            role='ADMIN',
            action_url='/ai-intelligence/',
            action_label='View AI Dashboard',
            related_object_type='Medicine',
            related_object_id=med.id,
            fingerprint=fingerprint,
        )


def check_and_notify_overstock():
    """Create AI_OVERSTOCK notifications for medicines exceeding maximum stock limit."""
    from ai_engine.intelligence_service import get_overstock
    from .models import Notification

    today = timezone.now().date()
    week_key = today.isocalendar()[1]

    overstock_list = get_overstock()

    for item in overstock_list[:20]:
        med = item['medicine']
        fingerprint = f"OVERSTOCK:{med.id}:{today.year}:{week_key}"

        title = f"Overstock Warning: {med.medicine_name} (+{item['excess_qty']} excess)"
        message = (
            f"{med.medicine_name} current stock ({item['current_stock']} units) exceeds configured maximum "
            f"({item['maximum_stock']} units) by {item['excess_qty']} units. "
            f"Excess inventory value: ₹{item['excess_value']}. Hold further reorders."
        )
        notify(
            notification_type=Notification.NotificationType.AI_OVERSTOCK,
            title=title, message=message,
            priority=Notification.Priority.LOW,
            role='ADMIN',
            action_url='/inventory/dashboard/',
            action_label='View Inventory',
            related_object_type='Medicine',
            related_object_id=med.id,
            fingerprint=fingerprint,
        )


# ─────────────────────────────────────────────────────────────────────────────
# REPORT GENERATION NOTIFICATION
# ─────────────────────────────────────────────────────────────────────────────

def notify_report_generated(user, report_record):
    """Notify user that their report has been generated successfully."""
    from .models import Notification
    notify(
        notification_type=Notification.NotificationType.REPORT_GENERATED,
        title=f"Report Ready: {report_record.report_id}",
        message=f"Your {report_record.get_report_type_display()} has been generated successfully. Report ID: {report_record.report_id}.",
        priority=Notification.Priority.INFO,
        recipient=user,
        action_url=f'/reports/history/',
        action_label='View Report History',
        fingerprint=f"REPORT:{report_record.report_id}",
    )


# ─────────────────────────────────────────────────────────────────────────────
# SUPPLIER PRICE CHANGE NOTIFICATIONS
# ─────────────────────────────────────────────────────────────────────────────

def notify_price_change(supplier_price_history):
    """Notify admin when supplier price changes significantly."""
    from .models import Notification

    sph = supplier_price_history
    change_pct = float(sph.price_change_pct) if sph.price_change_pct else 0
    is_increase = change_pct > 0
    today = timezone.now().date()

    fingerprint = f"PRICE:{'INC' if is_increase else 'DEC'}:{sph.id}:{today}"
    n_type = Notification.NotificationType.PRICE_INCREASE if is_increase else Notification.NotificationType.PRICE_DROP
    priority = Notification.Priority.HIGH if is_increase else Notification.Priority.INFO

    title = f"Supplier Price {'Increased' if is_increase else 'Decreased'}: {sph.supplier_medicine.medicine.medicine_name}"
    message = (
        f"{sph.supplier_medicine.supplier.company_name} {'raised' if is_increase else 'lowered'} "
        f"the price of {sph.supplier_medicine.medicine.medicine_name} by {abs(change_pct):.1f}% "
        f"(₹{sph.old_price} → ₹{sph.new_price})."
    )
    notify(
        notification_type=n_type,
        title=title, message=message,
        priority=priority,
        role='ADMIN',
        action_url='/suppliers/price-comparison/',
        action_label='View Price Comparison',
        fingerprint=fingerprint,
    )


# ─────────────────────────────────────────────────────────────────────────────
# PURCHASE ORDER NOTIFICATIONS
# ─────────────────────────────────────────────────────────────────────────────

def notify_po_status_change(po, new_status, performed_by=None):
    """Notify relevant users on PO status changes."""
    from .models import Notification

    status_map = {
        'APPROVED': (Notification.NotificationType.PO_APPROVED, 'ADMIN', Notification.Priority.MEDIUM),
        'ORDERED': (Notification.NotificationType.PO_SENT, 'ADMIN', Notification.Priority.MEDIUM),
        'REJECTED': (Notification.NotificationType.PO_REJECTED, 'ADMIN', Notification.Priority.HIGH),
        'CANCELLED': (Notification.NotificationType.PO_REJECTED, 'ADMIN', Notification.Priority.HIGH),
        'PARTIAL_RECEIPT': (Notification.NotificationType.PO_DELIVERED, None, Notification.Priority.MEDIUM),
        'COMPLETED': (Notification.NotificationType.PO_DELIVERED, None, Notification.Priority.MEDIUM),
        'DELIVERED': (Notification.NotificationType.PO_DELIVERED, None, Notification.Priority.MEDIUM),
    }

    if new_status not in status_map:
        return

    n_type, role, priority = status_map[new_status]
    fingerprint = f"PO:{new_status}:{po.id}"

    title = f"PO {new_status.replace('_', ' ').title()}: {po.po_number}"
    message = f"Purchase Order {po.po_number} from {po.supplier.company_name} is now {new_status.replace('_', ' ').lower()}."

    notify(
        notification_type=n_type,
        title=title, message=message,
        priority=priority,
        role=role,
        action_url=f'/purchases/{po.id}/',
        action_label='View Purchase Order',
        fingerprint=fingerprint,
    )


# ─────────────────────────────────────────────────────────────────────────────
# AUTH NOTIFICATIONS
# ─────────────────────────────────────────────────────────────────────────────

def notify_pharmacist_registered(user):
    """Notify admin when a new pharmacist registers."""
    from .models import Notification
    fingerprint = f"REG:{user.id}"
    notify(
        notification_type=Notification.NotificationType.PHARMACIST_REGISTERED,
        title=f"New Pharmacist Registration: {user.username}",
        message=f"{user.get_full_name() or user.username} has registered and is awaiting approval. Email: {user.email}",
        priority=Notification.Priority.MEDIUM,
        role='ADMIN',
        action_url='/auth/admin/approvals/',
        action_label='Review Registration',
        fingerprint=fingerprint,
    )


def notify_pharmacist_approved(user):
    """Notify pharmacist their account has been approved."""
    from .models import Notification
    fingerprint = f"APPROVED:{user.id}"
    notify(
        notification_type=Notification.NotificationType.PHARMACIST_APPROVED,
        title="Your Sanalogx Account Has Been Approved",
        message=f"Congratulations! Your pharmacist account has been approved. You can now log in to Sanalogx.",
        priority=Notification.Priority.HIGH,
        recipient=user,
        action_url='/auth/login/',
        action_label='Login Now',
        fingerprint=fingerprint,
    )


def resolve_stock_notifications(medicine):
    """
    Mark unread LOW_STOCK and OUT_OF_STOCK notifications for a medicine as READ / resolved
    when inventory stock has recovered above the reorder level.
    """
    from .models import Notification
    from django.utils import timezone
    Notification.objects.filter(
        related_object_type='Medicine',
        related_object_id=medicine.id,
        notification_type__in=[
            Notification.NotificationType.LOW_STOCK,
            Notification.NotificationType.OUT_OF_STOCK,
            Notification.NotificationType.CRITICAL_STOCK,
        ],
        is_read=False,
    ).update(is_read=True, status=Notification.Status.READ, read_at=timezone.now())

