"""
inventory/consumption_service.py
MediVault — Intelligent Pharmacy Inventory Optimization Platform

Dynamic Consumption Engine:
- Computes real pharmacy consumption from completed, valid outbound events.
- Central source of truth: StockTransaction records.
- Prevents double-counting: Deduplicates linked SALE and DISPENSE movements sharing common references.
- Handles returns and reversals: Net consumption reflects returns without deleting audit records.
- Strictly separates clinical consumption from non-demand stock adjustments (DAMAGE, EXPIRY, ADJUSTMENT).
- Detects stockout-suppressed periods where observed demand may underestimate true clinical demand.
- Provides dynamic day/period queries (Today, 7d, 14d, 30d, 90d, custom ranges) with zero fake data.
"""

from decimal import Decimal
from datetime import timedelta, date, datetime
from typing import Dict, Any, List, Optional

from django.db.models import Sum, Count, Q
from django.utils import timezone

from medicines.models import Medicine
from inventory.models import StockTransaction, Inventory
from analytics.date_service import resolve_date_range


# ─── Data Quality Thresholds ──────────────────────────────────────────────────
DQ_GOOD_MIN_TRANSACTIONS = 15       # ≥15 tx in 90d → GOOD data quality
DQ_WARNING_MIN_TRANSACTIONS = 5     # 5–14 tx → WARNING
# < 5 → INSUFFICIENT_DATA


def _get_outbound_transactions_query(medicine, start_dt, end_dt):
    """
    Returns the base queryset for outbound transactions of a medicine within datetime bounds.
    """
    return StockTransaction.objects.filter(
        medicine=medicine,
        created_at__gte=start_dt,
        created_at__lte=end_dt,
    )


def calculate_net_consumption_in_range(medicine, start_dt, end_dt) -> Dict[str, Any]:
    """
    Calculates deduplicated net consumption between two datetimes.
    
    Rules:
    1. Base consumption = SALE + DISPENSE transactions.
    2. Deduplication: If a SALE and a DISPENSE record share the exact same reference_number,
       only count one of them to prevent double counting.
    3. Returns: Outbound consumption is offset by customer RETURN transactions during the period.
    4. Net consumption cannot fall below 0.
    5. Damage/Expiry/Adjustment are tracked separately and not counted as normal clinical demand.
    """
    tx_qs = _get_outbound_transactions_query(medicine, start_dt, end_dt)
    
    sale_txs = list(tx_qs.filter(transaction_type=StockTransaction.TransactionType.SALE))
    disp_txs = list(tx_qs.filter(transaction_type=StockTransaction.TransactionType.DISPENSE))
    return_qty = tx_qs.filter(
        transaction_type=StockTransaction.TransactionType.RETURN
    ).aggregate(s=Sum('quantity'))['s'] or 0

    damage_qty = tx_qs.filter(
        transaction_type=StockTransaction.TransactionType.DAMAGE
    ).aggregate(s=Sum('quantity'))['s'] or 0

    expiry_qty = tx_qs.filter(
        transaction_type=StockTransaction.TransactionType.EXPIRY
    ).aggregate(s=Sum('quantity'))['s'] or 0

    # Deduplicate linked transactions and exclude cancelled sales
    from sales.models import Sale
    cancelled_sales = set(
        Sale.objects.filter(status=Sale.SaleStatus.CANCELLED).values_list('sale_number', flat=True)
    )

    seen_refs = set()
    gross_consumption = 0
    valid_tx_count = 0

    for tx in disp_txs:
        gross_consumption += tx.quantity
        valid_tx_count += 1
        if tx.reference_number:
            seen_refs.add(tx.reference_number.strip().upper())

    for tx in sale_txs:
        ref = (tx.reference_number or '').strip().upper()
        # Exclude cancelled sales
        if ref and ref in cancelled_sales:
            continue
        # If this sale explicitly links to a dispensing event already counted, do not double-count
        if ref and ref in seen_refs:
            continue
        gross_consumption += tx.quantity
        valid_tx_count += 1
        if ref:
            seen_refs.add(ref)

    net_consumption = max(0, gross_consumption - return_qty)

    return {
        'gross_consumption': gross_consumption,
        'return_quantity': return_qty,
        'net_consumption': net_consumption,
        'transaction_count': valid_tx_count,
        'damage_quantity': damage_qty,
        'expiry_quantity': expiry_qty,
    }


def get_consumption_for_period(medicine, days: int, as_of_date: Optional[date] = None) -> int:
    """
    Total net units consumed (SALE + DISPENSE minus RETURN) in the last `days` days.
    Deduplicated by transaction reference.
    """
    range_info = resolve_date_range(f"{days}d" if days in (7, 30, 90) else 'custom',
                                    date_from=(as_of_date or timezone.localdate()) - timedelta(days=days - 1),
                                    date_to=(as_of_date or timezone.localdate()),
                                    as_of_date=as_of_date)
    res = calculate_net_consumption_in_range(medicine, range_info['start_datetime'], range_info['end_datetime'])
    return res['net_consumption']


def get_today_consumption(medicine, as_of_date: Optional[date] = None) -> int:
    """Total net units consumed today."""
    range_info = resolve_date_range('today', as_of_date=as_of_date)
    res = calculate_net_consumption_in_range(medicine, range_info['start_datetime'], range_info['end_datetime'])
    return res['net_consumption']


def get_consumption_transaction_count(medicine, days: int, as_of_date: Optional[date] = None) -> int:
    """Number of valid consumption transactions in last `days` days."""
    range_info = resolve_date_range(f"{days}d" if days in (7, 30, 90) else 'custom',
                                    date_from=(as_of_date or timezone.localdate()) - timedelta(days=days - 1),
                                    date_to=(as_of_date or timezone.localdate()),
                                    as_of_date=as_of_date)
    res = calculate_net_consumption_in_range(medicine, range_info['start_datetime'], range_info['end_datetime'])
    return res['transaction_count']


def get_data_quality_indicator(medicine, as_of_date: Optional[date] = None) -> str:
    """
    Returns:
      'GOOD'              — ≥15 consumption transactions in last 90 days
      'WARNING'           — 5–14 transactions
      'INSUFFICIENT_DATA' — <5 transactions
    """
    count_90d = get_consumption_transaction_count(medicine, 90, as_of_date=as_of_date)
    if count_90d >= DQ_GOOD_MIN_TRANSACTIONS:
        return 'GOOD'
    if count_90d >= DQ_WARNING_MIN_TRANSACTIONS:
        return 'WARNING'
    return 'INSUFFICIENT_DATA'


def detect_stockout_suppression(medicine, days: int = 30) -> Dict[str, Any]:
    """
    Detects if observed consumption in the period was suppressed due to stockouts.
    If current stock is 0 or was 0, observed sales underestimate true patient demand.
    """
    inv = getattr(medicine, 'inventory', None)
    current_stock = inv.current_stock if inv else 0
    
    # Check if any stockout transactions or zero-stock state occurred
    has_zero_stock = current_stock == 0
    zero_stock_txns = StockTransaction.objects.filter(
        medicine=medicine,
        new_stock=0,
        created_at__gte=timezone.now() - timedelta(days=days)
    ).exists()

    is_suppressed = has_zero_stock or zero_stock_txns
    message = None
    if is_suppressed:
        message = (
            f"Stockout Warning: {medicine.medicine_name} experienced zero-stock conditions within the last {days} days. "
            "Observed consumption may underestimate true clinical demand."
        )

    return {
        'is_suppressed': is_suppressed,
        'current_stock': current_stock,
        'suppression_message': message,
    }


def get_consumption_analytics(medicine, as_of_date: Optional[date] = None) -> Dict[str, Any]:
    """
    Full consumption analytics for a single medicine with dynamic dates and stockout checks.
    """
    c_today = get_today_consumption(medicine, as_of_date=as_of_date)
    c7 = get_consumption_for_period(medicine, 7, as_of_date=as_of_date)
    c14 = get_consumption_for_period(medicine, 14, as_of_date=as_of_date)
    c30 = get_consumption_for_period(medicine, 30, as_of_date=as_of_date)
    c90 = get_consumption_for_period(medicine, 90, as_of_date=as_of_date)

    # Last movement date
    last_txn = StockTransaction.objects.filter(
        medicine=medicine,
        transaction_type__in=['SALE', 'DISPENSE'],
    ).order_by('-created_at').first()
    last_movement = last_txn.created_at.date() if last_txn else None

    # Stockout suppression check
    suppression = detect_stockout_suppression(medicine, 30)

    # Dynamic trend (30 days vs previous 30 days)
    prev_range = resolve_date_range(
        'custom',
        date_from=(as_of_date or timezone.localdate()) - timedelta(days=59),
        date_to=(as_of_date or timezone.localdate()) - timedelta(days=30),
        as_of_date=as_of_date
    )
    prev_res = calculate_net_consumption_in_range(medicine, prev_range['start_datetime'], prev_range['end_datetime'])
    c_prev30 = prev_res['net_consumption']

    if c_prev30 > 0:
        trend_pct = round(((c30 - c_prev30) / c_prev30) * 100, 1)
    else:
        trend_pct = 100.0 if c30 > 0 else 0.0

    return {
        'medicine_id': medicine.id,
        'medicine_name': medicine.medicine_name,
        'generic_name': medicine.generic_name,
        'consumed_today': c_today,
        'consumed_7d': c7,
        'consumed_14d': c14,
        'consumed_30d': c30,
        'consumed_90d': c90,
        'daily_avg_7d': round(c7 / 7.0, 2),
        'daily_avg_14d': round(c14 / 14.0, 2),
        'daily_avg_30d': round(c30 / 30.0, 2),
        'daily_avg_90d': round(c90 / 90.0, 2),
        'weekly_avg_30d': round(c30 / 4.0, 2),
        'monthly_avg_90d': round(c90 / 3.0, 2),
        'consumption_trend_pct': trend_pct,
        'txn_count_30d': get_consumption_transaction_count(medicine, 30, as_of_date=as_of_date),
        'txn_count_90d': get_consumption_transaction_count(medicine, 90, as_of_date=as_of_date),
        'last_movement_date': last_movement,
        'data_quality': get_data_quality_indicator(medicine, as_of_date=as_of_date),
        'stockout_suppressed': suppression['is_suppressed'],
        'stockout_message': suppression['suppression_message'],
    }


def get_daily_consumption_trend(medicine, days: int = 30, as_of_date: Optional[date] = None) -> List[Dict[str, Any]]:
    """
    Returns a list of daily consumption objects for charts.
    """
    today = as_of_date or timezone.localdate()
    result = []
    for i in range(days - 1, -1, -1):
        day = today - timedelta(days=i)
        start_dt = timezone.make_aware(datetime.combine(day, datetime.min.time()))
        end_dt = timezone.make_aware(datetime.combine(day, datetime.max.time()))
        net_info = calculate_net_consumption_in_range(medicine, start_dt, end_dt)
        result.append({
            'date': day.strftime('%d %b'),
            'iso_date': day.isoformat(),
            'quantity': net_info['net_consumption']
        })
    return result


def get_fast_movers(top_n: int = 10, days: int = 30, as_of_date: Optional[date] = None) -> List[Dict[str, Any]]:
    """
    Top N medicines by verified net consumption in last `days` days.
    """
    today = as_of_date or timezone.localdate()
    cutoff_dt = timezone.make_aware(datetime.combine(today - timedelta(days=days - 1), datetime.min.time()))

    active_medicines = Medicine.objects.filter(status='ACTIVE').select_related('category', 'inventory')
    med_scores = []

    for med in active_medicines:
        consumed = get_consumption_for_period(med, days, as_of_date=today)
        if consumed > 0:
            med_scores.append({
                'medicine': med,
                'total_consumed': consumed,
                'daily_avg': round(consumed / float(days), 2),
                'category': med.category.name if med.category else '—',
                'current_stock': med.inventory.current_stock if hasattr(med, 'inventory') else 0,
            })

    med_scores.sort(key=lambda x: x['total_consumed'], reverse=True)
    return med_scores[:top_n]


def get_slow_movers(days_threshold: int = 30, max_consumed: int = 5, top_n: int = 10, as_of_date: Optional[date] = None) -> List[Dict[str, Any]]:
    """
    Medicines with stock > 0 that consumed <= max_consumed units in last `days_threshold` days.
    """
    today = as_of_date or timezone.localdate()
    inventories = Inventory.objects.select_related('medicine__category').filter(current_stock__gt=0)
    result = []

    for inv in inventories:
        med = inv.medicine
        consumed = get_consumption_for_period(med, days_threshold, as_of_date=today)
        if consumed <= max_consumed:
            last_txn = StockTransaction.objects.filter(
                medicine=med,
                transaction_type__in=['SALE', 'DISPENSE'],
            ).order_by('-created_at').first()
            days_since = (timezone.now() - last_txn.created_at).days if last_txn else None

            result.append({
                'medicine': med,
                'total_consumed': consumed,
                'current_stock': inv.current_stock,
                'days_since_last_move': days_since,
                'category': med.category.name if med.category else '—',
            })

    result.sort(key=lambda x: x['total_consumed'])
    return result[:top_n]


def get_consumption_dashboard_summary(as_of_date: Optional[date] = None) -> Dict[str, Any]:
    """
    Top-level KPIs for Inventory Intelligence cards with proper deduplication.
    Uses medicine-level iteration to apply the same SALE/DISPENSE dedup logic as the
    main consumption engine instead of a raw aggregate that would double-count.
    """
    today = as_of_date or timezone.localdate()

    # Today
    c_today_total = 0
    # 7d and 30d via per-medicine dedup
    range_7d = resolve_date_range('7d', as_of_date=today)
    range_30d = resolve_date_range('30d', as_of_date=today)
    range_90d = resolve_date_range('90d', as_of_date=today)

    # Fast path: identify medicines with any SALE/DISPENSE in these ranges
    medicine_ids_7d = set(StockTransaction.objects.filter(
        transaction_type__in=['SALE', 'DISPENSE'],
        created_at__gte=range_7d['start_datetime'],
        created_at__lte=range_7d['end_datetime'],
    ).values_list('medicine_id', flat=True).distinct())

    medicine_ids_30d = set(StockTransaction.objects.filter(
        transaction_type__in=['SALE', 'DISPENSE'],
        created_at__gte=range_30d['start_datetime'],
        created_at__lte=range_30d['end_datetime'],
    ).values_list('medicine_id', flat=True).distinct())

    from medicines.models import Medicine
    total_7d = 0
    total_30d = 0
    total_90d = 0
    c_today_total = 0

    # Only iterate medicines that have activity (performance optimization)
    all_active_ids = medicine_ids_7d | medicine_ids_30d
    active_medicines = Medicine.objects.filter(id__in=all_active_ids)

    for med in active_medicines:
        c_today_total += get_today_consumption(med, as_of_date=today)
        if med.id in medicine_ids_7d:
            total_7d += get_consumption_for_period(med, 7, as_of_date=today)
        if med.id in medicine_ids_30d:
            total_30d += get_consumption_for_period(med, 30, as_of_date=today)
        total_90d += get_consumption_for_period(med, 90, as_of_date=today)

    # Trend: medicines with increasing demand (30d vs prev 30d)
    increasing = 0
    decreasing = 0
    stable = 0
    for med in active_medicines:
        c30 = get_consumption_for_period(med, 30, as_of_date=today)
        prev_range = resolve_date_range(
            'custom',
            date_from=today - timedelta(days=59),
            date_to=today - timedelta(days=30),
            as_of_date=today
        )
        prev_res = calculate_net_consumption_in_range(med, prev_range['start_datetime'], prev_range['end_datetime'])
        c_prev30 = prev_res['net_consumption']
        if c_prev30 > 0:
            pct = (c30 - c_prev30) / c_prev30 * 100
            if pct > 10:
                increasing += 1
            elif pct < -10:
                decreasing += 1
            else:
                stable += 1
        elif c30 > 0:
            increasing += 1

    return {
        'consumed_today': c_today_total,
        'total_units_7d': total_7d,
        'total_units_30d': total_30d,
        'total_units_90d': total_90d,
        'daily_avg_7d': round(total_7d / 7.0, 1),
        'daily_avg_30d': round(total_30d / 30.0, 1),
        'daily_avg_90d': round(total_90d / 90.0, 1),
        'active_movers_30d': len(medicine_ids_30d),
        'increasing_demand': increasing,
        'decreasing_demand': decreasing,
        'stable_demand': stable,
        'as_of_date': today,
    }


def rebuild_daily_summary(medicine, date_from: Optional[date] = None, date_to: Optional[date] = None) -> int:
    """
    Rebuilds DailyConsumptionSummary records for a medicine across a date range.
    Idempotent: uses update_or_create — safe to run multiple times.
    Does NOT modify any stock balances.
    Returns the count of days processed.
    """
    from inventory.models import DailyConsumptionSummary
    from datetime import timedelta, datetime

    today = timezone.localdate()
    start_d = date_from or (today - timedelta(days=89))
    end_d = date_to or today

    count = 0
    current = start_d
    while current <= end_d:
        start_dt = timezone.make_aware(datetime.combine(current, datetime.min.time()))
        end_dt = timezone.make_aware(datetime.combine(current, datetime.max.time()))

        result = calculate_net_consumption_in_range(medicine, start_dt, end_dt)

        # Raw per-type breakdown for auditability
        tx_qs = _get_outbound_transactions_query(medicine, start_dt, end_dt)
        sale_qty = tx_qs.filter(transaction_type='SALE').aggregate(s=Sum('quantity'))['s'] or 0
        disp_qty = tx_qs.filter(transaction_type='DISPENSE').aggregate(s=Sum('quantity'))['s'] or 0
        ret_qty = tx_qs.filter(transaction_type='RETURN').aggregate(s=Sum('quantity'))['s'] or 0

        DailyConsumptionSummary.objects.update_or_create(
            medicine=medicine,
            date=current,
            defaults={
                'sale_qty': sale_qty,
                'dispense_qty': disp_qty,
                'return_qty': ret_qty,
                'net_consumption': result['net_consumption'],
                'transaction_count': result['transaction_count'],
                'data_source': 'TRANSACTION',
            }
        )
        current += timedelta(days=1)
        count += 1

    return count


def get_period_consumption_breakdown(medicine, as_of_date: Optional[date] = None) -> Dict[str, Any]:
    """
    Returns consumption across multiple calendar periods, clearly labelled.
    Distinguishes rolling-30d from calendar-month-to-date.
    """
    today = as_of_date or timezone.localdate()

    # Rolling windows
    c7 = get_consumption_for_period(medicine, 7, as_of_date=today)
    c30 = get_consumption_for_period(medicine, 30, as_of_date=today)
    c90 = get_consumption_for_period(medicine, 90, as_of_date=today)

    # Calendar this-month (1st to today)
    mtd_range = resolve_date_range('this_month', as_of_date=today)
    mtd_res = calculate_net_consumption_in_range(medicine, mtd_range['start_datetime'], mtd_range['end_datetime'])
    c_month_to_date = mtd_res['net_consumption']

    # Last complete calendar month
    lm_range = resolve_date_range('last_month', as_of_date=today)
    lm_res = calculate_net_consumption_in_range(medicine, lm_range['start_datetime'], lm_range['end_datetime'])
    c_last_month = lm_res['net_consumption']
    last_month_days = lm_range['days_count']

    # This quarter
    q_range = resolve_date_range('this_quarter', as_of_date=today)
    q_res = calculate_net_consumption_in_range(medicine, q_range['start_datetime'], q_range['end_datetime'])
    c_quarter = q_res['net_consumption']
    quarter_days = q_range['days_count']

    return {
        'rolling_7d': c7,
        'rolling_30d': c30,
        'rolling_90d': c90,
        'month_to_date': c_month_to_date,
        'mtd_label': mtd_range['period_label'],
        'last_month': c_last_month,
        'last_month_label': lm_range['period_label'],
        'last_month_daily_avg': round(c_last_month / last_month_days, 2) if last_month_days else 0,
        'this_quarter': c_quarter,
        'quarter_label': q_range['period_label'],
        'quarter_daily_avg': round(c_quarter / quarter_days, 2) if quarter_days else 0,
        'avg_daily_7d': round(c7 / 7.0, 2),
        'avg_daily_30d': round(c30 / 30.0, 2),
        'avg_daily_90d': round(c90 / 90.0, 2),
        'avg_weekly_30d': round(c30 / 4.0, 2),
        'avg_monthly_90d': round(c90 / 3.0, 2),
    }


def get_stockout_risk(medicine, as_of_date: Optional[date] = None) -> Dict[str, Any]:
    """
    Calculates stockout risk and estimated days-until-stockout.
    Uses actual usable stock (non-expired batches), daily consumption rate, and pending PO quantities.

    Returns:
        risk_level: CRITICAL / HIGH / MEDIUM / LOW / NO_DATA
        estimated_stockout_date: date or None
        days_until_stockout: int or None
        daily_rate: float
        usable_stock: int
        incoming_stock: int
        effective_stock: int
        risk_factors: list[str]
        recommendation: str
    """
    from ai_engine.intelligence_service import _get_usable_stock, _get_open_po_qty

    today = as_of_date or timezone.localdate()
    usable_stock = _get_usable_stock(medicine, as_of_date=today)

    inv = getattr(medicine, 'inventory', None)
    safety_stock = inv.safety_stock if inv else medicine.safety_stock
    reorder_level = inv.reorder_level if inv else medicine.reorder_level

    # Consumption rate from most reliable window
    c30 = get_consumption_for_period(medicine, 30, as_of_date=today)
    c7 = get_consumption_for_period(medicine, 7, as_of_date=today)
    txn_count = get_consumption_transaction_count(medicine, 30, as_of_date=today)

    # Use 30d rate as primary; fall back to 7d if more recent data differs significantly
    if txn_count >= 5 and c30 > 0:
        daily_rate = round(c30 / 30.0, 2)
    elif c7 > 0:
        daily_rate = round(c7 / 7.0, 2)
    else:
        # No consumption data — cannot estimate reliably
        return {
            'risk_level': 'NO_DATA',
            'estimated_stockout_date': None,
            'days_until_stockout': None,
            'daily_rate': 0.0,
            'usable_stock': usable_stock,
            'incoming_stock': 0,
            'effective_stock': usable_stock,
            'risk_factors': ['No recent consumption data available'],
            'recommendation': 'Insufficient consumption history to estimate stockout risk.',
            'stockout_suppressed': usable_stock == 0,
        }

    # Pending PO stock
    incoming_stock = _get_open_po_qty(medicine)
    effective_stock = usable_stock + incoming_stock

    risk_factors = []
    if usable_stock == 0:
        risk_factors.append('Currently out of stock')
    if usable_stock <= safety_stock:
        risk_factors.append(f'Below safety stock ({safety_stock} units)')
    if usable_stock <= reorder_level:
        risk_factors.append(f'At or below reorder level ({reorder_level} units)')
    if incoming_stock == 0:
        risk_factors.append('No pending purchase orders')
    if txn_count < 5:
        risk_factors.append('Low transaction count — estimate may be unreliable')

    if daily_rate > 0:
        days_until_stockout = int(effective_stock / daily_rate)
        estimated_stockout_date = today + timedelta(days=days_until_stockout)
    else:
        days_until_stockout = None
        estimated_stockout_date = None

    # Lead time for risk classification
    supplier_lead_days = 7  # Default fallback
    try:
        from suppliers.models import SupplierMedicine
        sm = SupplierMedicine.objects.filter(medicine=medicine, is_preferred=True).first()
        if sm and sm.supplier.average_delivery_days:
            supplier_lead_days = sm.supplier.average_delivery_days
    except Exception:
        pass

    if usable_stock == 0:
        risk_level = 'CRITICAL'
        recommendation = 'Medicine is out of stock. Raise emergency purchase order immediately.'
    elif days_until_stockout is not None and days_until_stockout <= supplier_lead_days:
        risk_level = 'CRITICAL'
        recommendation = f'Stockout expected within {days_until_stockout} days — before supplier can deliver. Order immediately.'
    elif days_until_stockout is not None and days_until_stockout <= supplier_lead_days * 2:
        risk_level = 'HIGH'
        recommendation = f'Stockout risk within {days_until_stockout} days. Review and place reorder soon.'
    elif usable_stock <= reorder_level:
        risk_level = 'MEDIUM'
        recommendation = 'Stock at or below reorder level. Consider placing an order.'
    else:
        risk_level = 'LOW'
        recommendation = 'Stock levels appear adequate based on current consumption rate.'

    return {
        'risk_level': risk_level,
        'estimated_stockout_date': estimated_stockout_date,
        'days_until_stockout': days_until_stockout,
        'daily_rate': daily_rate,
        'usable_stock': usable_stock,
        'incoming_stock': incoming_stock,
        'effective_stock': effective_stock,
        'risk_factors': risk_factors,
        'recommendation': recommendation,
        'stockout_suppressed': (usable_stock == 0 and txn_count < 5),
        'supplier_lead_days': supplier_lead_days,
        'data_quality': get_data_quality_indicator(medicine, as_of_date=today),
    }


def refresh_consumption_cache(medicine) -> Any:
    """
    Recalculates and caches consumption metrics for a single medicine.
    Centralized method called automatically on SALE, DISPENSE, RETURN, and historical import.
    """
    from inventory.models import ConsumptionCache

    analytics = get_consumption_analytics(medicine)
    cache, _ = ConsumptionCache.objects.update_or_create(
        medicine=medicine,
        defaults={
            'consumed_7d': analytics['consumed_7d'],
            'consumed_14d': analytics['consumed_14d'],
            'consumed_30d': analytics['consumed_30d'],
            'consumed_90d': analytics['consumed_90d'],
            'daily_avg_30d': Decimal(str(analytics['daily_avg_30d'])),
            'data_quality': analytics['data_quality'],
            'last_movement_date': analytics['last_movement_date'],
        }
    )
    return cache


def evaluate_inventory_optimization(medicine, as_of_date: Optional[date] = None) -> Dict[str, Any]:
    """
    Comprehensive Inventory Automation & Optimization Engine.
    Evaluates:
      Current usable stock + Average consumption + Forecast demand + Reorder level +
      Safety stock + Supplier lead time + Pending purchase orders + Expiry risk +
      VED criticality + Supplier availability.

    Determines:
      - Optimization status:
        URGENT REORDER | HIGH STOCKOUT RISK | REORDER SOON | LOW STOCK | EXPIRY RISK | OVERSTOCK | NORMAL
      - Recommended order quantity factoring in pending POs (anti-duplication)
      - Recommended best supplier and alternative options
      - Dynamic, explainable reason string showing WHY the status was assigned.
    """
    from ai_engine.intelligence_service import (
        _get_usable_stock, _get_open_po_qty, get_demand_forecast_multi_horizon,
        get_ved_classification
    )
    from suppliers.intelligence_service import get_supplier_rankings, get_best_supplier_for_medicine

    today = as_of_date or timezone.localdate()
    inv = getattr(medicine, 'inventory', None)

    current_stock = inv.current_stock if inv else 0
    usable_stock = _get_usable_stock(medicine, as_of_date=today)
    reorder_level = inv.reorder_level if inv else medicine.reorder_level
    safety_stock = inv.safety_stock if inv else medicine.safety_stock
    minimum_stock = inv.minimum_stock if inv else medicine.minimum_stock
    maximum_stock = inv.maximum_stock if inv else medicine.maximum_stock

    # Consumption velocities
    c_today = get_today_consumption(medicine, as_of_date=today)
    c7 = get_consumption_for_period(medicine, 7, as_of_date=today)
    c30 = get_consumption_for_period(medicine, 30, as_of_date=today)
    c90 = get_consumption_for_period(medicine, 90, as_of_date=today)
    txn_count_30d = get_consumption_transaction_count(medicine, 30, as_of_date=today)

    daily_avg = round(c30 / 30.0, 2) if c30 > 0 else (round(c7 / 7.0, 2) if c7 > 0 else 0.0)
    weekly_avg = round(daily_avg * 7.0, 1)
    monthly_avg = round(daily_avg * 30.0, 1)

    # Demand Trend (last 30d vs previous 30d)
    prev_range = resolve_date_range(
        'custom',
        date_from=today - timedelta(days=59),
        date_to=today - timedelta(days=30),
        as_of_date=today
    )
    prev_res = calculate_net_consumption_in_range(medicine, prev_range['start_datetime'], prev_range['end_datetime'])
    c_prev30 = prev_res['net_consumption']

    if txn_count_30d < 5 and c_prev30 == 0 and c30 == 0:
        demand_trend = 'INSUFFICIENT_DATA'
        trend_pct = 0.0
    elif c_prev30 > 0:
        trend_pct = round(((c30 - c_prev30) / c_prev30) * 100, 1)
        if trend_pct > 10:
            demand_trend = 'INCREASING'
        elif trend_pct < -10:
            demand_trend = 'DECREASING'
        else:
            demand_trend = 'STABLE'
    else:
        trend_pct = 100.0 if c30 > 0 else 0.0
        demand_trend = 'INCREASING' if c30 > 0 else 'STABLE'

    # Multi-horizon forecast
    forecast_data = get_demand_forecast_multi_horizon(medicine, cutoff_date=today)
    forecast_7d = forecast_data.get('forecast_7d', max(0, round(daily_avg * 7)))
    forecast_30d = forecast_data.get('forecast_30d', max(0, round(daily_avg * 30)))
    forecast_60d = forecast_data.get('forecast_60d', max(0, round(daily_avg * 60)))
    forecast_90d = forecast_data.get('forecast_90d', max(0, round(daily_avg * 90)))
    is_cold_start = forecast_data.get('is_cold_start', False)
    forecast_model = forecast_data.get('model_type', 'RULE_BASED')

    # Supplier Intelligence & Lead Time
    rankings = get_supplier_rankings(medicine)
    best_sm = get_best_supplier_for_medicine(medicine)
    recommended_supplier = best_sm.supplier if best_sm else None
    supplier_lead_days = best_sm.delivery_days if best_sm else (
        recommended_supplier.average_delivery_days if recommended_supplier and recommended_supplier.average_delivery_days else 7
    )
    unit_price = best_sm.unit_price if best_sm else Decimal('0.00')

    # Pending Purchase Orders (Anti-duplication)
    open_po_qty = _get_open_po_qty(medicine)
    effective_stock = usable_stock + open_po_qty

    # Lead-Time Demand Calculation
    lead_time_demand = round(daily_avg * supplier_lead_days, 1)
    projected_stock_at_lead_time = round(usable_stock + open_po_qty - lead_time_demand, 1)

    # Days of Stock Remaining & Run-out Date
    if daily_avg > 0:
        days_until_stockout = int(effective_stock / daily_avg)
        estimated_stockout_date = today + timedelta(days=days_until_stockout)
        physical_dos = int(usable_stock / daily_avg)
    else:
        days_until_stockout = None
        estimated_stockout_date = None
        physical_dos = 9999 if usable_stock > 0 else 0

    # Expiry Risk Check (active batches expiring within 90 days with unabsorbed stock)
    near_expiry_batches = medicine.batches.filter(
        status__in=['ACTIVE', 'NEAR_EXPIRY'],
        quantity_available__gt=0,
        expiry_date__gt=today,
        expiry_date__lte=today + timedelta(days=90),
    ).order_by('expiry_date')
    
    expiry_risk_qty = 0
    for b in near_expiry_batches:
        d_left = (b.expiry_date - today).days
        exp_absorbed = round(daily_avg * d_left)
        if b.quantity_available > exp_absorbed:
            expiry_risk_qty += (b.quantity_available - exp_absorbed)

    # VED Clinical Criticality
    ved_category = get_ved_classification(medicine)

    # ─────────────────────────────────────────────────────────────────────────
    # DETERMINING OPTIMIZATION STATUS & REASON
    # ─────────────────────────────────────────────────────────────────────────
    reasons = []
    reasons.append(f"Current stock is {usable_stock} units.")
    reasons.append(f"Average daily consumption is {daily_avg} units.")
    reasons.append(f"Supplier lead time is {supplier_lead_days} days.")
    reasons.append(f"Expected lead-time demand is approximately {lead_time_demand} units.")

    if usable_stock == 0:
        optimization_status = 'URGENT REORDER'
        reasons.append("Medicine is currently out of stock.")
        if open_po_qty > 0:
            reasons.append(f"An open purchase order for {open_po_qty} units is in progress but not yet received.")
        else:
            reasons.append("No incoming purchase orders exist. Immediate replenishment required.")
        needs_reorder = (effective_stock <= reorder_level)
    elif projected_stock_at_lead_time < 0 or (physical_dos <= supplier_lead_days and open_po_qty == 0):
        optimization_status = 'URGENT REORDER'
        reasons.append(f"Current stock is below expected lead-time demand. Stockout is anticipated within {physical_dos} days, before supplier can deliver.")
        needs_reorder = True
    elif days_until_stockout is not None and days_until_stockout <= supplier_lead_days * 2:
        optimization_status = 'HIGH STOCKOUT RISK'
        reasons.append(f"Projected stockout within {days_until_stockout} days ({supplier_lead_days * 2}d buffer threshold).")
        needs_reorder = (effective_stock <= reorder_level or (effective_stock - forecast_30d) < safety_stock)
    elif expiry_risk_qty > 0 and usable_stock > reorder_level:
        optimization_status = 'EXPIRY RISK'
        reasons.append(f"{expiry_risk_qty} units in near-expiry batches are at risk of expiring before projected consumption. Priority dispensing or return recommended.")
        needs_reorder = False
    elif usable_stock > maximum_stock or (physical_dos > 180 and c30 > 0):
        optimization_status = 'OVERSTOCK'
        reasons.append(f"Current stock exceeds maximum stock policy ({maximum_stock} units). Days of stock remaining: {physical_dos} days. Hold further purchases.")
        needs_reorder = False
    elif effective_stock <= reorder_level or (effective_stock - forecast_30d) < safety_stock:
        if open_po_qty > 0 and effective_stock > reorder_level:
            optimization_status = 'LOW STOCK'
            reasons.append(f"Physical stock is low ({usable_stock} units), but an incoming open PO of {open_po_qty} units covers the required threshold.")
            needs_reorder = False
        else:
            optimization_status = 'REORDER SOON'
            reasons.append(f"Stock ({effective_stock} units available/incoming) is at or below reorder level ({reorder_level} units) and safety stock ({safety_stock} units).")
            needs_reorder = True
    elif usable_stock <= reorder_level:
        optimization_status = 'LOW STOCK'
        if open_po_qty > 0:
            reasons.append(f"Current stock is below reorder level, but pending PO of {open_po_qty} units is already scheduled.")
            needs_reorder = False
        else:
            reasons.append("Current stock is below reorder level.")
            needs_reorder = True
    else:
        optimization_status = 'NORMAL'
        reasons.append(f"Current stock is within healthy range (Reorder level: {reorder_level}, Max: {maximum_stock}).")
        needs_reorder = False

    # Calculate recommended quantity considering open POs (anti-duplication)
    if needs_reorder:
        net_shortfall = forecast_30d + safety_stock - effective_stock
        remaining_capacity = max(0, maximum_stock - effective_stock)
        rec_qty = min(remaining_capacity, max(0, net_shortfall))
        if rec_qty > 0:
            reasons.append(f"Recommended replenishment quantity: {rec_qty} units.")
        else:
            rec_qty = 0
            needs_reorder = False
    else:
        rec_qty = 0

    return {
        'medicine': medicine,
        'current_stock': current_stock,
        'usable_stock': usable_stock,
        'consumed_today': c_today,
        'consumed_7d': c7,
        'consumed_30d': c30,
        'consumed_90d': c90,
        'daily_avg': daily_avg,
        'weekly_avg': weekly_avg,
        'monthly_avg': monthly_avg,
        'demand_trend': demand_trend,
        'trend_pct': trend_pct,
        'forecast_7d': forecast_7d,
        'forecast_30d': forecast_30d,
        'forecast_60d': forecast_60d,
        'forecast_90d': forecast_90d,
        'forecast_model': forecast_model,
        'is_cold_start': is_cold_start,
        'reorder_level': reorder_level,
        'safety_stock': safety_stock,
        'minimum_stock': minimum_stock,
        'maximum_stock': maximum_stock,
        'supplier_lead_days': supplier_lead_days,
        'lead_time_demand': lead_time_demand,
        'open_po_qty': open_po_qty,
        'effective_stock': effective_stock,
        'projected_stock_at_lead_time': projected_stock_at_lead_time,
        'days_until_stockout': days_until_stockout,
        'estimated_stockout_date': estimated_stockout_date,
        'physical_dos': physical_dos if physical_dos != 9999 else None,
        'expiry_risk_qty': expiry_risk_qty,
        'ved_category': ved_category,
        'optimization_status': optimization_status,
        'needs_reorder': needs_reorder,
        'recommended_reorder_qty': rec_qty,
        'recommended_supplier': recommended_supplier,
        'estimated_unit_price': unit_price,
        'estimated_total_cost': round(Decimal(str(rec_qty)) * unit_price, 2) if rec_qty > 0 else Decimal('0.00'),
        'alternative_suppliers': rankings[1:4] if len(rankings) > 1 else [],
        'reason': " ".join(reasons),
    }

