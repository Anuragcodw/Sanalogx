"""
inventory/health_service.py
MediVault — Inventory Health Engine

Computes days-of-stock-remaining and classifies each medicine's
inventory health into one of: OUT_OF_STOCK, CRITICAL, LOW, NORMAL,
OVERSTOCK, DEAD_STOCK, EXPIRY_RISK.

Integrates with:
  - inventory/consumption_service.py  (daily_avg for DoS calculation)
  - ai_engine/intelligence_service.py (expiry_risk)
  - inventory/models.py               (Inventory, StockAlert)
"""

from datetime import timedelta

from django.utils import timezone
from django.db.models import Sum

from medicines.models import Medicine, MedicineBatch
from inventory.models import Inventory, StockTransaction
from inventory.consumption_service import get_consumption_for_period


# ─── Classification Labels ────────────────────────────────────────────────────
class HealthStatus:
    OUT_OF_STOCK = 'OUT_OF_STOCK'
    CRITICAL = 'CRITICAL'          # ≤ safety_stock
    LOW = 'LOW'                    # ≤ reorder_level
    NORMAL = 'NORMAL'
    OVERSTOCK = 'OVERSTOCK'        # > maximum_stock
    DEAD_STOCK = 'DEAD_STOCK'      # no movement in 90d with stock > 0
    EXPIRY_RISK = 'EXPIRY_RISK'    # batch expiring soon relative to forecast


HEALTH_BADGE_COLORS = {
    HealthStatus.OUT_OF_STOCK: 'danger',
    HealthStatus.CRITICAL: 'danger',
    HealthStatus.LOW: 'warning',
    HealthStatus.NORMAL: 'success',
    HealthStatus.OVERSTOCK: 'info',
    HealthStatus.DEAD_STOCK: 'secondary',
    HealthStatus.EXPIRY_RISK: 'warning',
}


# ─── Days of Stock Remaining ─────────────────────────────────────────────────

def get_days_of_stock_remaining(medicine) -> float | None:
    """
    Days of Stock Remaining = current_stock / daily_consumption_rate

    Uses 30-day consumption average for the daily rate.
    Returns None if daily_rate == 0 (cannot be calculated).
    Returns float('inf') equivalent as 9999 when consumption is 0
    but stock exists (potential dead stock).
    """
    inv = getattr(medicine, 'inventory', None)
    current_stock = inv.current_stock if inv else 0

    if current_stock == 0:
        return 0.0

    consumed_30d = get_consumption_for_period(medicine, 30)
    if consumed_30d == 0:
        return 9999.0   # no consumption → effectively infinite, flag as potential dead stock

    daily_rate = consumed_30d / 30.0
    return round(current_stock / daily_rate, 1)


# ─── Health Classification ────────────────────────────────────────────────────

def classify_inventory_health(medicine) -> dict:
    """
    Returns:
      {
        'status': str (HealthStatus constant),
        'badge_color': str (Bootstrap color),
        'days_of_stock': float or None,
        'current_stock': int,
        'daily_avg': float,
        'flags': list[str],
        'explanation': str,
      }
    """
    inv = getattr(medicine, 'inventory', None)
    current_stock = inv.current_stock if inv else 0
    reorder_level = inv.reorder_level if inv else medicine.reorder_level
    safety_stock = inv.safety_stock if inv else medicine.safety_stock
    maximum_stock = inv.maximum_stock if inv else medicine.maximum_stock

    consumed_30d = get_consumption_for_period(medicine, 30)
    daily_avg = round(consumed_30d / 30.0, 2)
    dos = get_days_of_stock_remaining(medicine)

    today = timezone.now().date()
    flags = []

    # ── OUT_OF_STOCK ──
    if current_stock == 0:
        return {
            'status': HealthStatus.OUT_OF_STOCK,
            'badge_color': HEALTH_BADGE_COLORS[HealthStatus.OUT_OF_STOCK],
            'days_of_stock': 0.0,
            'current_stock': 0,
            'daily_avg': daily_avg,
            'flags': ['OUT_OF_STOCK'],
            'explanation': 'No units in stock. Immediate procurement required.',
        }

    # ── DEAD_STOCK check ──
    last_txn = StockTransaction.objects.filter(
        medicine=medicine,
        transaction_type__in=['SALE', 'DISPENSE'],
    ).order_by('-created_at').first()
    days_since_last_move = (timezone.now() - last_txn.created_at).days if last_txn else 9999
    if days_since_last_move >= 90:
        flags.append('DEAD_STOCK')

    # ── EXPIRY_RISK check (any batch expiring within 60 days with stock) ──
    expiry_risk_batch = MedicineBatch.objects.filter(
        medicine=medicine,
        quantity_available__gt=0,
        expiry_date__gt=today,
        expiry_date__lte=today + timedelta(days=60),
    ).exclude(status__in=['EXPIRED', 'QUARANTINED', 'RETURNED']).first()
    if expiry_risk_batch:
        days_to_expiry = (expiry_risk_batch.expiry_date - today).days
        # Only flag expiry risk if we can't consume before expiry
        if daily_avg > 0:
            can_consume = daily_avg * days_to_expiry
            if can_consume < expiry_risk_batch.quantity_available:
                flags.append('EXPIRY_RISK')
        else:
            flags.append('EXPIRY_RISK')

    # ── Priority flag resolution ──
    if 'DEAD_STOCK' in flags:
        return {
            'status': HealthStatus.DEAD_STOCK,
            'badge_color': HEALTH_BADGE_COLORS[HealthStatus.DEAD_STOCK],
            'days_of_stock': dos,
            'current_stock': current_stock,
            'daily_avg': daily_avg,
            'flags': flags,
            'explanation': (
                f"No consumption recorded in {days_since_last_move} days. "
                f"Current stock: {current_stock} units. "
                "Review procurement and consider redistribution."
            ),
        }

    if 'EXPIRY_RISK' in flags:
        return {
            'status': HealthStatus.EXPIRY_RISK,
            'badge_color': HEALTH_BADGE_COLORS[HealthStatus.EXPIRY_RISK],
            'days_of_stock': dos,
            'current_stock': current_stock,
            'daily_avg': daily_avg,
            'flags': flags,
            'explanation': (
                f"Batch expiring in {days_to_expiry} days. "
                f"At daily rate {daily_avg}/day, "
                f"{expiry_risk_batch.quantity_available} units may expire before consumption."
            ),
        }

    # ── Stock level classification ──
    if current_stock <= safety_stock:
        status = HealthStatus.CRITICAL
        explanation = (
            f"Stock ({current_stock}) ≤ safety stock ({safety_stock}). "
            f"At {daily_avg}/day, DoS remaining: {dos} days."
        )
    elif current_stock <= reorder_level:
        status = HealthStatus.LOW
        explanation = (
            f"Stock ({current_stock}) ≤ reorder level ({reorder_level}). "
            f"DoS remaining: {dos} days."
        )
    elif current_stock > maximum_stock:
        status = HealthStatus.OVERSTOCK
        explanation = (
            f"Stock ({current_stock}) exceeds maximum ({maximum_stock}). "
            "Hold further reorders."
        )
    else:
        status = HealthStatus.NORMAL
        explanation = (
            f"Stock ({current_stock}) is within normal range. "
            f"DoS remaining: {dos} days."
        )

    return {
        'status': status,
        'badge_color': HEALTH_BADGE_COLORS[status],
        'days_of_stock': dos,
        'current_stock': current_stock,
        'daily_avg': daily_avg,
        'flags': flags,
        'explanation': explanation,
    }


def get_inventory_health_summary() -> dict:
    """
    Aggregated health summary for all active medicines.
    Returns counts by health status and a list of medicine health records.
    Medicines are sorted: OUT_OF_STOCK → CRITICAL → EXPIRY_RISK → LOW → DEAD_STOCK → OVERSTOCK → NORMAL.
    """
    SORT_ORDER = {
        HealthStatus.OUT_OF_STOCK: 0,
        HealthStatus.CRITICAL: 1,
        HealthStatus.EXPIRY_RISK: 2,
        HealthStatus.LOW: 3,
        HealthStatus.DEAD_STOCK: 4,
        HealthStatus.OVERSTOCK: 5,
        HealthStatus.NORMAL: 6,
    }

    medicines = Medicine.objects.filter(status='ACTIVE').select_related(
        'category', 'inventory'
    ).prefetch_related('batches')

    records = []
    counts = {s: 0 for s in SORT_ORDER}

    for med in medicines:
        health = classify_inventory_health(med)
        health['medicine'] = med
        records.append(health)
        counts[health['status']] = counts.get(health['status'], 0) + 1

    records.sort(key=lambda x: SORT_ORDER.get(x['status'], 99))

    return {
        'records': records,
        'counts': counts,
        'total': len(records),
    }
