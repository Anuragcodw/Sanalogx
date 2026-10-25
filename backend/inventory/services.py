import uuid
from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone
from .models import Inventory, StockTransaction, StockAlert
from medicines.models import Medicine, MedicineBatch
from datetime import timedelta


def _generate_tx_number(prefix="TX"):
    return f"{prefix}-{timezone.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"


def recalculate_inventory(inventory):
    """
    Recalculate current_stock and available_stock for the given Inventory instance.
    - current_stock: total quantity of medicine across all active, near expiry, expired, or quarantined batches.
    - available_stock: total quantity across active & near expiry batches only (excludes expired/quarantined/returned).
    """
    batches = inventory.medicine.batches.all()
    
    total_physical = 0
    total_available = 0
    
    for b in batches:
        # Dynamically ensure batch status is calculated
        total_physical += b.quantity_available
        if b.status in ['ACTIVE', 'NEAR_EXPIRY']:
            total_available += b.quantity_available
            
    inventory.current_stock = total_physical
    inventory.available_stock = total_available
    inventory.save()


@transaction.atomic
def add_stock(medicine, batch, quantity, transaction_type, performed_by, reference_number="", notes="", transaction_number=""):
    """
    Adds stock to a medicine batch and records the transaction.
    """
    if quantity <= 0:
        raise ValidationError("Quantity to add must be greater than zero.")
        
    # Update batch stock with row-level lock
    batch = MedicineBatch.objects.select_for_update().get(pk=batch.pk)
    if transaction_type == StockTransaction.TransactionType.PURCHASE:
        batch.quantity_received += quantity
    batch.quantity_available += quantity
    
    # Recalculate status of batch
    days = (batch.expiry_date - timezone.now().date()).days
    if days <= 0:
        batch.status = MedicineBatch.BatchStatus.EXPIRED
    elif days <= 90:
        batch.status = MedicineBatch.BatchStatus.NEAR_EXPIRY
    else:
        batch.status = MedicineBatch.BatchStatus.ACTIVE
    batch.save()

    # Get or create inventory summary
    inventory, created = Inventory.objects.get_or_create(
        medicine=medicine,
        defaults={
            'minimum_stock': medicine.minimum_stock,
            'maximum_stock': medicine.maximum_stock,
            'reorder_level': medicine.reorder_level,
            'safety_stock': medicine.safety_stock,
        }
    )
    
    previous_stock = inventory.current_stock
    new_stock = previous_stock + quantity
    tx_num = transaction_number or _generate_tx_number("TX-ADD")
    
    # Create Audit Transaction
    txn = StockTransaction.objects.create(
        transaction_number=tx_num,
        inventory=inventory,
        medicine=medicine,
        batch=batch,
        transaction_type=transaction_type,
        quantity=quantity,
        previous_stock=previous_stock,
        new_stock=new_stock,
        reference_number=reference_number,
        notes=notes,
        performed_by=performed_by
    )
    
    recalculate_inventory(inventory)
    generate_reorder_alerts(inventory)
    if transaction_type == StockTransaction.TransactionType.RETURN:
        try:
            from .consumption_service import refresh_consumption_cache
            refresh_consumption_cache(medicine)
        except Exception:
            pass
    return txn


@transaction.atomic
def remove_stock(medicine, batch, quantity, transaction_type, performed_by, reference_number="", notes="", transaction_number=""):
    """
    Deducts stock from a medicine batch and records the transaction.
    Enforces concurrency lock, available stock bounds, and clinical batch validity.
    """
    if quantity <= 0:
        raise ValidationError("Quantity to deduct must be greater than zero.")
        
    # Lock the batch row to prevent race conditions and overselling
    batch = MedicineBatch.objects.select_for_update().get(pk=batch.pk)
    if batch.quantity_available < quantity:
        raise ValidationError(f"Insufficient stock in batch {batch.batch_number} (Available: {batch.quantity_available}, Requested: {quantity}).")
        
    # Block expired, inactive, quarantined, or returned batch allocation for clinical deductions
    today = timezone.now().date()
    if batch.expiry_date <= today and transaction_type in [StockTransaction.TransactionType.SALE, StockTransaction.TransactionType.DISPENSE]:
        raise ValidationError(f"Cannot dispense or sell expired batch {batch.batch_number} (Expired: {batch.expiry_date}).")
    if batch.status in ['EXPIRED', 'QUARANTINED', 'RETURNED'] and transaction_type in [StockTransaction.TransactionType.SALE, StockTransaction.TransactionType.DISPENSE]:
        raise ValidationError(f"Cannot dispense or sell batch {batch.batch_number} in status {batch.get_status_display()}.")

    batch.quantity_available -= quantity
    batch.save()
    
    # Get or create inventory summary
    inventory, created = Inventory.objects.get_or_create(
        medicine=medicine,
        defaults={
            'minimum_stock': medicine.minimum_stock,
            'maximum_stock': medicine.maximum_stock,
            'reorder_level': medicine.reorder_level,
            'safety_stock': medicine.safety_stock,
        }
    )
    
    previous_stock = inventory.current_stock
    new_stock = previous_stock - quantity
    tx_num = transaction_number or _generate_tx_number("TX-REM")
    
    # Create Audit Transaction
    txn = StockTransaction.objects.create(
        transaction_number=tx_num,
        inventory=inventory,
        medicine=medicine,
        batch=batch,
        transaction_type=transaction_type,
        quantity=quantity,
        previous_stock=previous_stock,
        new_stock=new_stock,
        reference_number=reference_number,
        notes=notes,
        performed_by=performed_by
    )
    
    recalculate_inventory(inventory)
    generate_reorder_alerts(inventory)
    if transaction_type in [StockTransaction.TransactionType.SALE, StockTransaction.TransactionType.DISPENSE]:
        try:
            from .consumption_service import refresh_consumption_cache
            refresh_consumption_cache(medicine)
        except Exception:
            pass
    return txn


@transaction.atomic
def adjust_stock(medicine, batch, adjustment_type, quantity, performed_by, reason, notes="", transaction_number="", reference_number=""):
    """
    Controlled stock adjustments (Physical stock count, Data entry errors, Reconciliation).
    """
    if quantity <= 0:
        raise ValidationError("Quantity must be greater than zero.")
        
    batch = MedicineBatch.objects.select_for_update().get(pk=batch.pk)
    if adjustment_type == 'Decrease' and batch.quantity_available < quantity:
        raise ValidationError(f"Cannot adjust down by {quantity}; only {batch.quantity_available} available in batch {batch.batch_number}.")
        
    # Get or create inventory summary
    inventory, created = Inventory.objects.get_or_create(
        medicine=medicine,
        defaults={
            'minimum_stock': medicine.minimum_stock,
            'maximum_stock': medicine.maximum_stock,
            'reorder_level': medicine.reorder_level,
            'safety_stock': medicine.safety_stock,
        }
    )
    
    previous_stock = inventory.current_stock
    
    if adjustment_type == 'Increase':
        batch.quantity_available += quantity
        net_quantity = quantity
        new_stock = previous_stock + quantity
    else:  # Decrease or Correction
        batch.quantity_available -= quantity
        net_quantity = quantity
        new_stock = previous_stock - quantity
        
    batch.save()
    tx_num = transaction_number or _generate_tx_number("TX-ADJ")
    
    # Create Audit Transaction
    txn = StockTransaction.objects.create(
        transaction_number=tx_num,
        inventory=inventory,
        medicine=medicine,
        batch=batch,
        transaction_type=StockTransaction.TransactionType.ADJUSTMENT,
        quantity=net_quantity,
        previous_stock=previous_stock,
        new_stock=new_stock,
        reference_number=reference_number or reason,
        notes=notes,
        performed_by=performed_by
    )
    
    recalculate_inventory(inventory)
    generate_reorder_alerts(inventory)
    return txn


@transaction.atomic
def record_damage(medicine, batch, quantity, performed_by, reason, notes="", transaction_number=""):
    """
    Deducts quantity due to broken packaging, physical damage, temperature contamination, etc.
    """
    if quantity <= 0:
        raise ValidationError("Quantity must be greater than zero.")
        
    batch = MedicineBatch.objects.select_for_update().get(pk=batch.pk)
    if batch.quantity_available < quantity:
        raise ValidationError(f"Cannot record damage of {quantity}; only {batch.quantity_available} available in batch {batch.batch_number}.")
        
    batch.quantity_available -= quantity
    batch.save()
    
    inventory, created = Inventory.objects.get_or_create(
        medicine=medicine,
        defaults={
            'minimum_stock': medicine.minimum_stock,
            'maximum_stock': medicine.maximum_stock,
            'reorder_level': medicine.reorder_level,
            'safety_stock': medicine.safety_stock,
        }
    )
    
    previous_stock = inventory.current_stock
    new_stock = previous_stock - quantity
    tx_num = transaction_number or _generate_tx_number("TX-DAM")
    
    # Create Audit Transaction
    txn = StockTransaction.objects.create(
        transaction_number=tx_num,
        inventory=inventory,
        medicine=medicine,
        batch=batch,
        transaction_type=StockTransaction.TransactionType.DAMAGE,
        quantity=quantity,
        previous_stock=previous_stock,
        new_stock=new_stock,
        reference_number=reason,
        notes=notes,
        performed_by=performed_by
    )
    
    recalculate_inventory(inventory)
    generate_reorder_alerts(inventory)
    return txn


@transaction.atomic
def record_expiry(medicine, batch, quantity, performed_by, notes="", transaction_number=""):
    """
    Deducts stock due to expiry write-off.
    """
    if quantity <= 0:
        raise ValidationError("Quantity must be greater than zero.")
        
    batch = MedicineBatch.objects.select_for_update().get(pk=batch.pk)
    if batch.quantity_available < quantity:
        raise ValidationError(f"Cannot record expiry of {quantity}; only {batch.quantity_available} available in batch {batch.batch_number}.")
        
    batch.quantity_available -= quantity
    batch.status = MedicineBatch.BatchStatus.EXPIRED
    batch.save()
    
    inventory, created = Inventory.objects.get_or_create(
        medicine=medicine,
        defaults={
            'minimum_stock': medicine.minimum_stock,
            'maximum_stock': medicine.maximum_stock,
            'reorder_level': medicine.reorder_level,
            'safety_stock': medicine.safety_stock,
        }
    )
    
    previous_stock = inventory.current_stock
    new_stock = previous_stock - quantity
    tx_num = transaction_number or _generate_tx_number("TX-EXP")
    
    # Create Audit Transaction
    txn = StockTransaction.objects.create(
        transaction_number=tx_num,
        inventory=inventory,
        medicine=medicine,
        batch=batch,
        transaction_type=StockTransaction.TransactionType.EXPIRY,
        quantity=quantity,
        previous_stock=previous_stock,
        new_stock=new_stock,
        reference_number="Batch Expired",
        notes=notes,
        performed_by=performed_by
    )
    
    recalculate_inventory(inventory)
    generate_reorder_alerts(inventory)
    return txn


@transaction.atomic
def record_return(medicine, batch, quantity, performed_by, reason="", reference_number="", notes="", transaction_number=""):
    """
    Restores stock to a batch due to customer or clinical return.
    """
    ref = reference_number or reason or f"RET-{batch.batch_number}"
    note = notes or f"Medicine return: {reason or 'Patient/Clinical return'}"
    return add_stock(
        medicine=medicine,
        batch=batch,
        quantity=quantity,
        transaction_type=StockTransaction.TransactionType.RETURN,
        performed_by=performed_by,
        reference_number=ref,
        notes=note,
        transaction_number=transaction_number,
    )


def allocate_stock(medicine, quantity_needed, policy='FEFO'):
    """
    Returns a breakdown of which active, non-expired batches to use based on FEFO or FIFO.
    """
    batches = medicine.batches.filter(
        quantity_available__gt=0,
        expiry_date__gt=timezone.now().date()
    ).exclude(
        status__in=['EXPIRED', 'QUARANTINED', 'RETURNED']
    )
    
    if policy == 'FIFO':
        batches = batches.order_by('received_date', 'id')
    else:  # FEFO (Default)
        batches = batches.order_by('expiry_date', 'id')
        
    total_available = sum(b.quantity_available for b in batches)
    if total_available < quantity_needed:
        raise ValidationError(f"Insufficient available stock to allocate {quantity_needed} units. (Available: {total_available})")
        
    allocations = []
    remaining = quantity_needed
    
    for b in batches:
        if remaining <= 0:
            break
        use_qty = min(b.quantity_available, remaining)
        allocations.append({
            'batch': b,
            'quantity': use_qty
        })
        remaining -= use_qty
        
    return allocations


@transaction.atomic
def generate_expiry_alerts():
    """
    Iterates through all non-depleted batches and triggers stock alerts based on expiry date.
    - Info: Expires in 61-90 days
    - Warning: Expires in 31-60 days
    - Critical: Expires in <= 30 days
    - Expired: Exceeded expiry date
    """
    batches = MedicineBatch.objects.filter(quantity_available__gt=0).exclude(status='RETURNED')
    today = timezone.now().date()
    
    for b in batches:
        days = (b.expiry_date - today).days
        
        if days <= 0:
            b.status = MedicineBatch.BatchStatus.EXPIRED
            b.save()
            # Generate / update alert
            StockAlert.objects.update_or_create(
                medicine=b.medicine,
                alert_type=StockAlert.AlertType.EXPIRED,
                is_active=True,
                defaults={
                    'batch': b,
                    'severity': StockAlert.AlertSeverity.CRITICAL,
                    'message': f"CRITICAL: Batch {b.batch_number} of {b.medicine.medicine_name} has expired on {b.expiry_date}."
                }
            )
        elif days <= 30:
            b.status = MedicineBatch.BatchStatus.NEAR_EXPIRY
            b.save()
            StockAlert.objects.update_or_create(
                medicine=b.medicine,
                alert_type=StockAlert.AlertType.NEAR_EXPIRY,
                is_active=True,
                defaults={
                    'batch': b,
                    'severity': StockAlert.AlertSeverity.CRITICAL,
                    'message': f"CRITICAL: Batch {b.batch_number} of {b.medicine.medicine_name} expires in {days} days ({b.expiry_date})."
                }
            )
        elif days <= 60:
            b.status = MedicineBatch.BatchStatus.NEAR_EXPIRY
            b.save()
            StockAlert.objects.update_or_create(
                medicine=b.medicine,
                alert_type=StockAlert.AlertType.NEAR_EXPIRY,
                is_active=True,
                defaults={
                    'batch': b,
                    'severity': StockAlert.AlertSeverity.WARNING,
                    'message': f"WARNING: Batch {b.batch_number} of {b.medicine.medicine_name} expires in {days} days ({b.expiry_date})."
                }
            )
        elif days <= 90:
            b.status = MedicineBatch.BatchStatus.NEAR_EXPIRY
            b.save()
            StockAlert.objects.update_or_create(
                medicine=b.medicine,
                alert_type=StockAlert.AlertType.NEAR_EXPIRY,
                is_active=True,
                defaults={
                    'batch': b,
                    'severity': StockAlert.AlertSeverity.INFO,
                    'message': f"INFO: Batch {b.batch_number} of {b.medicine.medicine_name} expires in {days} days ({b.expiry_date})."
                }
            )
            
        # Update inventory calculations
        inventory, created = Inventory.objects.get_or_create(medicine=b.medicine)
        recalculate_inventory(inventory)


def generate_reorder_alerts(inventory):
    """
    Checks if stock has fallen below thresholds and issues appropriate alerts.
    """
    medicine = inventory.medicine
    
    # Resolve old alerts if stock is healthy
    if inventory.current_stock > inventory.reorder_level:
        StockAlert.objects.filter(medicine=medicine, alert_type=StockAlert.AlertType.LOW_STOCK, is_active=True).update(is_active=False, resolved_at=timezone.now())
        StockAlert.objects.filter(medicine=medicine, alert_type=StockAlert.AlertType.OUT_OF_STOCK, is_active=True).update(is_active=False, resolved_at=timezone.now())
        try:
            from notifications.service import resolve_stock_notifications
            resolve_stock_notifications(medicine)
        except Exception:
            pass
        return
        
    if inventory.current_stock == 0:
        # Resolve Low Stock alert if it is now out of stock
        StockAlert.objects.filter(medicine=medicine, alert_type=StockAlert.AlertType.LOW_STOCK, is_active=True).update(is_active=False, resolved_at=timezone.now())
        
        StockAlert.objects.update_or_create(
            medicine=medicine,
            alert_type=StockAlert.AlertType.OUT_OF_STOCK,
            is_active=True,
            defaults={
                'severity': StockAlert.AlertSeverity.CRITICAL,
                'message': f"CRITICAL: {medicine.medicine_name} is completely out of stock!",
                'recommended_reorder_qty': inventory.maximum_stock
            }
        )
        try:
            from notifications.service import notify
            from notifications.models import Notification
            notify(
                notification_type=Notification.NotificationType.OUT_OF_STOCK,
                title=f"Out of Stock: {medicine.medicine_name}",
                message=f"CRITICAL: {medicine.medicine_name} is completely out of stock!",
                priority=Notification.Priority.CRITICAL,
                role='ADMIN',
                action_url=f"/inventory/medicine/{medicine.id}/",
                action_label="View Inventory",
                related_object_type="Medicine",
                related_object_id=medicine.id,
                fingerprint=f"OOS:{medicine.id}:{timezone.now().date()}",
            )
        except Exception:
            pass
    elif inventory.current_stock <= inventory.reorder_level:
        # Resolve Out of Stock alert if we got some stock back but still low
        StockAlert.objects.filter(medicine=medicine, alert_type=StockAlert.AlertType.OUT_OF_STOCK, is_active=True).update(is_active=False, resolved_at=timezone.now())
        
        severity = StockAlert.AlertSeverity.WARNING
        if inventory.current_stock <= inventory.safety_stock:
            severity = StockAlert.AlertSeverity.CRITICAL
            
        StockAlert.objects.update_or_create(
            medicine=medicine,
            alert_type=StockAlert.AlertType.LOW_STOCK,
            is_active=True,
            defaults={
                'severity': severity,
                'message': f"WARNING: {medicine.medicine_name} is low on stock! (Current: {inventory.current_stock} units, Reorder Level: {inventory.reorder_level}).",
                'recommended_reorder_qty': inventory.maximum_stock - inventory.current_stock
            }
        )
        try:
            from notifications.service import notify
            from notifications.models import Notification
            notify(
                notification_type=Notification.NotificationType.LOW_STOCK,
                title=f"Low Stock: {medicine.medicine_name}",
                message=f"WARNING: {medicine.medicine_name} is low on stock! (Current: {inventory.current_stock} units, Reorder: {inventory.reorder_level}).",
                priority=Notification.Priority.HIGH,
                role='ADMIN',
                action_url=f"/inventory/medicine/{medicine.id}/",
                action_label="View Inventory",
                related_object_type="Medicine",
                related_object_id=medicine.id,
                fingerprint=f"LOW_STOCK:{medicine.id}:{timezone.now().date()}",
            )
        except Exception:
            pass


# ─── Authoritative Central Expiry Calculation Service ─────────────────────────

class ExpiryRiskCategory:
    EXPIRED = 'EXPIRED'
    CRITICAL = 'CRITICAL'
    WARNING = 'WARNING'
    NEAR_EXPIRY = 'NEAR_EXPIRY'
    NORMAL = 'NORMAL'
    UNKNOWN = 'UNKNOWN'


def get_batch_expiry_info(batch, today=None, near_expiry_days=90):
    """
    Central authoritative expiry calculation service for Sanalogx.
    Used consistently by Dashboard, Batch Ledger, Expiry Management, Notifications, and Reports.

    Thresholds:
    - EXPIRED: expiry date <= today.
    - CRITICAL: valid batch expires within 1..30 days.
    - WARNING: valid batch expires within 31..60 days.
    - NEAR_EXPIRY: valid batch expires within 61..near_expiry_days (default 90 days).
    - NORMAL: valid batch expires in > near_expiry_days.
    - UNKNOWN: expiry date is missing or invalid.
    """
    if today is None:
        today = timezone.now().date()

    if not batch.expiry_date:
        return {
            'days_remaining': None,
            'days_abs': None,
            'risk_category': ExpiryRiskCategory.UNKNOWN,
            'risk_label': 'Unknown / Needs Review',
            'badge_class': 'secondary',
            'priority': 'secondary',
            'is_expired': False,
            'is_dispensable': False,
            'formatted_expiry': 'Not Recorded',
            'formatted_days': 'Unknown',
        }

    days = (batch.expiry_date - today).days

    if days <= 0:
        return {
            'days_remaining': days,
            'days_abs': abs(days),
            'risk_category': ExpiryRiskCategory.EXPIRED,
            'risk_label': 'Expired' if days < 0 else 'Expires Today',
            'badge_class': 'danger',
            'priority': 'danger',
            'is_expired': True,
            'is_dispensable': False,
            'formatted_expiry': batch.expiry_date.strftime('%d %b %Y'),
            'formatted_days': f"{abs(days)}d ago" if days < 0 else "Today",
        }
    elif days <= 30:
        is_dispensable = batch.quantity_available > 0 and batch.status not in ['QUARANTINED', 'RETURNED', 'EXPIRED']
        return {
            'days_remaining': days,
            'days_abs': days,
            'risk_category': ExpiryRiskCategory.CRITICAL,
            'risk_label': 'Critical (≤ 30d)',
            'badge_class': 'danger',
            'priority': 'critical',
            'is_expired': False,
            'is_dispensable': is_dispensable,
            'formatted_expiry': batch.expiry_date.strftime('%d %b %Y'),
            'formatted_days': f"{days}d left",
        }
    elif days <= 60:
        is_dispensable = batch.quantity_available > 0 and batch.status not in ['QUARANTINED', 'RETURNED', 'EXPIRED']
        return {
            'days_remaining': days,
            'days_abs': days,
            'risk_category': ExpiryRiskCategory.WARNING,
            'risk_label': 'Warning (31–60d)',
            'badge_class': 'warning',
            'priority': 'warning',
            'is_expired': False,
            'is_dispensable': is_dispensable,
            'formatted_expiry': batch.expiry_date.strftime('%d %b %Y'),
            'formatted_days': f"{days}d left",
        }
    elif days <= near_expiry_days:
        is_dispensable = batch.quantity_available > 0 and batch.status not in ['QUARANTINED', 'RETURNED', 'EXPIRED']
        return {
            'days_remaining': days,
            'days_abs': days,
            'risk_category': ExpiryRiskCategory.NEAR_EXPIRY,
            'risk_label': f'Near Expiry (61–{near_expiry_days}d)',
            'badge_class': 'info',
            'priority': 'info',
            'is_expired': False,
            'is_dispensable': is_dispensable,
            'formatted_expiry': batch.expiry_date.strftime('%d %b %Y'),
            'formatted_days': f"{days}d left",
        }
    else:
        is_dispensable = batch.quantity_available > 0 and batch.status not in ['QUARANTINED', 'RETURNED', 'EXPIRED']
        return {
            'days_remaining': days,
            'days_abs': days,
            'risk_category': ExpiryRiskCategory.NORMAL,
            'risk_label': f'Normal (> {near_expiry_days}d)',
            'badge_class': 'success',
            'priority': 'normal',
            'is_expired': False,
            'is_dispensable': is_dispensable,
            'formatted_expiry': batch.expiry_date.strftime('%d %b %Y'),
            'formatted_days': f"{days}d left",
        }


def get_expiry_kpis(today=None, near_expiry_days=90):
    """
    Computes mutually exclusive summary metrics across batches.
    Expired batches are strictly excluded from near-expiry and warning/critical counts.
    """
    if today is None:
        today = timezone.now().date()

    # Batches expiring within the active window (non-expired, strictly available > 0)
    near_qs = MedicineBatch.objects.filter(
        expiry_date__gt=today,
        expiry_date__lte=today + timedelta(days=near_expiry_days),
        quantity_available__gt=0
    )

    critical_count = near_qs.filter(expiry_date__lte=today + timedelta(days=30)).count()
    warning_count = near_qs.filter(
        expiry_date__gt=today + timedelta(days=30),
        expiry_date__lte=today + timedelta(days=60)
    ).count()
    near_count = near_qs.count()

    # Expired batches with remaining stock that must be acted upon
    expired_with_stock = MedicineBatch.objects.filter(
        expiry_date__lte=today,
        quantity_available__gt=0
    ).count()

    # Total expired count in system
    total_expired = MedicineBatch.objects.filter(expiry_date__lte=today).count()

    return {
        'near_expiry_count': near_count,
        'critical_count': critical_count,
        'warning_count': warning_count,
        'expired_count': expired_with_stock,
        'total_expired_count': total_expired,
        'total_batches': MedicineBatch.objects.count(),
        'active_batches': MedicineBatch.objects.filter(status='ACTIVE', quantity_available__gt=0).count(),
    }

