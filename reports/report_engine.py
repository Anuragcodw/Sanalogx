"""
reports/report_engine.py
Phase 7 — Report Data Extraction Engine

Pure data extraction functions for all MediVault report types.
All functions return: {'data': [...], 'summary': {...}, 'columns': [...]}
All queries respect date filters and user permissions.
"""

from decimal import Decimal
from datetime import timedelta
from django.db.models import Sum, Count, Avg, F, Q
from django.utils import timezone

from medicines.models import Medicine, MedicineBatch
from inventory.models import Inventory, StockTransaction
from sales.models import Sale, SaleItem, SaleReturn
from purchases.models import PurchaseOrder, GoodsReceipt
from suppliers.models import Supplier, SupplierMedicine, SupplierPriceHistory
from dispensing.models import MedicineRequest, DispensingInvoice, DispensingRecord, ControlledMedicineRegister


def _date_range(date_from, date_to):
    """Convert dates to timezone-aware datetimes for queryset filtering."""
    import datetime
    if date_from:
        start = timezone.make_aware(datetime.datetime.combine(date_from, datetime.time.min))
    else:
        start = None
    if date_to:
        end = timezone.make_aware(datetime.datetime.combine(date_to, datetime.time.max))
    else:
        end = None
    return start, end


def get_inventory_report_data(date_from=None, date_to=None, filters=None):
    """Inventory snapshot: batch-wise valuation and stock status for active medicines."""
    filters = filters or {}
    qs = Inventory.objects.select_related('medicine', 'medicine__category').all()

    if filters.get('category_id'):
        qs = qs.filter(medicine__category_id=filters['category_id'])
    if filters.get('medicine_id'):
        qs = qs.filter(medicine_id=filters['medicine_id'])
    if filters.get('stock_status'):
        qs = qs.filter(stock_status=filters['stock_status'])

    today = timezone.now().date()
    data = []
    total_usable_value = Decimal('0.00')
    total_expired_value = Decimal('0.00')

    for inv in qs:
        med = inv.medicine
        usable_batches = med.batches.filter(quantity_available__gt=0, expiry_date__gt=today).exclude(
            status__in=['EXPIRED', 'QUARANTINED', 'RETURNED']
        )
        med_usable_val = sum((b.quantity_available * (b.unit_cost or b.purchase_price) for b in usable_batches), Decimal('0.00'))
        total_usable_value += med_usable_val

        expired_batches = med.batches.filter(quantity_available__gt=0).filter(Q(status='EXPIRED') | Q(expiry_date__lte=today))
        med_expired_val = sum((b.quantity_available * (b.unit_cost or b.purchase_price) for b in expired_batches), Decimal('0.00'))
        total_expired_value += med_expired_val

        batch_count = usable_batches.count()
        data.append({
            'medicine': med.medicine_name,
            'generic_name': med.generic_name or '',
            'category': med.category.name if med.category else '',
            'current_stock': inv.current_stock,
            'available_stock': inv.available_stock,
            'minimum_stock': inv.minimum_stock,
            'maximum_stock': inv.maximum_stock,
            'reorder_level': inv.reorder_level,
            'safety_stock': inv.safety_stock,
            'batch_count': batch_count,
            'stock_status': inv.get_stock_status_display(),
            'inventory_value': round(med_usable_val, 2),
            'expired_value': round(med_expired_val, 2),
            'last_updated': inv.last_updated.strftime('%d %b %Y %H:%M') if inv.last_updated else '',
        })

    summary = {
        'total_medicines': len(data),
        'low_stock': sum(1 for d in data if 'Low' in d['stock_status']),
        'out_of_stock': sum(1 for d in data if 'Out of Stock' in d['stock_status']),
        'overstock': sum(1 for d in data if 'Overstock' in d['stock_status']),
        'total_inventory_value': round(total_usable_value, 2),
        'total_expired_risk_value': round(total_expired_value, 2),
    }
    columns = ['Medicine', 'Generic Name', 'Category', 'Current Stock', 'Available', 'Min', 'Max', 'Reorder Level', 'Safety Stock', 'Batches', 'Status', 'Usable Stock Value', 'Expired Risk Value', 'Last Updated']
    return {'data': data, 'summary': summary, 'columns': columns}


def get_sales_report_data(date_from=None, date_to=None, filters=None):
    """Sales report: completed sales within date range."""
    filters = filters or {}
    start, end = _date_range(date_from, date_to)

    qs = Sale.objects.select_related('sold_by').filter(status__in=['COMPLETED', 'PARTIAL_RETURN'])
    if start:
        qs = qs.filter(sale_date__gte=start)
    if end:
        qs = qs.filter(sale_date__lte=end)
    if filters.get('payment_method'):
        qs = qs.filter(payment_method=filters['payment_method'])

    data = []
    total_revenue = Decimal('0')
    total_tax = Decimal('0')
    total_discount = Decimal('0')
    total_profit = Decimal('0')

    for sale in qs:
        items = sale.items.select_related('medicine', 'batch').all()
        item_names = ', '.join(set(i.medicine.medicine_name for i in items))
        qty = sum(i.quantity for i in items)
        cost = sum(i.purchase_unit_price * i.quantity for i in items)
        profit = sale.total_amount - cost
        total_revenue += sale.total_amount
        total_tax += sale.tax_amount
        total_discount += sale.discount_amount
        total_profit += profit

        data.append({
            'sale_number': sale.sale_number,
            'invoice_number': sale.invoice_number,
            'date': sale.sale_date.strftime('%d %b %Y %H:%M'),
            'medicines': item_names[:60],
            'quantity': qty,
            'subtotal': round(sale.subtotal, 2),
            'tax': round(sale.tax_amount, 2),
            'discount': round(sale.discount_amount, 2),
            'total': round(sale.total_amount, 2),
            'profit': round(profit, 2),
            'payment_method': sale.get_payment_method_display(),
            'status': sale.get_status_display(),
            'pharmacist': sale.sold_by.username if sale.sold_by else 'N/A',
        })

    summary = {
        'total_sales': len(data),
        'total_revenue': round(total_revenue, 2),
        'total_tax': round(total_tax, 2),
        'total_discount': round(total_discount, 2),
        'total_profit': round(total_profit, 2),
    }
    columns = ['Sale Number', 'Invoice', 'Date', 'Medicines', 'Qty', 'Subtotal', 'Tax', 'Discount', 'Total', 'Profit', 'Payment', 'Status', 'Pharmacist']
    return {'data': data, 'summary': summary, 'columns': columns}


def get_purchase_report_data(date_from=None, date_to=None, filters=None):
    """Purchase report: POs within date range."""
    filters = filters or {}
    start, end = _date_range(date_from, date_to)

    qs = PurchaseOrder.objects.select_related('supplier').all()
    if start:
        qs = qs.filter(order_date__gte=start)
    if end:
        qs = qs.filter(order_date__lte=end)
    if filters.get('supplier_id'):
        qs = qs.filter(supplier_id=filters['supplier_id'])
    if filters.get('status'):
        qs = qs.filter(status=filters['status'])

    data = []
    total_amount = Decimal('0')
    for po in qs:
        item_count = po.items.count()
        total_qty = po.items.aggregate(s=Sum('ordered_quantity'))['s'] or 0
        total_amount += po.total_amount
        data.append({
            'po_number': po.po_number,
            'supplier': po.supplier.company_name,
            'medicine_count': item_count,
            'quantity': total_qty,
            'total_amount': round(po.total_amount, 2),
            'status': po.get_status_display(),
            'order_date': po.order_date.strftime('%d %b %Y') if po.order_date else '',
            'expected_delivery': po.expected_delivery_date.strftime('%d %b %Y') if po.expected_delivery_date else 'N/A',
        })

    summary = {
        'total_orders': len(data),
        'total_purchase_value': round(total_amount, 2),
        'pending_count': sum(1 for d in data if 'Pending' in d['status'] or 'Approved' in d['status']),
        'completed_count': sum(1 for d in data if 'Delivered' in d['status'] or 'Received' in d['status']),
    }
    columns = ['PO Number', 'Supplier', 'Items', 'Quantity', 'Total Amount', 'Status', 'Order Date', 'Expected Delivery']
    return {'data': data, 'summary': summary, 'columns': columns}


def get_expiry_report_data(date_from=None, date_to=None, filters=None):
    """Expiry report: batches expiring or already expired using central ExpiryService."""
    from inventory.services import get_batch_expiry_info, ExpiryRiskCategory
    filters = filters or {}
    today = timezone.now().date()
    try:
        days_threshold = int(filters.get('days_threshold', 90))
    except (ValueError, TypeError):
        days_threshold = 90
    category = filters.get('category', 'all')

    qs = MedicineBatch.objects.select_related('medicine', 'medicine__category').filter(
        quantity_available__gt=0
    )

    if category == 'expired':
        qs = qs.filter(expiry_date__lte=today)
    elif category == 'critical':
        qs = qs.filter(expiry_date__gt=today, expiry_date__lte=today + timedelta(days=30))
    elif category == 'warning':
        qs = qs.filter(expiry_date__gt=today + timedelta(days=30), expiry_date__lte=today + timedelta(days=60))
    elif category == 'near_expiry':
        qs = qs.filter(expiry_date__gt=today + timedelta(days=60), expiry_date__lte=today + timedelta(days=days_threshold))
    else:
        qs = qs.filter(expiry_date__lte=today + timedelta(days=days_threshold))

    if filters.get('medicine_id'):
        qs = qs.filter(medicine_id=filters['medicine_id'])
    if filters.get('category_id'):
        qs = qs.filter(medicine__category_id=filters['category_id'])

    data = []
    for batch in qs.order_by('expiry_date'):
        info = get_batch_expiry_info(batch, today=today, near_expiry_days=days_threshold)
        data.append({
            'medicine': batch.medicine.medicine_name,
            'category': batch.medicine.category.name if batch.medicine.category else '',
            'batch_number': batch.batch_number,
            'expiry_date': info['formatted_expiry'],
            'days_remaining': info['days_remaining'] if info['days_remaining'] is not None else 'Unknown',
            'quantity_available': batch.quantity_available,
            'risk_level': info['risk_category'],
            'risk_label': info['risk_label'],
            'status': batch.get_status_display() if hasattr(batch, 'get_status_display') else batch.status,
        })

    summary = {
        'total_batches': len(data),
        'expired': sum(1 for d in data if d['risk_level'] == ExpiryRiskCategory.EXPIRED),
        'critical_30d': sum(1 for d in data if d['risk_level'] == ExpiryRiskCategory.CRITICAL),
        'warning_60d': sum(1 for d in data if d['risk_level'] == ExpiryRiskCategory.WARNING),
        'near_expiry_90d': sum(1 for d in data if d['risk_level'] == ExpiryRiskCategory.NEAR_EXPIRY),
        'total_at_risk_qty': sum(d['quantity_available'] for d in data),
    }
    columns = ['Medicine', 'Category', 'Batch Number', 'Expiry Date', 'Days Remaining', 'Quantity Available', 'Risk Level', 'Risk Description', 'Status']
    return {'data': data, 'summary': summary, 'columns': columns}


def get_profit_report_data(date_from=None, date_to=None, filters=None):
    """Profit report using actual historical cost basis from SaleItem.purchase_unit_price, excluding taxes collected."""
    filters = filters or {}
    start, end = _date_range(date_from, date_to)

    qs = SaleItem.objects.select_related('medicine', 'sale', 'batch').filter(
        sale__status__in=['COMPLETED', 'PARTIAL_RETURN']
    )
    if start:
        qs = qs.filter(sale__sale_date__gte=start)
    if end:
        qs = qs.filter(sale__sale_date__lte=end)
    if filters.get('medicine_id'):
        qs = qs.filter(medicine_id=filters['medicine_id'])
    if filters.get('category_id'):
        qs = qs.filter(medicine__category_id=filters['category_id'])

    from collections import defaultdict
    agg = defaultdict(lambda: {'qty': 0, 'revenue': Decimal('0.00'), 'cost': Decimal('0.00')})
    for item in qs:
        k = item.medicine.medicine_name
        agg[k]['qty'] += item.active_quantity
        agg[k]['revenue'] += item.net_taxable_amount
        agg[k]['cost'] += item.net_cost

    data = []
    total_revenue = Decimal('0.00')
    total_cost = Decimal('0.00')
    total_profit = Decimal('0.00')

    for med_name, vals in sorted(agg.items(), key=lambda x: x[1]['revenue'], reverse=True):
        profit = max(Decimal('0.00'), vals['revenue'] - vals['cost'])
        margin = round((profit / vals['revenue'] * Decimal('100.00')), 1) if vals['revenue'] else Decimal('0.00')
        total_revenue += vals['revenue']
        total_cost += vals['cost']
        total_profit += profit
        data.append({
            'medicine': med_name,
            'quantity_sold': vals['qty'],
            'revenue': round(vals['revenue'], 2),
            'cost': round(vals['cost'], 2),
            'gross_profit': round(profit, 2),
            'profit_margin': margin,
        })

    summary = {
        'total_revenue': round(total_revenue, 2),
        'total_cost': round(total_cost, 2),
        'total_gross_profit': round(total_profit, 2),
        'overall_margin': round((total_profit / total_revenue * Decimal('100.00')), 1) if total_revenue else Decimal('0.00'),
    }
    columns = ['Medicine', 'Units Sold', 'Net Revenue (Excl Tax)', 'Cost Basis (COGS)', 'Gross Profit', 'Gross Margin %']
    return {'data': data, 'summary': summary, 'columns': columns}


def get_low_stock_report_data(date_from=None, date_to=None, filters=None):
    """Low stock report enriched with Phase 6 AI demand forecast."""
    from ai_engine.intelligence_service import get_demand_forecast
    filters = filters or {}

    qs = Inventory.objects.select_related('medicine', 'medicine__category').filter(
        stock_status__in=['LOW_STOCK', 'OUT_OF_STOCK']
    )
    if filters.get('category_id'):
        qs = qs.filter(medicine__category_id=filters['category_id'])

    data = []
    for inv in qs:
        forecast = get_demand_forecast(inv.medicine)
        rec_qty = max(forecast['forecast_30d'] + inv.safety_stock - inv.current_stock, inv.maximum_stock - inv.current_stock, 1)
        priority = 'CRITICAL' if inv.current_stock == 0 else ('HIGH' if inv.current_stock <= int(inv.reorder_level * 0.3) else 'MEDIUM')
        data.append({
            'medicine': inv.medicine.medicine_name,
            'category': inv.medicine.category.name if inv.medicine.category else '',
            'current_stock': inv.current_stock,
            'reorder_level': inv.reorder_level,
            'minimum_stock': inv.minimum_stock,
            'safety_stock': inv.safety_stock,
            'forecast_30d': forecast['forecast_30d'],
            'recommended_reorder': rec_qty,
            'priority': priority,
        })

    data.sort(key=lambda x: {'CRITICAL': 0, 'HIGH': 1, 'MEDIUM': 2}.get(x['priority'], 3))
    summary = {
        'total_low_stock': len(data),
        'critical': sum(1 for d in data if d['priority'] == 'CRITICAL'),
        'high': sum(1 for d in data if d['priority'] == 'HIGH'),
    }
    columns = ['Medicine', 'Category', 'Current Stock', 'Reorder Level', 'Min Stock', 'Safety Stock', '30D Forecast', 'Recommended Order', 'Priority']
    return {'data': data, 'summary': summary, 'columns': columns}


def get_stock_movement_report_data(date_from=None, date_to=None, filters=None):
    """Stock movement report: all StockTransactions in date range."""
    filters = filters or {}
    start, end = _date_range(date_from, date_to)

    qs = StockTransaction.objects.select_related('medicine', 'batch', 'performed_by').all()
    if start:
        qs = qs.filter(created_at__gte=start)
    if end:
        qs = qs.filter(created_at__lte=end)
    if filters.get('medicine_id'):
        qs = qs.filter(medicine_id=filters['medicine_id'])
    if filters.get('transaction_type'):
        qs = qs.filter(transaction_type=filters['transaction_type'])

    data = []
    for txn in qs[:1000]:
        data.append({
            'date': txn.created_at.strftime('%d %b %Y %H:%M'),
            'medicine': txn.medicine.medicine_name,
            'batch': txn.batch.batch_number if txn.batch else 'N/A',
            'transaction_type': txn.get_transaction_type_display(),
            'quantity': txn.quantity,
            'previous_stock': txn.previous_stock,
            'new_stock': txn.new_stock,
            'reference': txn.reference_number or '',
            'performed_by': txn.performed_by.username if txn.performed_by else 'System',
        })

    summary = {
        'total_transactions': qs.count(),
        'total_added': qs.filter(transaction_type__in=['PURCHASE', 'RETURN', 'ADJUSTMENT', 'OPENING']).aggregate(s=Sum('quantity'))['s'] or 0,
        'total_removed': qs.filter(transaction_type__in=['SALE', 'DAMAGE', 'EXPIRY', 'DISPENSE']).aggregate(s=Sum('quantity'))['s'] or 0,
    }
    columns = ['Date', 'Medicine', 'Batch', 'Transaction Type', 'Quantity', 'Previous Stock', 'New Stock', 'Reference', 'Performed By']
    return {'data': data, 'summary': summary, 'columns': columns}


def get_supplier_report_data(date_from=None, date_to=None, filters=None):
    """Supplier performance report using actual PO and supplier metrics."""
    filters = filters or {}
    start, end = _date_range(date_from, date_to)

    qs = Supplier.objects.filter(status='ACTIVE')
    if filters.get('supplier_id'):
        qs = qs.filter(id=filters['supplier_id'])

    data = []
    for supplier in qs:
        po_qs = PurchaseOrder.objects.filter(supplier=supplier)
        if start:
            po_qs = po_qs.filter(order_date__gte=start)
        if end:
            po_qs = po_qs.filter(order_date__lte=end)

        po_count = po_qs.count()
        total_value = po_qs.aggregate(s=Sum('total_amount'))['s'] or Decimal('0')
        price_changes = SupplierPriceHistory.objects.filter(supplier=supplier).count()

        on_time = f"{supplier.on_time_delivery_rate:.1f}%" if supplier.on_time_delivery_rate is not None else "N/A"
        fill = f"{supplier.fill_rate:.1f}%" if supplier.fill_rate is not None else "N/A"

        data.append({
            'supplier': supplier.company_name,
            'contact': supplier.contact_person or '',
            'orders': po_count,
            'purchase_value': round(total_value, 2),
            'avg_delivery_days': supplier.average_delivery_days or 0,
            'on_time_rate': on_time,
            'fill_rate': fill,
            'rating': supplier.rating or 0,
            'price_changes': price_changes,
        })

    data.sort(key=lambda x: x['purchase_value'], reverse=True)
    summary = {
        'total_suppliers': len(data),
        'total_spend': round(sum(d['purchase_value'] for d in data), 2),
        'avg_delivery_days': round(sum(d['avg_delivery_days'] for d in data) / len(data), 1) if data else 0,
    }
    columns = ['Supplier', 'Contact', 'Orders', 'Purchase Value', 'Avg Delivery Days', 'On-Time Rate', 'Fill Rate', 'Rating', 'Price Changes']
    return {'data': data, 'summary': summary, 'columns': columns}


def get_price_history_report_data(date_from=None, date_to=None, filters=None):
    """Supplier price history report."""
    filters = filters or {}
    start, end = _date_range(date_from, date_to)

    qs = SupplierPriceHistory.objects.select_related('supplier', 'medicine').all()
    if start:
        qs = qs.filter(changed_at__gte=start)
    if end:
        qs = qs.filter(changed_at__lte=end)
    if filters.get('supplier_id'):
        qs = qs.filter(supplier_id=filters['supplier_id'])
    if filters.get('medicine_id'):
        qs = qs.filter(medicine_id=filters['medicine_id'])
    if filters.get('direction') == 'increase':
        qs = qs.filter(price_change_pct__gt=0)
    elif filters.get('direction') == 'decrease':
        qs = qs.filter(price_change_pct__lt=0)

    data = []
    for sph in qs.order_by('-changed_at')[:500]:
        data.append({
            'date': sph.changed_at.strftime('%d %b %Y') if sph.changed_at else '',
            'medicine': sph.medicine.medicine_name if sph.medicine else '',
            'supplier': sph.supplier.company_name if sph.supplier else '',
            'old_price': round(sph.old_price, 2) if sph.old_price is not None else 0.0,
            'new_price': round(sph.new_price, 2) if sph.new_price is not None else 0.0,
            'change': round((sph.new_price or Decimal('0')) - (sph.old_price or Decimal('0')), 2),
            'change_pct': f"{sph.price_change_pct:.1f}%" if sph.price_change_pct is not None else '0%',
            'direction': 'Increase' if float(sph.price_change_pct or 0) > 0 else 'Decrease',
        })

    summary = {
        'total_changes': len(data),
        'increases': sum(1 for d in data if d['direction'] == 'Increase'),
        'decreases': sum(1 for d in data if d['direction'] == 'Decrease'),
    }
    columns = ['Date', 'Medicine', 'Supplier', 'Old Price', 'New Price', 'Change', 'Change %', 'Direction']
    return {'data': data, 'summary': summary, 'columns': columns}


def get_ai_prediction_report_data(date_from=None, date_to=None, filters=None):
    """AI Prediction report using Phase 6 intelligence service."""
    from ai_engine.intelligence_service import get_demand_forecast
    from medicines.models import Medicine
    filters = filters or {}

    meds = Medicine.objects.filter(status='ACTIVE')
    if filters.get('medicine_id'):
        meds = meds.filter(id=filters['medicine_id'])

    data = []
    for med in meds[:50]:
        forecast = get_demand_forecast(med)
        inv = getattr(med, 'inventory', None)
        current_stock = inv.current_stock if inv else 0
        rec_qty = max(forecast['forecast_30d'] + (inv.safety_stock if inv else 0) - current_stock, 0) if forecast['forecast_30d'] > current_stock else 0
        conf_val = forecast.get('confidence_score')
        conf_str = f"{conf_val:.1f}%" if conf_val is not None else "N/A"
        data.append({
            'medicine': med.medicine_name,
            'category': med.category.name if med.category else '',
            'forecast_30d': forecast['forecast_30d'],
            'daily_rate': forecast['daily_rate'],
            'current_stock': current_stock,
            'recommended_order': rec_qty,
            'model_type': forecast['model_type'],
            'confidence': conf_str,
            'cold_start': 'Yes (Insufficient Data)' if forecast.get('is_cold_start') else 'No',
            'explanation': forecast['explanation'][:80],
        })

    summary = {
        'total_medicines_analyzed': len(data),
        'requiring_reorder': sum(1 for d in data if d['recommended_order'] > 0),
        'ml_predictions': sum(1 for d in data if d['model_type'] == 'ML_MODEL'),
        'rule_based': sum(1 for d in data if d['model_type'] == 'RULE_BASED'),
    }
    columns = ['Medicine', 'Category', '30D Forecast', 'Daily Rate', 'Current Stock', 'Recommended Order', 'Model', 'Confidence', 'Cold Start', 'Explanation']
    return {'data': data, 'summary': summary, 'columns': columns}


def get_consumption_report_data(date_from=None, date_to=None, filters=None):
    """Consumption analytics report."""
    from inventory.consumption_service import get_consumption_analytics
    from medicines.models import Medicine
    filters = filters or {}

    meds = Medicine.objects.filter(status='ACTIVE').select_related('category', 'inventory')
    if filters.get('category_id'):
        meds = meds.filter(category_id=filters['category_id'])
    if filters.get('medicine_id'):
        meds = meds.filter(id=filters['medicine_id'])

    data = []
    total_30d = 0
    for med in meds:
        c = get_consumption_analytics(med)
        stock = med.inventory.current_stock if hasattr(med, 'inventory') else 0
        total_30d += c['consumed_30d']
        data.append({
            'medicine': med.medicine_name,
            'category': med.category.name if med.category else '',
            'current_stock': stock,
            'consumed_7d': c['consumed_7d'],
            'consumed_14d': c['consumed_14d'],
            'consumed_30d': c['consumed_30d'],
            'consumed_90d': c['consumed_90d'],
            'daily_avg_30d': c['daily_avg_30d'],
            'data_quality': c['data_quality'],
            'last_movement': c['last_movement_date'].strftime('%d %b %Y') if c['last_movement_date'] else 'No Movement',
        })

    data.sort(key=lambda x: x['consumed_30d'], reverse=True)
    summary = {
        'total_medicines': len(data),
        'total_consumed_30d': total_30d,
        'good_data_quality': sum(1 for d in data if d['data_quality'] == 'GOOD'),
        'insufficient_data': sum(1 for d in data if d['data_quality'] == 'INSUFFICIENT_DATA'),
    }
    columns = ['Medicine', 'Category', 'Current Stock', '7D Consumed', '14D Consumed', '30D Consumed', '90D Consumed', 'Daily Rate', 'Data Quality', 'Last Movement']
    return {'data': data, 'summary': summary, 'columns': columns}


def get_optimization_report_data(date_from=None, date_to=None, filters=None):
    """Composite inventory optimization & health report."""
    from inventory.health_service import classify_inventory_health
    from ai_engine.intelligence_service import get_inventory_risk_score
    from medicines.models import Medicine
    filters = filters or {}

    meds = Medicine.objects.filter(status='ACTIVE').select_related('category', 'inventory')
    if filters.get('category_id'):
        meds = meds.filter(category_id=filters['category_id'])

    data = []
    for med in meds:
        health = classify_inventory_health(med)
        risk = get_inventory_risk_score(med)
        dos = health['days_of_stock']
        dos_str = str(dos) if dos is not None and dos < 999 else 'N/A'
        data.append({
            'medicine': med.medicine_name,
            'category': med.category.name if med.category else '',
            'current_stock': health['current_stock'],
            'health_status': health['status'],
            'days_of_stock': dos_str,
            'daily_avg': health['daily_avg'],
            'risk_score': f"{risk['score']}/100",
            'risk_level': risk['level'],
            'explanation': health['explanation'][:80],
        })

    summary = {
        'total_medicines': len(data),
        'critical_or_oos': sum(1 for d in data if d['health_status'] in ('CRITICAL', 'OUT_OF_STOCK')),
        'dead_or_overstock': sum(1 for d in data if d['health_status'] in ('DEAD_STOCK', 'OVERSTOCK')),
        'normal_stock': sum(1 for d in data if d['health_status'] == 'NORMAL'),
    }
    columns = ['Medicine', 'Category', 'Current Stock', 'Health Status', 'Days of Stock', 'Daily Avg', 'Risk Score', 'Risk Level', 'Diagnosis']
    return {'data': data, 'summary': summary, 'columns': columns}


def get_dispensing_report_data(date_from=None, date_to=None, filters=None):
    """Medication Dispensing & Traceability report."""
    filters = filters or {}
    start, end = _date_range(date_from, date_to)

    qs = DispensingRecord.objects.select_related('medicine', 'pharmacist', 'prescription').prefetch_related('allocations').all()
    if start:
        qs = qs.filter(created_at__gte=start)
    if end:
        qs = qs.filter(created_at__lte=end)
    if filters.get('medicine_id'):
        qs = qs.filter(medicine_id=filters['medicine_id'])
    if filters.get('regulatory_category'):
        qs = qs.filter(regulatory_category=filters['regulatory_category'])
    if filters.get('status'):
        qs = qs.filter(status=filters['status'])

    data = []
    total_units = 0
    for rec in qs[:1000]:
        total_units += rec.total_quantity
        batch_summary = ", ".join([f"{a.quantity_allocated}x {a.batch_number}" for a in rec.allocations.all()])
        data.append({
            'dispensing_id': rec.dispensing_id,
            'date': rec.created_at.strftime('%d %b %Y %H:%M'),
            'patient_name': rec.patient_name,
            'patient_id': rec.patient_id,
            'medicine': rec.medicine.medicine_name,
            'regulatory_class': rec.get_regulatory_category_display(),
            'total_quantity': rec.total_quantity,
            'batches_allocated': batch_summary or 'N/A',
            'pharmacist': rec.pharmacist.username if rec.pharmacist else 'N/A',
            'status': rec.get_status_display(),
        })

    summary = {
        'total_dispensing_records': len(data),
        'total_units_dispensed': total_units,
        'completed': sum(1 for d in data if d['status'] == 'Completed'),
        'reversed': sum(1 for d in data if d['status'] == 'Reversed / Returned'),
    }
    columns = ['Dispensing ID', 'Date', 'Patient Name', 'UHID', 'Medicine', 'Regulatory Class', 'Quantity', 'Batches Allocated', 'Pharmacist', 'Status']
    return {'data': data, 'summary': summary, 'columns': columns}


def get_controlled_report_data(date_from=None, date_to=None, filters=None):
    """Statutory Controlled Medicine running ledger report."""
    filters = filters or {}
    start, end = _date_range(date_from, date_to)

    qs = ControlledMedicineRegister.objects.select_related('medicine', 'batch', 'pharmacist').all()
    if start:
        qs = qs.filter(transaction_date__gte=start)
    if end:
        qs = qs.filter(transaction_date__lte=end)
    if filters.get('medicine_id'):
        qs = qs.filter(medicine_id=filters['medicine_id'])
    if filters.get('regulatory_category'):
        qs = qs.filter(regulatory_category=filters['regulatory_category'])

    data = []
    total_out = 0
    total_in = 0
    for entry in qs[:1000]:
        total_out += entry.quantity_dispensed
        total_in += entry.quantity_in
        data.append({
            'register_id': entry.register_id,
            'date': entry.transaction_date.strftime('%d %b %Y %H:%M'),
            'medicine': entry.medicine.medicine_name,
            'regulatory_class': entry.get_regulatory_category_display(),
            'batch': entry.batch.batch_number if entry.batch else 'N/A',
            'patient_name': entry.patient_name,
            'doctor': entry.prescribing_doctor,
            'doctor_reg': entry.doctor_registration or 'N/A',
            'opening_balance': entry.opening_balance,
            'quantity_in': entry.quantity_in,
            'quantity_dispensed': entry.quantity_dispensed,
            'closing_balance': entry.closing_balance,
            'pharmacist': entry.pharmacist.username if entry.pharmacist else 'N/A',
        })

    summary = {
        'total_ledger_entries': len(data),
        'total_units_dispensed': total_out,
        'total_units_received': total_in,
    }
    columns = ['Register ID', 'Date', 'Medicine', 'Class', 'Batch', 'Patient', 'Doctor', 'Doctor Reg', 'Opening Bal', 'Qty In', 'Qty Out', 'Closing Bal', 'Pharmacist']
    return {'data': data, 'summary': summary, 'columns': columns}


def get_consumption_report_data(date_from=None, date_to=None, filters=None):
    """Dynamic consumption velocity and movement report."""
    from inventory import consumption_service
    medicines = Medicine.objects.filter(status='ACTIVE').select_related('category', 'inventory')
    data = []
    total_30d = 0
    total_today = 0

    for med in medicines:
        stats = consumption_service.get_consumption_analytics(med)
        total_30d += stats['consumed_30d']
        total_today += stats['consumed_today']
        data.append({
            'medicine': med.medicine_name,
            'generic_name': med.generic_name or '',
            'category': med.category.name if med.category else '—',
            'consumed_today': stats['consumed_today'],
            'consumed_7d': stats['consumed_7d'],
            'consumed_30d': stats['consumed_30d'],
            'consumed_90d': stats['consumed_90d'],
            'daily_avg_30d': stats['daily_avg_30d'],
            'trend_pct': f"{stats['consumption_trend_pct']}%",
            'data_quality': stats['data_quality'],
            'stockout_warning': 'Yes' if stats['stockout_suppressed'] else 'No',
        })

    summary = {
        'total_medicines': len(data),
        'total_consumed_today': total_today,
        'total_consumed_30d': total_30d,
        'stockout_affected': sum(1 for d in data if d['stockout_warning'] == 'Yes'),
    }
    columns = ['Medicine', 'Generic Name', 'Category', 'Consumed Today', '7d Consumed', '30d Consumed', '90d Consumed', 'Daily Run Rate (30d)', 'Trend (30d)', 'Data Quality', 'Stockout Suppressed']
    return {'data': data, 'summary': summary, 'columns': columns}


def get_hospital_integration_report_data(date_from=None, date_to=None, filters=None):
    """Hospital and EHR Integration activity report."""
    from integrations.models import HospitalMedicationOrder
    start, end = _date_range(date_from, date_to)
    qs = HospitalMedicationOrder.objects.select_related('hospital_system', 'mapped_medicine', 'prescription').all()
    if start:
        qs = qs.filter(received_at__gte=start)
    if end:
        qs = qs.filter(received_at__lte=end)

    data = []
    for order in qs[:500]:
        data.append({
            'order_id': order.external_order_id,
            'system': order.hospital_system.name if order.hospital_system else 'Cerner / EHR',
            'patient_uhid': order.patient_uhid,
            'patient_name': order.patient_name,
            'medication_requested': order.raw_medication_name,
            'mapped_medicine': order.mapped_medicine.medicine_name if order.mapped_medicine else 'Unmapped',
            'prescribed_qty': order.prescribed_quantity,
            'priority': order.priority.upper(),
            'status': order.get_status_display(),
            'received_at': order.received_at.strftime('%d %b %Y %H:%M'),
        })

    summary = {
        'total_orders': len(data),
        'awaiting_review': sum(1 for d in data if 'Review' in d['status']),
        'verified': sum(1 for d in data if 'Verified' in d['status']),
        'dispensed': sum(1 for d in data if 'Dispensed' in d['status']),
    }
    columns = ['External Order ID', 'Hospital System', 'UHID', 'Patient Name', 'Medication Requested', 'Mapped Medicine', 'Prescribed Qty', 'Priority', 'Status', 'Received At']
    return {'data': data, 'summary': summary, 'columns': columns}


def get_reorder_recommendation_report_data(date_from=None, date_to=None, filters=None):
    """Explainable AI Reorder Recommendations Report (Open PO Aware)."""
    from ai_engine.intelligence_service import get_reorder_recommendations
    recs = get_reorder_recommendations()
    data = []
    total_est_cost = Decimal('0')

    for r in recs:
        total_est_cost += r['estimated_total_cost']
        data.append({
            'medicine': r['medicine'].medicine_name,
            'current_stock': r['current_stock'],
            'open_po_qty': r['open_po_qty'],
            'reorder_level': r['reorder_level'],
            'forecast_30d': r['forecast_30d'],
            'recommended_qty': r['recommended_qty'],
            'priority': r['priority'],
            'recommended_supplier': r['recommended_supplier'].name if r['recommended_supplier'] else 'Best Available',
            'estimated_unit_price': r['estimated_unit_price'],
            'estimated_total_cost': r['estimated_total_cost'],
            'explanation': r['explanation'],
        })

    summary = {
        'total_recommendations': len(data),
        'critical_priority': sum(1 for d in data if d['priority'] == 'CRITICAL'),
        'high_priority': sum(1 for d in data if d['priority'] == 'HIGH'),
        'total_estimated_procurement_cost': float(total_est_cost),
    }
    columns = ['Medicine', 'Current Stock', 'Open PO Qty', 'Reorder Level', '30d Forecast', 'Recommended Qty', 'Priority', 'Best Supplier', 'Unit Price', 'Total Cost', 'Clinical Rationale']
    return {'data': data, 'summary': summary, 'columns': columns}


def get_demand_forecast_report_data(date_from=None, date_to=None, filters=None):
    """Multi-horizon demand forecasting report."""
    from ai_engine.intelligence_service import get_demand_forecast_multi_horizon
    medicines = Medicine.objects.filter(status='ACTIVE')[:100]
    data = []
    for med in medicines:
        fc = get_demand_forecast_multi_horizon(med)
        data.append({
            'medicine': med.medicine_name,
            'model_type': fc['model_type'],
            'data_origin': fc.get('data_origin', 'FORECAST'),
            'confidence_score': f"{fc['confidence_score']}%",
            'daily_run_rate': fc['daily_rate'],
            'forecast_7d': fc['forecast_7d'],
            'forecast_14d': fc['forecast_14d'],
            'forecast_30d': fc['forecast_30d'],
            'forecast_60d': fc['forecast_60d'],
            'forecast_90d': fc['forecast_90d'],
        })

    summary = {
        'total_medicines_forecasted': len(data),
        'ml_models': sum(1 for d in data if d['model_type'] == 'ML_MODEL'),
        'rule_based_fallbacks': sum(1 for d in data if d['model_type'] == 'RULE_BASED'),
    }
    columns = ['Medicine', 'Model Type', 'Data Origin', 'Confidence Score', 'Daily Run Rate', '7-Day Horizon', '14-Day Horizon', '30-Day Horizon', '60-Day Horizon', '90-Day Horizon']
    return {'data': data, 'summary': summary, 'columns': columns}


def get_dead_stock_report_data(date_from=None, date_to=None, filters=None):
    """Identifies medicines with positive stock but zero consumption in the last 90 days."""
    from inventory.consumption_service import get_slow_movers
    slow_items = get_slow_movers(days_threshold=90, max_consumed=0, top_n=50)
    data = []
    for item in slow_items:
        data.append({
            'medicine': item['medicine'].medicine_name,
            'category': item['category'],
            'current_stock': item['current_stock'],
            'total_consumed_90d': item['total_consumed'],
            'days_since_last_movement': item['days_since_last_move'] or 'No movements',
        })

    summary = {
        'total_dead_stock_items': len(data),
        'total_idle_stock_units': sum(d['current_stock'] for d in data),
    }
    columns = ['Medicine', 'Category', 'Current Stock', '90-Day Consumption', 'Days Since Last Movement']
    return {'data': data, 'summary': summary, 'columns': columns}


def get_overstock_report_data(date_from=None, date_to=None, filters=None):
    """Identifies medicines exceeding maximum stock or safe days-of-stock thresholds."""
    from inventory.health_service import classify_inventory_health
    medicines = Medicine.objects.filter(status='ACTIVE').select_related('inventory')
    data = []
    for med in medicines:
        h = classify_inventory_health(med)
        if h['status'] == 'OVERSTOCK' or (h['days_of_stock'] and h['days_of_stock'] > 180):
            data.append({
                'medicine': med.medicine_name,
                'current_stock': h['current_stock'],
                'maximum_stock': med.maximum_stock,
                'days_of_stock': h['days_of_stock'],
                'explanation': h['explanation'],
            })

    summary = {
        'total_overstock_items': len(data),
        'excess_units': sum(max(0, d['current_stock'] - d['maximum_stock']) for d in data),
    }
    columns = ['Medicine', 'Current Stock', 'Maximum Stock Threshold', 'Days of Stock Remaining', 'Clinical Analysis']
    return {'data': data, 'summary': summary, 'columns': columns}

