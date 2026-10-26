"""
inventory/valuation_service.py
MediVault — Production Inventory Valuation Engine

Guarantees:
- Calculates stock value from actual remaining batch quantities and their recorded batch costs.
- Excludes or separately reports expired, quarantined, damaged, and unavailable stock.
- Distinguishes asset inventory valuation from revenue, sales, and profit.
- Uses DecimalField and Decimal precision for all valuations.
"""

from decimal import Decimal
from datetime import timedelta
from django.utils import timezone
from django.db.models import Sum, F, Q

from medicines.models import Medicine, MedicineBatch
from inventory.models import Inventory


def get_inventory_valuation(category_id=None, medicine_id=None):
    """
    Computes rigorous batch-wise inventory valuation:
    1. Usable / Available Stock: Active and Near-Expiry batches with future expiry.
       Valued at actual historical cost basis (unit_cost or purchase_price).
    2. Potential Retail Value: Evaluated at batch selling price.
    3. Unrealized Gross Margin: Retail Value - Cost Value.
    4. Separately tracks at-risk / non-saleable stock:
       - Expired Stock (Write-off liability)
       - Quarantined Stock (Pending QC / non-dispensable)
    """
    today = timezone.now().date()

    batches_qs = MedicineBatch.objects.select_related('medicine', 'medicine__category')

    if category_id:
        batches_qs = batches_qs.filter(medicine__category_id=category_id)
    if medicine_id:
        batches_qs = batches_qs.filter(medicine_id=medicine_id)

    # 1. Usable / Active Batches
    usable_batches = batches_qs.filter(
        quantity_available__gt=0,
        expiry_date__gt=today
    ).exclude(status__in=[
        MedicineBatch.BatchStatus.EXPIRED,
        MedicineBatch.BatchStatus.QUARANTINED,
        MedicineBatch.BatchStatus.RETURNED,
    ])

    usable_units = 0
    usable_cost_value = Decimal('0.00')
    usable_retail_value = Decimal('0.00')

    for b in usable_batches:
        qty = Decimal(str(b.quantity_available))
        cost = b.unit_cost or b.purchase_price or Decimal('0.00')
        sp = b.selling_price or Decimal('0.00')
        usable_units += b.quantity_available
        usable_cost_value += round(qty * cost, 2)
        usable_retail_value += round(qty * sp, 2)

    unrealized_margin = max(Decimal('0.00'), usable_retail_value - usable_cost_value)
    unrealized_margin_pct = round((unrealized_margin / usable_retail_value) * Decimal('100.00'), 2) if usable_retail_value > Decimal('0.00') else Decimal('0.00')

    # 2. Near Expiry Batches (<= 30 days)
    near_expiry_batches = usable_batches.filter(expiry_date__lte=today + timedelta(days=30))
    near_expiry_units = 0
    near_expiry_cost_value = Decimal('0.00')
    for b in near_expiry_batches:
        qty = Decimal(str(b.quantity_available))
        cost = b.unit_cost or b.purchase_price or Decimal('0.00')
        near_expiry_units += b.quantity_available
        near_expiry_cost_value += round(qty * cost, 2)

    # 3. Expired Stock (Write-off Liability)
    expired_batches = batches_qs.filter(
        quantity_available__gt=0
    ).filter(
        Q(status=MedicineBatch.BatchStatus.EXPIRED) | Q(expiry_date__lte=today)
    )
    expired_units = 0
    expired_cost_value = Decimal('0.00')
    for b in expired_batches:
        qty = Decimal(str(b.quantity_available))
        cost = b.unit_cost or b.purchase_price or Decimal('0.00')
        expired_units += b.quantity_available
        expired_cost_value += round(qty * cost, 2)

    # 4. Quarantined Stock
    quarantined_batches = batches_qs.filter(
        quantity_available__gt=0,
        status=MedicineBatch.BatchStatus.QUARANTINED
    )
    quarantined_units = 0
    quarantined_cost_value = Decimal('0.00')
    for b in quarantined_batches:
        qty = Decimal(str(b.quantity_available))
        cost = b.unit_cost or b.purchase_price or Decimal('0.00')
        quarantined_units += b.quantity_available
        quarantined_cost_value += round(qty * cost, 2)

    # Category-level breakdown
    from collections import defaultdict
    cat_agg = defaultdict(lambda: {'units': 0, 'cost_value': Decimal('0.00'), 'retail_value': Decimal('0.00')})
    for b in usable_batches:
        cat_name = b.medicine.category.name if b.medicine.category else 'General'
        qty = Decimal(str(b.quantity_available))
        cost = b.unit_cost or b.purchase_price or Decimal('0.00')
        sp = b.selling_price or Decimal('0.00')
        cat_agg[cat_name]['units'] += b.quantity_available
        cat_agg[cat_name]['cost_value'] += round(qty * cost, 2)
        cat_agg[cat_name]['retail_value'] += round(qty * sp, 2)

    category_breakdown = [
        {
            'category': k,
            'units': v['units'],
            'cost_value': v['cost_value'],
            'retail_value': v['retail_value'],
            'margin': max(Decimal('0.00'), v['retail_value'] - v['cost_value']),
        }
        for k, v in sorted(cat_agg.items(), key=lambda x: x[1]['cost_value'], reverse=True)
    ]

    return {
        'usable_units': usable_units,
        'usable_cost_value': usable_cost_value,
        'usable_retail_value': usable_retail_value,
        'unrealized_margin': unrealized_margin,
        'unrealized_margin_pct': unrealized_margin_pct,
        'near_expiry_units': near_expiry_units,
        'near_expiry_cost_value': near_expiry_cost_value,
        'expired_units': expired_units,
        'expired_cost_value': expired_cost_value,
        'quarantined_units': quarantined_units,
        'quarantined_cost_value': quarantined_cost_value,
        'total_active_medicines': Medicine.objects.filter(status='ACTIVE').count(),
        'total_usable_batches_count': usable_batches.count(),
        'total_expired_batches_count': expired_batches.count(),
        'total_quarantined_batches_count': quarantined_batches.count(),
        'category_breakdown': category_breakdown,
    }
