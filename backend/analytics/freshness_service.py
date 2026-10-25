"""
analytics/freshness_service.py
MediVault — Enterprise Data Freshness, Quality & Coverage Engine

Guarantees:
- Inspects actual database records dynamically (sales, dispensing, stock movements, purchases, imports).
- Calculates data coverage boundaries (earliest and latest transaction dates).
- Never marks data as FRESH simply because a page was opened.
- Generates transparent, clinical-grade warnings when data is stale or when viewing periods
  with incomplete or missing records.
- Labels datasets as: FRESH, PARTIALLY UPDATED, STALE, INSUFFICIENT DATA.
"""

from datetime import date, datetime, timedelta
from typing import Dict, Any, Optional
from django.db.models import Max, Min, Count
from django.utils import timezone

from sales.models import Sale
from dispensing.models import DispensingRecord
from inventory.models import StockTransaction, ConsumptionCache
from purchases.models import PurchaseOrder, GoodsReceipt
from ai_engine.models import AIRecommendation


class FreshnessStatus:
    FRESH = 'FRESH'
    PARTIALLY_UPDATED = 'PARTIALLY_UPDATED'
    STALE = 'STALE'
    INSUFFICIENT_DATA = 'INSUFFICIENT_DATA'


def get_data_freshness_report(as_of_date: Optional[date] = None) -> Dict[str, Any]:
    """
    Evaluates system-wide data freshness and coverage against current application date.

    Returns:
        dict:
            - 'status': FreshnessStatus string
            - 'status_badge_class': CSS class string
            - 'latest_data_timestamp': datetime of newest recorded clinical activity
            - 'latest_transaction_date': date of newest recorded clinical activity
            - 'coverage_start_date': date of earliest recorded transaction
            - 'coverage_end_date': date of latest recorded transaction
            - 'days_stale': int days between latest transaction and current date
            - 'is_stale': bool True if days_stale > 2
            - 'warning_message': Optional human-readable warning message
            - 'latest_sale_date': date
            - 'latest_dispensing_date': date
            - 'latest_stock_txn_date': date
            - 'latest_purchase_date': date
            - 'latest_import_date': date
            - 'last_forecast_timestamp': datetime
            - 'last_cache_refresh': datetime
            - 'total_tx_count': int
    """
    today = as_of_date if as_of_date is not None else timezone.localdate()

    # 1. Inspect Stream Timestamps
    latest_sale_dt = Sale.objects.filter(
        status__in=['COMPLETED', 'PARTIAL_RETURN']
    ).aggregate(m=Max('sale_date'))['m']

    latest_disp_dt = DispensingRecord.objects.filter(
        status__in=['COMPLETED', 'CONFIRMED']
    ).aggregate(m=Max('created_at'))['m']

    latest_st_dt = StockTransaction.objects.aggregate(m=Max('created_at'))['m']
    earliest_st_dt = StockTransaction.objects.aggregate(m=Min('created_at'))['m']
    total_st_count = StockTransaction.objects.count()

    latest_gr_date = GoodsReceipt.objects.aggregate(m=Max('receipt_date'))['m']
    latest_po_dt = PurchaseOrder.objects.aggregate(m=Max('created_at'))['m']

    latest_import_dt = StockTransaction.objects.filter(
        transaction_type='HISTORICAL_IMPORT'
    ).aggregate(m=Max('created_at'))['m']

    last_forecast_dt = AIRecommendation.objects.aggregate(m=Max('generated_at'))['m']
    last_cache_dt = ConsumptionCache.objects.aggregate(m=Max('updated_at'))['m']

    # 2. Determine Latest Overall Clinical Activity Timestamp
    all_timestamps = [dt for dt in [latest_sale_dt, latest_disp_dt, latest_st_dt, latest_po_dt] if dt is not None]
    
    # If GoodsReceipt date exists, convert to tz-aware datetime for comparison
    if latest_gr_date:
        all_timestamps.append(timezone.make_aware(datetime.combine(latest_gr_date, datetime.min.time())))

    latest_data_dt = max(all_timestamps) if all_timestamps else None
    latest_tx_date = latest_data_dt.date() if latest_data_dt else None
    coverage_start = earliest_st_dt.date() if earliest_st_dt else None

    # 3. Freshness Classification
    if total_st_count < 5 or latest_tx_date is None:
        status = FreshnessStatus.INSUFFICIENT_DATA
        badge_class = 'secondary'
        days_stale = 0
        warning = "Insufficient transaction history (< 5 transactions) in database. Analytics rely on baseline estimates."
    else:
        days_stale = max(0, (today - latest_tx_date).days)
        if days_stale <= 1:
            status = FreshnessStatus.FRESH
            badge_class = 'success'
            warning = None
        elif days_stale <= 7:
            status = FreshnessStatus.PARTIALLY_UPDATED
            badge_class = 'warning'
            warning = (
                f"Latest transaction data recorded on {latest_tx_date.strftime('%d %b %Y')} "
                f"({days_stale} days ago). Recent consumption figures may be partially updated."
            )
        else:
            status = FreshnessStatus.STALE
            badge_class = 'danger'
            warning = (
                f"Data Freshness Warning: Latest transaction data available through {latest_tx_date.strftime('%d %b %Y')}. "
                f"Current period ({today.strftime('%B %Y')}) usage may be incomplete."
            )

    # Build structured data_sources list for UI and tests
    data_sources = []

    def _source_status(latest_dt, label):
        if latest_dt is None:
            return {'name': label, 'status': 'NO_DATA', 'latest': None}
        dt = latest_dt if isinstance(latest_dt, date) else (latest_dt.date() if latest_dt else None)
        if dt is None:
            return {'name': label, 'status': 'NO_DATA', 'latest': None}
        days = (today - dt).days
        if days <= 1:
            src_status = FreshnessStatus.FRESH
        elif days <= 7:
            src_status = FreshnessStatus.PARTIALLY_UPDATED
        else:
            src_status = FreshnessStatus.STALE
        return {'name': label, 'status': src_status, 'latest': dt, 'days_ago': days}

    sale_date = latest_sale_dt.date() if latest_sale_dt else None
    disp_date = latest_disp_dt.date() if latest_disp_dt else None
    st_date = latest_st_dt.date() if latest_st_dt else None
    po_date = latest_po_dt.date() if latest_po_dt else None

    data_sources.append(_source_status(sale_date, 'Sales'))
    data_sources.append(_source_status(disp_date, 'Dispensing'))
    data_sources.append(_source_status(st_date, 'Stock Transactions'))
    data_sources.append(_source_status(latest_gr_date, 'Goods Receipts'))
    data_sources.append(_source_status(po_date, 'Purchase Orders'))

    return {
        'status': status,
        'overall_status': status,          # alias for test & UI compatibility
        'data_sources': data_sources,      # structured per-source breakdown
        'status_badge_class': badge_class,
        'latest_data_timestamp': latest_data_dt,
        'latest_transaction_date': latest_tx_date,
        'coverage_start_date': coverage_start,
        'coverage_end_date': latest_tx_date,
        'days_stale': days_stale,
        'is_stale': days_stale > 1,
        'warning_message': warning,
        'latest_sale_date': sale_date,
        'latest_dispensing_date': disp_date,
        'latest_stock_txn_date': st_date,
        'latest_purchase_date': latest_gr_date or po_date,
        'latest_import_date': latest_import_dt.date() if latest_import_dt else None,
        'last_forecast_timestamp': last_forecast_dt,
        'last_cache_refresh': last_cache_dt,
        'total_tx_count': total_st_count,
        'as_of_date': today,
        'generated_at': timezone.now(),
    }

