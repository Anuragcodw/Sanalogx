"""
suppliers/intelligence_service.py
MediVault — Supplier Intelligence & Price Intelligence Engine

Provides:
  - Weighted supplier rankings per medicine
  - Price intelligence (min/max/avg/trend) from SupplierPriceHistory
  - Price opportunity detection
  - Supplier performance scoring
"""

from decimal import Decimal
from datetime import timedelta

from django.db.models import Avg, Min, Max, Sum, Count, Q
from django.utils import timezone

from suppliers.models import Supplier, SupplierMedicine, SupplierPriceHistory
from medicines.models import Medicine


# ─── Supplier Rankings ────────────────────────────────────────────────────────

def get_supplier_rankings(medicine) -> list:
    """
    Returns a ranked list of suppliers for a specific medicine,
    ordered by composite score DESC (best first).

    Each entry:
      {
        'supplier_medicine': SupplierMedicine,
        'supplier': Supplier,
        'unit_price': Decimal,
        'delivery_days': int,
        'composite_score': float,
        'price_rank': int,
        'is_preferred': bool,
        'is_available': bool,
      }
    """
    supplier_medicines = SupplierMedicine.objects.filter(
        medicine=medicine,
        supplier__status='ACTIVE',
    ).select_related('supplier').order_by('unit_price')

    if not supplier_medicines.exists():
        return []

    all_prices = [float(sm.unit_price) for sm in supplier_medicines]

    ranked = []
    for sm in supplier_medicines:
        score = sm.calculate_composite_score(all_prices_for_medicine=all_prices)
        ranked.append({
            'supplier_medicine': sm,
            'supplier': sm.supplier,
            'unit_price': sm.unit_price,
            'delivery_days': sm.delivery_days,
            'composite_score': score,
            'is_preferred': sm.is_preferred,
            'is_available': sm.is_available,
            'minimum_order_quantity': sm.minimum_order_quantity,
        })

    # Sort by composite score desc
    ranked.sort(key=lambda x: x['composite_score'], reverse=True)

    # Add price rank (ascending by price, 1 = cheapest)
    price_ranked = sorted(ranked, key=lambda x: x['unit_price'])
    for i, item in enumerate(price_ranked):
        item['price_rank'] = i + 1

    return ranked


def get_best_supplier_for_medicine(medicine):
    """
    Returns the single best SupplierMedicine for a medicine based on composite score.
    Returns None if no suppliers found.
    """
    rankings = get_supplier_rankings(medicine)
    if not rankings:
        return None
    # Preferred suppliers get a boost
    preferred = [r for r in rankings if r['is_preferred'] and r['is_available']]
    if preferred:
        return preferred[0]['supplier_medicine']
    available = [r for r in rankings if r['is_available']]
    return available[0]['supplier_medicine'] if available else rankings[0]['supplier_medicine']


# ─── Price Intelligence ───────────────────────────────────────────────────────

def get_price_intelligence(medicine) -> dict:
    """
    Analyzes price history for a medicine across all suppliers.

    Returns:
      {
        'medicine_id', 'medicine_name',
        'current_min_price', 'current_max_price', 'current_avg_price',
        'historical_min', 'historical_max',
        'price_trend': 'INCREASING' | 'DECREASING' | 'STABLE',
        'trend_pct': float,
        'recent_changes': list of SupplierPriceHistory,
        'supplier_count': int,
      }
    """
    # Current prices from SupplierMedicine
    sm_qs = SupplierMedicine.objects.filter(
        medicine=medicine,
        supplier__status='ACTIVE',
    )
    current_agg = sm_qs.aggregate(
        min_p=Min('unit_price'),
        max_p=Max('unit_price'),
        avg_p=Avg('unit_price'),
        count=Count('id'),
    )

    # Historical price changes in last 90 days
    cutoff = timezone.now() - timedelta(days=90)
    history_qs = SupplierPriceHistory.objects.filter(
        medicine=medicine,
        changed_at__gte=cutoff,
    ).select_related('supplier').order_by('-changed_at')

    historical_agg = SupplierPriceHistory.objects.filter(
        medicine=medicine,
    ).aggregate(
        h_min=Min('new_price'),
        h_max=Max('new_price'),
    )

    # Trend analysis: compare avg price_change_pct in last 30 days
    trend_data = SupplierPriceHistory.objects.filter(
        medicine=medicine,
        changed_at__gte=timezone.now() - timedelta(days=30),
    ).aggregate(avg_chg=Avg('price_change_pct'))
    avg_change = float(trend_data['avg_chg'] or 0)

    if avg_change > 2.0:
        trend = 'INCREASING'
    elif avg_change < -2.0:
        trend = 'DECREASING'
    else:
        trend = 'STABLE'

    return {
        'medicine_id': medicine.id,
        'medicine_name': medicine.medicine_name,
        'current_min_price': current_agg['min_p'],
        'current_max_price': current_agg['max_p'],
        'current_avg_price': round(current_agg['avg_p'], 2) if current_agg['avg_p'] else None,
        'historical_min': historical_agg['h_min'],
        'historical_max': historical_agg['h_max'],
        'price_trend': trend,
        'trend_pct': round(avg_change, 2),
        'recent_changes': list(history_qs[:10]),
        'supplier_count': current_agg['count'],
    }


def get_price_opportunities(max_results: int = 20) -> list:
    """
    Identifies medicines where a significant price drop opportunity exists.
    Logic: medicines where at least one supplier's price dropped > 5% in last 30 days.

    Each entry:
      {'medicine', 'old_price', 'new_price', 'drop_pct', 'supplier'}
    """
    cutoff = timezone.now() - timedelta(days=30)
    drops = SupplierPriceHistory.objects.filter(
        changed_at__gte=cutoff,
        price_change_pct__lt=-5,       # > 5% price drop
    ).select_related('medicine', 'supplier').order_by('price_change_pct')[:max_results]

    result = []
    for ph in drops:
        result.append({
            'medicine': ph.medicine,
            'supplier': ph.supplier,
            'old_price': ph.old_price,
            'new_price': ph.new_price,
            'drop_pct': float(ph.price_change_pct),
            'changed_at': ph.changed_at,
        })
    return result


def get_supplier_performance_summary(supplier) -> dict:
    """
    Per-supplier performance summary with:
      - total medicines supplied, orders, delivery rate, fill rate, rating
      - price change trend (last 90 days)
      - recommended as best supplier for N medicines
    """
    from purchases.models import PurchaseOrder

    medicines_supplied = SupplierMedicine.objects.filter(supplier=supplier).count()
    total_pos = PurchaseOrder.objects.filter(supplier=supplier).count()
    completed_pos = PurchaseOrder.objects.filter(supplier=supplier, status='COMPLETED').count()
    pending_pos = PurchaseOrder.objects.filter(
        supplier=supplier,
        status__in=['DRAFT', 'SUBMITTED', 'APPROVED', 'ORDERED', 'PARTIAL_RECEIPT'],
    ).count()

    # Price changes last 90 days
    cutoff = timezone.now() - timedelta(days=90)
    price_changes = SupplierPriceHistory.objects.filter(
        supplier=supplier,
        changed_at__gte=cutoff,
    ).count()

    return {
        'supplier': supplier,
        'medicines_supplied': medicines_supplied,
        'total_pos': total_pos,
        'completed_pos': completed_pos,
        'pending_pos': pending_pos,
        'on_time_delivery_rate': float(supplier.on_time_delivery_rate),
        'fill_rate': float(supplier.fill_rate),
        'rating': float(supplier.rating),
        'composite_score': supplier.supplier_score,
        'price_changes_90d': price_changes,
    }


def get_all_supplier_rankings() -> list:
    """
    Returns all active suppliers ranked by their composite score.
    """
    suppliers = Supplier.objects.filter(status='ACTIVE').order_by('-rating')
    result = []
    for s in suppliers:
        result.append({
            'supplier': s,
            'composite_score': s.supplier_score,
            'medicines_count': SupplierMedicine.objects.filter(supplier=s).count(),
        })
    result.sort(key=lambda x: x['composite_score'], reverse=True)
    return result
