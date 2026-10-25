"""
MediVault — inventory/views.py
Phase 3: Inventory & Stock Management views.
"""
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Sum, Q, Count
from django.utils import timezone
from datetime import timedelta, date, datetime

from medicines.models import Medicine, MedicineBatch, Category
from .models import Inventory, StockTransaction, StockAlert, ConsumptionCache, DailyConsumptionSummary, HistoricalImportLog
from . import services, consumption_service, health_service, historical_import
from suppliers import intelligence_service as supplier_intel
from ai_engine import intelligence_service as ai_intel



def admin_or_pharmacist_required(view_func):
    """Allow both ADMIN and approved PHARMACIST users."""
    @login_required
    def wrapped(request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated:
            return redirect('pharmacist_login')
        if user.role == 'ADMIN':
            return view_func(request, *args, **kwargs)
        if user.role == 'PHARMACIST' and user.status == 'APPROVED':
            return view_func(request, *args, **kwargs)
        messages.error(request, "You are not authorized to access this page.")
        return redirect('pharmacist_login')
    return wrapped


def admin_required(view_func):
    """Restrict view to ADMIN users only."""
    @login_required
    def wrapped(request, *args, **kwargs):
        if request.user.role != 'ADMIN':
            messages.error(request, "This page is restricted to administrators.")
            return redirect('inventory_dashboard')
        return view_func(request, *args, **kwargs)
    return wrapped


def pharmacist_required(view_func):
    """
    Restrict operational clinical actions (such as dispensing) to approved PHARMACIST users.
    If an ADMIN attempts direct access, redirects with clear guidance to administrative oversight.
    """
    @login_required
    def wrapped(request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated:
            return redirect('pharmacist_login')
        if user.role == 'PHARMACIST' and user.status == 'APPROVED':
            return view_func(request, *args, **kwargs)
        if user.role == 'ADMIN':
            if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.path.startswith('/api/'):
                from django.http import JsonResponse
                return JsonResponse({
                    'success': False,
                    'message': 'Operational dispensing execution is restricted to licensed pharmacists. Use audit and history endpoints for administrative oversight.'
                }, status=403)
            messages.info(
                request,
                "Secure Dispensing is an operational clinical workflow reserved for licensed pharmacists. "
                "Redirecting to Dispensing History and Audit Records for administrative oversight."
            )
            return redirect('dispensing_history')
        messages.error(request, "You are not authorized to access this operational dispensing page.")
        return redirect('pharmacist_login')
    return wrapped


@admin_or_pharmacist_required
def inventory_dashboard(request):
    """Main inventory dashboard with KPI cards, status breakdown, and table."""
    today = timezone.now().date()

    # Auto-refresh expiry statuses
    services.generate_expiry_alerts()

    # All medicines with inventory
    medicines = Medicine.objects.filter(status='ACTIVE').select_related('category').prefetch_related('batches', 'inventory')

    # Ensure every active medicine has an Inventory record
    for med in medicines:
        Inventory.objects.get_or_create(
            medicine=med,
            defaults={
                'minimum_stock': med.minimum_stock,
                'maximum_stock': med.maximum_stock,
                'reorder_level': med.reorder_level,
                'safety_stock': med.safety_stock,
            }
        )

    # Refresh stock for each medicine from batch data
    inventories = Inventory.objects.select_related('medicine__category').prefetch_related('medicine__batches')
    for inv in inventories:
        services.recalculate_inventory(inv)

    # Reload after recalculation
    inventories = Inventory.objects.select_related('medicine__category').prefetch_related('medicine__batches')

    # KPI Summary
    total_medicines = inventories.count()
    total_stock = inventories.aggregate(total=Sum('current_stock'))['total'] or 0
    normal_count = inventories.filter(stock_status='NORMAL').count()
    low_stock_count = inventories.filter(stock_status__in=['LOW_STOCK', 'CRITICAL']).count()
    out_of_stock_count = inventories.filter(stock_status='OUT_OF_STOCK').count()
    overstock_count = inventories.filter(stock_status='OVERSTOCK').count()

    kpis = services.get_expiry_kpis(today=today, near_expiry_days=90)
    near_expiry_batches = kpis['near_expiry_count']
    expired_batches = kpis['expired_count']

    # Active alerts
    active_alerts = StockAlert.objects.filter(is_active=True).select_related('medicine').order_by('-created_at')[:10]

    # Search & Filter
    search_q = request.GET.get('search', '').strip()
    status_filter = request.GET.get('status', '')
    category_filter = request.GET.get('category', '')

    table_qs = inventories
    if search_q:
        table_qs = table_qs.filter(
            Q(medicine__medicine_name__icontains=search_q) |
            Q(medicine__generic_name__icontains=search_q)
        )
    if status_filter:
        if status_filter == 'LOW_STOCK':
            table_qs = table_qs.filter(stock_status__in=['LOW_STOCK', 'CRITICAL'])
        else:
            table_qs = table_qs.filter(stock_status=status_filter)
    if category_filter:
        table_qs = table_qs.filter(medicine__category_id=category_filter)

    # Annotate near expiry / nearest expiry date
    inventory_data = []
    for inv in table_qs:
        active_batches = inv.medicine.batches.filter(
            status__in=['ACTIVE', 'NEAR_EXPIRY'],
            quantity_available__gt=0
        ).order_by('expiry_date')
        nearest_expiry = active_batches.first()
        inventory_data.append({
            'inventory': inv,
            'active_batches_count': active_batches.count(),
            'nearest_expiry': nearest_expiry,
        })

    # Pagination
    paginator = Paginator(inventory_data, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    categories = Category.objects.filter(is_active=True)

    # Chart data
    chart_labels = ['Normal', 'Low Stock', 'Out of Stock', 'Overstock']
    chart_data = [normal_count, low_stock_count, out_of_stock_count, overstock_count]

    # Inventory Intelligence Summaries
    try:
        health_summary = health_service.get_inventory_health_summary()
        consumption_summary = consumption_service.get_consumption_dashboard_summary()
    except Exception:
        health_summary = {'counts': {}, 'total': 0}
        consumption_summary = {'total_units_7d': 0, 'total_units_30d': 0, 'daily_avg_30d': 0, 'active_movers_30d': 0}

    context = {
        'page_obj': page_obj,
        'total_medicines': total_medicines,
        'total_stock': total_stock,
        'normal_count': normal_count,
        'low_stock_count': low_stock_count,
        'out_of_stock_count': out_of_stock_count,
        'overstock_count': overstock_count,
        'near_expiry_batches': near_expiry_batches,
        'expired_batches': expired_batches,
        'active_alerts': active_alerts,
        'categories': categories,
        'search_q': search_q,
        'status_filter': status_filter,
        'category_filter': category_filter,
        'chart_labels': chart_labels,
        'chart_data': chart_data,
        'health_summary': health_summary,
        'consumption_summary': consumption_summary,
    }
    return render(request, 'inventory/dashboard.html', context)


@admin_or_pharmacist_required
def batch_stock_view(request):
    """Batch-wise stock list with filters."""
    today = timezone.now().date()
    batches = MedicineBatch.objects.select_related('medicine__category').order_by('expiry_date')

    # Filters
    search_q = request.GET.get('search', '').strip()
    status_filter = request.GET.get('status', '')
    near_expiry_filter = request.GET.get('near_expiry', '')
    controlled_filter = request.GET.get('controlled', '')
    medicine_filter = request.GET.get('medicine', '')

    if search_q:
        batches = batches.filter(
            Q(batch_number__icontains=search_q) |
            Q(medicine__medicine_name__icontains=search_q)
        )
    if status_filter:
        batches = batches.filter(status=status_filter)
    if near_expiry_filter:
        try:
            days = int(near_expiry_filter)
            batches = batches.filter(expiry_date__lte=today + timedelta(days=days), expiry_date__gt=today)
        except ValueError:
            pass
    if controlled_filter == '1':
        batches = batches.filter(is_controlled=True)
    if medicine_filter:
        batches = batches.filter(medicine_id=medicine_filter)

    # Annotate days to expiry and standard risk metadata
    batch_data = []
    for b in batches:
        info = services.get_batch_expiry_info(b, today=today)
        expiry_class = (
            'expired' if info['is_expired']
            else ('critical' if info['risk_category'] == services.ExpiryRiskCategory.CRITICAL
                  else ('near' if info['risk_category'] in [services.ExpiryRiskCategory.WARNING, services.ExpiryRiskCategory.NEAR_EXPIRY]
                        else 'normal'))
        )
        batch_data.append({
            'batch': b,
            'days_to_expiry': info['days_remaining'] if info['days_remaining'] is not None else 0,
            'expiry_class': expiry_class,
            'expiry_info': info,
        })

    paginator = Paginator(batch_data, 25)
    page_obj = paginator.get_page(request.GET.get('page'))

    medicines = Medicine.objects.filter(status='ACTIVE').order_by('medicine_name')

    kpis = services.get_expiry_kpis(today=today, near_expiry_days=90)

    context = {
        'page_obj': page_obj,
        'medicines': medicines,
        'search_q': search_q,
        'status_filter': status_filter,
        'near_expiry_filter': near_expiry_filter,
        'controlled_filter': controlled_filter,
        'medicine_filter': medicine_filter,
        'total_batches': kpis['total_batches'],
        'active_batches': kpis['active_batches'],
        'near_expiry_count': kpis['near_expiry_count'],
        'expired_count': kpis['expired_count'],
    }
    return render(request, 'inventory/batch_stock.html', context)


@admin_or_pharmacist_required
def stock_history_view(request):
    """Full stock transaction audit trail with filters."""
    transactions = StockTransaction.objects.select_related(
        'medicine', 'batch', 'performed_by', 'inventory'
    ).order_by('-created_at')

    # Filters
    search_q = request.GET.get('search', '').strip()
    type_filter = request.GET.get('type', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    medicine_filter = request.GET.get('medicine', '')

    if search_q:
        transactions = transactions.filter(
            Q(medicine__medicine_name__icontains=search_q) |
            Q(reference_number__icontains=search_q)
        )
    if type_filter:
        transactions = transactions.filter(transaction_type=type_filter)
    if date_from:
        transactions = transactions.filter(created_at__date__gte=date_from)
    if date_to:
        transactions = transactions.filter(created_at__date__lte=date_to)
    if medicine_filter:
        transactions = transactions.filter(medicine_id=medicine_filter)

    paginator = Paginator(transactions, 30)
    page_obj = paginator.get_page(request.GET.get('page'))

    medicines = Medicine.objects.filter(status='ACTIVE').order_by('medicine_name')
    transaction_types = StockTransaction.TransactionType.choices

    context = {
        'page_obj': page_obj,
        'medicines': medicines,
        'transaction_types': transaction_types,
        'search_q': search_q,
        'type_filter': type_filter,
        'date_from': date_from,
        'date_to': date_to,
        'medicine_filter': medicine_filter,
        'total_transactions': StockTransaction.objects.count(),
    }
    return render(request, 'inventory/stock_history.html', context)


@admin_or_pharmacist_required
def expiry_management_view(request):
    """Near-expiry and expired batch management page."""
    today = timezone.now().date()

    try:
        days_filter = int(request.GET.get('days', 90))
        if days_filter <= 0:
            days_filter = 90
    except (ValueError, TypeError):
        days_filter = 90

    search_q = request.GET.get('search', '').strip()
    category_id = request.GET.get('category', '').strip()
    sort = request.GET.get('sort', 'expiry_asc')

    # Base querysets - near expiry strictly requires quantity_available > 0 and expiry_date > today
    near_expiry_qs = MedicineBatch.objects.filter(
        expiry_date__gt=today,
        expiry_date__lte=today + timedelta(days=days_filter),
        quantity_available__gt=0
    ).select_related('medicine__category')

    # Expired batches with remaining stock that need quarantine or write-off
    expired_qs = MedicineBatch.objects.filter(
        expiry_date__lte=today,
        quantity_available__gt=0
    ).select_related('medicine__category')

    # Apply search filter
    if search_q:
        near_expiry_qs = near_expiry_qs.filter(
            Q(batch_number__icontains=search_q) |
            Q(medicine__medicine_name__icontains=search_q) |
            Q(medicine__generic_name__icontains=search_q)
        )
        expired_qs = expired_qs.filter(
            Q(batch_number__icontains=search_q) |
            Q(medicine__medicine_name__icontains=search_q) |
            Q(medicine__generic_name__icontains=search_q)
        )

    # Apply category filter
    if category_id:
        near_expiry_qs = near_expiry_qs.filter(medicine__category_id=category_id)
        expired_qs = expired_qs.filter(medicine__category_id=category_id)

    # Apply sorting
    if sort == 'expiry_desc':
        near_expiry_qs = near_expiry_qs.order_by('-expiry_date')
        expired_qs = expired_qs.order_by('-expiry_date')
    elif sort == 'qty_desc':
        near_expiry_qs = near_expiry_qs.order_by('-quantity_available')
        expired_qs = expired_qs.order_by('-quantity_available')
    elif sort == 'name_asc':
        near_expiry_qs = near_expiry_qs.order_by('medicine__medicine_name')
        expired_qs = expired_qs.order_by('medicine__medicine_name')
    else:  # expiry_asc
        near_expiry_qs = near_expiry_qs.order_by('expiry_date')
        expired_qs = expired_qs.order_by('expiry_date')

    # Authoritative KPIs across the entire inventory (using services.get_expiry_kpis)
    kpis = services.get_expiry_kpis(today=today, near_expiry_days=days_filter)

    # Build detail lists with days remaining using shared get_batch_expiry_info
    near_expiry_data = []
    for b in near_expiry_qs:
        info = services.get_batch_expiry_info(b, today=today, near_expiry_days=days_filter)
        near_expiry_data.append({
            'batch': b,
            'days_left': info['days_remaining'],
            'priority': info['priority'],
            'risk_label': info['risk_label'],
            'badge_class': info['badge_class'],
            'is_dispensable': info['is_dispensable'],
        })

    expired_data = []
    for b in expired_qs:
        info = services.get_batch_expiry_info(b, today=today)
        expired_data.append({
            'batch': b,
            'days_expired': info['days_abs'],
            'risk_label': info['risk_label'],
            'badge_class': info['badge_class'],
        })

    categories = Category.objects.filter(is_active=True).order_by('name')

    context = {
        'near_expiry_data': near_expiry_data,
        'expired_data': expired_data,
        'days_filter': days_filter,
        'search_q': search_q,
        'category_id': category_id,
        'sort': sort,
        'categories': categories,
        'near_expiry_count': kpis['near_expiry_count'],
        'expired_count': kpis['expired_count'],
        'critical_count': kpis['critical_count'],
        'warning_count': kpis['warning_count'],
        'filtered_near_count': len(near_expiry_data),
        'filtered_expired_count': len(expired_data),
    }
    return render(request, 'inventory/expiry_management.html', context)


@admin_or_pharmacist_required
def medicine_inventory_detail(request, pk):
    """Detailed inventory view for a single medicine."""
    medicine = get_object_or_404(Medicine, pk=pk)
    inventory, created = Inventory.objects.get_or_create(
        medicine=medicine,
        defaults={
            'minimum_stock': medicine.minimum_stock,
            'maximum_stock': medicine.maximum_stock,
            'reorder_level': medicine.reorder_level,
            'safety_stock': medicine.safety_stock,
        }
    )
    services.recalculate_inventory(inventory)
    inventory.refresh_from_db()

    batches = medicine.batches.order_by('expiry_date')
    today = timezone.now().date()
    batch_data = []
    for b in batches:
        info = services.get_batch_expiry_info(b, today=today)
        days_left = info['days_remaining'] if info['days_remaining'] is not None else 0
        exp_class = (
            'expired' if info['is_expired']
            else ('critical' if info['risk_category'] == services.ExpiryRiskCategory.CRITICAL
                  else ('near' if info['risk_category'] in [services.ExpiryRiskCategory.WARNING, services.ExpiryRiskCategory.NEAR_EXPIRY]
                        else 'normal'))
        )
        batch_data.append({'batch': b, 'days_left': days_left, 'exp_class': exp_class, 'expiry_info': info})

    recent_transactions = StockTransaction.objects.filter(
        medicine=medicine
    ).select_related('batch', 'performed_by').order_by('-created_at')[:10]

    # Chart: last 30 days stock movement by date
    chart_labels = []
    chart_purchases = []
    chart_sales = []
    chart_damage = []
    for i in range(29, -1, -1):
        day = today - timedelta(days=i)
        chart_labels.append(day.strftime('%d %b'))
        day_txns = StockTransaction.objects.filter(medicine=medicine, created_at__date=day)
        chart_purchases.append(day_txns.filter(transaction_type__in=['PURCHASE', 'OPENING']).aggregate(s=Sum('quantity'))['s'] or 0)
        chart_sales.append(day_txns.filter(transaction_type__in=['SALE', 'DISPENSE']).aggregate(s=Sum('quantity'))['s'] or 0)
        chart_damage.append(day_txns.filter(transaction_type='DAMAGE').aggregate(s=Sum('quantity'))['s'] or 0)

    # Advanced Intelligence Metrics
    try:
        consumption_analytics = consumption_service.get_consumption_analytics(medicine)
        consumption_trend = consumption_service.get_daily_consumption_trend(medicine, 30)
        multi_forecast = ai_intel.get_demand_forecast_multi_horizon(medicine)
        health_info = health_service.classify_inventory_health(medicine)
        supplier_rankings = supplier_intel.get_supplier_rankings(medicine)
        price_intel = supplier_intel.get_price_intelligence(medicine)
        risk_score = ai_intel.get_inventory_risk_score(medicine)
    except Exception:
        consumption_analytics = {}
        consumption_trend = []
        multi_forecast = {}
        health_info = {'status': inventory.stock_status, 'badge_color': 'secondary', 'days_of_stock': 0, 'explanation': ''}
        supplier_rankings = []
        price_intel = {}
        risk_score = {'score': 0, 'level': 'LOW'}

    # Inventory Optimization Engine
    try:
        optimization = consumption_service.evaluate_inventory_optimization(medicine)
    except Exception:
        optimization = None

    context = {
        'medicine': medicine,
        'inventory': inventory,
        'batch_data': batch_data,
        'recent_transactions': recent_transactions,
        'chart_labels': chart_labels,
        'chart_purchases': chart_purchases,
        'chart_sales': chart_sales,
        'chart_damage': chart_damage,
        'consumption_analytics': consumption_analytics,
        'consumption_trend': consumption_trend,
        'multi_forecast': multi_forecast,
        'health_info': health_info,
        'supplier_rankings': supplier_rankings,
        'price_intel': price_intel,
        'risk_score': risk_score,
        'optimization': optimization,
    }
    return render(request, 'inventory/medicine_detail.html', context)


@admin_or_pharmacist_required
def add_stock_view(request):
    """Add stock to a medicine batch."""
    medicines = Medicine.objects.filter(status='ACTIVE').order_by('medicine_name')

    if request.method == 'POST':
        medicine_id = request.POST.get('medicine')
        batch_id = request.POST.get('batch')
        quantity_str = request.POST.get('quantity', '0')
        transaction_type = request.POST.get('transaction_type', 'PURCHASE')
        reference_number = request.POST.get('reference_number', '').strip()
        notes = request.POST.get('notes', '').strip()

        try:
            medicine = Medicine.objects.get(pk=medicine_id)
            batch = MedicineBatch.objects.get(pk=batch_id, medicine=medicine)
            quantity = int(quantity_str)
            if quantity <= 0:
                raise ValidationError("Quantity must be greater than zero.")

            services.add_stock(
                medicine=medicine,
                batch=batch,
                quantity=quantity,
                transaction_type=transaction_type,
                performed_by=request.user,
                reference_number=reference_number,
                notes=notes,
            )
            messages.success(request, f"Successfully added {quantity} units to {medicine.medicine_name} — Batch {batch.batch_number}.")
            return redirect('inventory_dashboard')
        except Medicine.DoesNotExist:
            messages.error(request, "Medicine not found.")
        except MedicineBatch.DoesNotExist:
            messages.error(request, "Batch not found or does not belong to the selected medicine.")
        except (ValidationError, ValueError) as e:
            messages.error(request, str(e))

    return render(request, 'inventory/add_stock.html', {'medicines': medicines})


@admin_or_pharmacist_required
def get_medicine_batches(request):
    """AJAX: Return batches for a given medicine ID."""
    medicine_id = request.GET.get('medicine_id')
    if not medicine_id:
        return JsonResponse({'batches': []})
    batches = MedicineBatch.objects.filter(
        medicine_id=medicine_id
    ).exclude(status__in=['EXPIRED', 'RETURNED']).values(
        'id', 'batch_number', 'quantity_available', 'expiry_date', 'status'
    )
    data = []
    for b in batches:
        data.append({
            'id': b['id'],
            'batch_number': b['batch_number'],
            'quantity_available': b['quantity_available'],
            'expiry_date': str(b['expiry_date']),
            'status': b['status'],
        })
    return JsonResponse({'batches': data})


@admin_or_pharmacist_required
def adjust_stock_view(request):
    """Stock adjustment (increase, decrease, correction)."""
    medicines = Medicine.objects.filter(status='ACTIVE').order_by('medicine_name')

    if request.method == 'POST':
        medicine_id = request.POST.get('medicine')
        batch_id = request.POST.get('batch')
        adjustment_type = request.POST.get('adjustment_type')
        quantity_str = request.POST.get('quantity', '0')
        reason = request.POST.get('reason', '').strip()
        notes = request.POST.get('notes', '').strip()

        if not reason:
            messages.error(request, "A reason is required for stock adjustment.")
            return render(request, 'inventory/adjust_stock.html', {'medicines': medicines})

        try:
            medicine = Medicine.objects.get(pk=medicine_id)
            batch = MedicineBatch.objects.get(pk=batch_id, medicine=medicine)
            quantity = int(quantity_str)

            services.adjust_stock(
                medicine=medicine,
                batch=batch,
                adjustment_type=adjustment_type,
                quantity=quantity,
                performed_by=request.user,
                reason=reason,
                notes=notes,
            )
            messages.success(request, f"Stock adjusted for {medicine.medicine_name} — Batch {batch.batch_number}.")
            return redirect('inventory_dashboard')
        except (Medicine.DoesNotExist, MedicineBatch.DoesNotExist):
            messages.error(request, "Medicine or Batch not found.")
        except (ValidationError, ValueError) as e:
            messages.error(request, str(e))

    return render(request, 'inventory/adjust_stock.html', {'medicines': medicines})


@admin_or_pharmacist_required
def record_damage_view(request):
    """Record damaged stock."""
    medicines = Medicine.objects.filter(status='ACTIVE').order_by('medicine_name')

    if request.method == 'POST':
        medicine_id = request.POST.get('medicine')
        batch_id = request.POST.get('batch')
        quantity_str = request.POST.get('quantity', '0')
        reason = request.POST.get('reason', '').strip()
        notes = request.POST.get('notes', '').strip()

        try:
            medicine = Medicine.objects.get(pk=medicine_id)
            batch = MedicineBatch.objects.get(pk=batch_id, medicine=medicine)
            quantity = int(quantity_str)

            services.record_damage(
                medicine=medicine,
                batch=batch,
                quantity=quantity,
                performed_by=request.user,
                reason=reason,
                notes=notes,
            )
            messages.success(request, f"Damage of {quantity} units recorded for {medicine.medicine_name} — Batch {batch.batch_number}.")
            return redirect('inventory_dashboard')
        except (Medicine.DoesNotExist, MedicineBatch.DoesNotExist):
            messages.error(request, "Medicine or Batch not found.")
        except (ValidationError, ValueError) as e:
            messages.error(request, str(e))

    return render(request, 'inventory/record_damage.html', {'medicines': medicines})


@admin_or_pharmacist_required
def consumption_analytics_view(request):
    """
    Inventory Consumption Analytics Page:
    - Real-time deduped 7/30/90-day consumption metrics
    - Daily, weekly, monthly averages with correct period labelling
    - Fast and slow moving medicines
    - Stockout risk indicators
    - Trend indicators
    - Date-range, medicine, category, trend filters
    """
    from analytics.date_service import resolve_date_range as resolve
    import json

    search_q = request.GET.get('search', '').strip()
    category_filter = request.GET.get('category', '')
    dq_filter = request.GET.get('dq', '')
    trend_filter = request.GET.get('trend', '')
    period = request.GET.get('period', '30d')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    medicines = Medicine.objects.filter(status='ACTIVE').select_related('category', 'inventory')

    if search_q:
        medicines = medicines.filter(
            Q(medicine_name__icontains=search_q) |
            Q(generic_name__icontains=search_q)
        )
    if category_filter:
        medicines = medicines.filter(category_id=category_filter)

    analytics_list = []
    for med in medicines:
        data = consumption_service.get_consumption_analytics(med)
        data['medicine'] = med
        inv = getattr(med, 'inventory', None)
        data['current_stock'] = inv.current_stock if inv else 0

        # Trend label
        pct = data.get('consumption_trend_pct', 0)
        if pct > 10:
            data['trend_label'] = 'INCREASING'
        elif pct < -10:
            data['trend_label'] = 'DECREASING'
        elif data.get('txn_count_30d', 0) < 5:
            data['trend_label'] = 'INSUFFICIENT_DATA'
        else:
            data['trend_label'] = 'STABLE'

        # Quick stockout risk
        risk = consumption_service.get_stockout_risk(med)
        data['stockout_risk'] = risk['risk_level']
        data['days_until_stockout'] = risk.get('days_until_stockout')

        if dq_filter and data['data_quality'] != dq_filter:
            continue
        if trend_filter and data['trend_label'] != trend_filter:
            continue
        analytics_list.append(data)

    # Sort by 30-day consumption descending
    analytics_list.sort(key=lambda x: x['consumed_30d'], reverse=True)

    paginator = Paginator(analytics_list, 20)
    page_obj = paginator.get_page(request.GET.get('page'))

    # Dashboard KPIs
    try:
        summary = consumption_service.get_consumption_dashboard_summary()
    except Exception:
        summary = {
            'consumed_today': 0, 'total_units_7d': 0, 'total_units_30d': 0,
            'total_units_90d': 0, 'daily_avg_30d': 0, 'active_movers_30d': 0,
            'increasing_demand': 0, 'decreasing_demand': 0, 'stable_demand': 0,
        }

    fast_movers = consumption_service.get_fast_movers(top_n=8, days=30)
    slow_movers = consumption_service.get_slow_movers(days_threshold=30, top_n=8)
    categories = Category.objects.filter(is_active=True)

    # Daily trend chart data (last 30 days, aggregate across all medicines)
    today = timezone.now().date()
    daily_labels = []
    daily_values = []
    for i in range(29, -1, -1):
        day = today - timedelta(days=i)
        daily_labels.append(day.strftime('%d %b'))
        # Use DailyConsumptionSummary if available, else fallback to raw aggregation
        day_sum = 0
        try:
            day_sum = DailyConsumptionSummary.objects.filter(date=day).aggregate(
                total=Sum('net_consumption'))['total'] or 0
        except Exception:
            day_sum = 0

        if not day_sum:
            try:
                day_start = timezone.make_aware(datetime.combine(day, datetime.min.time()))
                day_end = timezone.make_aware(datetime.combine(day, datetime.max.time()))
                day_sum = StockTransaction.objects.filter(
                    transaction_type__in=['SALE', 'DISPENSE'],
                    created_at__gte=day_start,
                    created_at__lte=day_end,
                ).aggregate(total=Sum('quantity'))['total'] or 0
            except Exception:
                day_sum = 0

        daily_values.append(day_sum)

    # Stockout risk counts
    critical_risk = sum(1 for r in analytics_list if r.get('stockout_risk') == 'CRITICAL')
    high_risk = sum(1 for r in analytics_list if r.get('stockout_risk') == 'HIGH')

    context = {
        'page_obj': page_obj,
        'summary': summary,
        'fast_movers': fast_movers,
        'slow_movers': slow_movers,
        'categories': categories,
        'search_q': search_q,
        'category_filter': category_filter,
        'dq_filter': dq_filter,
        'trend_filter': trend_filter,
        'total_analyzed': len(analytics_list),
        'daily_labels': json.dumps(daily_labels),
        'daily_values': json.dumps(daily_values),
        'critical_risk_count': critical_risk,
        'high_risk_count': high_risk,
    }
    return render(request, 'inventory/consumption_analytics.html', context)



@admin_required
def historical_import_view(request):
    """
    Historical Consumption Data Import (Admin Only).
    Workflow: Upload → Validate → Preview → Confirm → Import with provenance log.
    NEVER modifies current physical stock.
    Uses HistoricalImportLog for full import audit trail.
    """
    preview_data = None
    recent_imports = HistoricalImportLog.objects.select_related('uploaded_by').order_by('-uploaded_at')[:10]

    if request.method == 'POST':
        action = request.POST.get('action', 'preview')

        if action == 'preview':
            upload_file = request.FILES.get('import_file')
            if not upload_file:
                messages.error(request, "Please select a CSV or Excel file to upload.")
            else:
                try:
                    raw_rows = historical_import.parse_import_file(upload_file, upload_file.name)
                    preview_data = historical_import.validate_and_preview_import(raw_rows)
                    request.session['historical_import_rows'] = preview_data['preview_rows']
                    request.session['historical_import_filename'] = upload_file.name
                    if preview_data['valid_count'] > 0:
                        messages.success(
                            request,
                            f"File validated: {preview_data['valid_count']} valid rows, "
                            f"{preview_data['error_count']} errors, "
                            f"{preview_data.get('duplicate_count', 0)} duplicates skipped."
                        )
                    else:
                        messages.warning(request, "No valid rows found in the uploaded file.")
                except Exception as e:
                    messages.error(request, f"Error processing file: {str(e)}")

        elif action == 'confirm':
            validated_rows = request.session.get('historical_import_rows', [])
            filename = request.session.get('historical_import_filename', 'upload')
            if not validated_rows:
                messages.error(request, "No validated import session found. Please upload again.")
                return redirect('inventory_historical_import')

            try:
                # Rebuild preview_data for log creation
                mock_preview = {
                    'total_rows': len(validated_rows),
                    'valid_count': len(validated_rows),
                    'error_count': 0,
                    'errors': [],
                }
                import_log = historical_import.create_import_log(filename, request.user, mock_preview)
                result = historical_import.execute_historical_import(
                    validated_rows, request.user, filename=filename, import_log=import_log
                )
                request.session.pop('historical_import_rows', None)
                request.session.pop('historical_import_filename', None)
                messages.success(
                    request,
                    f"Import complete: {result['created']} records imported, "
                    f"{result['skipped']} duplicates skipped."
                )
                return redirect('inventory_consumption_analytics')
            except Exception as e:
                messages.error(request, f"Import failed: {str(e)}")

    return render(request, 'inventory/historical_import.html', {
        'preview_data': preview_data,
        'recent_imports': recent_imports,
    })


@admin_or_pharmacist_required
def reorder_recommendations_view(request):
    """
    AI-powered reorder recommendations page.
    Shows all medicines needing replenishment with stockout risk, reorder qty,
    supplier lead time, and pending PO awareness.
    Admin approval required before any PO is created.
    """
    import json

    category_filter = request.GET.get('category', '')
    risk_filter = request.GET.get('risk', '')
    search_q = request.GET.get('search', '').strip()

    try:
        recommendations = ai_intel.get_reorder_recommendations()
    except Exception:
        recommendations = []

    # Enrich with stockout risk
    enriched = []
    for rec in recommendations:
        med = rec.get('medicine')
        if not med:
            continue
        if search_q and search_q.lower() not in med.medicine_name.lower():
            continue
        if category_filter and str(getattr(med, 'category_id', '')) != category_filter:
            continue
        try:
            risk = consumption_service.get_stockout_risk(med)
            rec['stockout_risk'] = risk['risk_level']
            rec['days_until_stockout'] = risk.get('days_until_stockout')
            rec['estimated_stockout_date'] = risk.get('estimated_stockout_date')
            rec['stockout_risk_factors'] = risk.get('risk_factors', [])
            rec['daily_rate'] = risk.get('daily_rate', 0)
        except Exception:
            rec['stockout_risk'] = 'NO_DATA'
            rec['days_until_stockout'] = None
            rec['estimated_stockout_date'] = None
            rec['stockout_risk_factors'] = []
            rec['daily_rate'] = 0

        if risk_filter and rec.get('stockout_risk') != risk_filter:
            continue
        enriched.append(rec)

    # Sort: CRITICAL first, then HIGH, then MEDIUM
    risk_order = {'CRITICAL': 0, 'HIGH': 1, 'MEDIUM': 2, 'LOW': 3, 'NO_DATA': 4}
    enriched.sort(key=lambda x: risk_order.get(x.get('stockout_risk', 'NO_DATA'), 5))

    paginator = Paginator(enriched, 20)
    page_obj = paginator.get_page(request.GET.get('page'))
    categories = Category.objects.filter(is_active=True)

    context = {
        'page_obj': page_obj,
        'total_recommendations': len(enriched),
        'categories': categories,
        'category_filter': category_filter,
        'risk_filter': risk_filter,
        'search_q': search_q,
        'critical_count': sum(1 for r in enriched if r.get('stockout_risk') == 'CRITICAL'),
        'high_count': sum(1 for r in enriched if r.get('stockout_risk') == 'HIGH'),
    }
    return render(request, 'inventory/reorder_recommendations.html', context)


@admin_or_pharmacist_required
def medicine_consumption_api(request, pk):
    """
    JSON API: returns full consumption analytics + multi-horizon forecast + stockout risk
    for a single medicine. Used by AJAX calls in medicine detail page.
    """
    from django.http import JsonResponse
    medicine = get_object_or_404(Medicine, pk=pk)

    try:
        analytics = consumption_service.get_consumption_analytics(medicine)
        forecast = ai_intel.get_demand_forecast_multi_horizon(medicine)
        risk = consumption_service.get_stockout_risk(medicine)
        trend_data = consumption_service.get_daily_consumption_trend(medicine, 30)

        # Serialise dates for JSON
        for key in ['estimated_stockout_date']:
            if risk.get(key):
                risk[key] = risk[key].isoformat()

        analytics['last_movement_date'] = (
            analytics['last_movement_date'].isoformat()
            if analytics.get('last_movement_date') else None
        )
        forecast['cutoff_date'] = (
            forecast['cutoff_date'].isoformat()
            if forecast.get('cutoff_date') else None
        )
        forecast['training_cutoff'] = (
            forecast['training_cutoff'].isoformat()
            if forecast.get('training_cutoff') else None
        )

        return JsonResponse({
            'ok': True,
            'analytics': analytics,
            'forecast': forecast,
            'stockout_risk': risk,
            'trend': trend_data,
        })
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=500)


@admin_or_pharmacist_required
def inventory_optimization_api(request, pk):
    """
    JSON API: returns the full evaluate_inventory_optimization() result for a single medicine.
    Used by AJAX calls. Serializes Decimal, date, and model instance fields safely.
    """
    from decimal import Decimal as D
    medicine = get_object_or_404(Medicine, pk=pk)

    try:
        data = consumption_service.evaluate_inventory_optimization(medicine)

        def _safe(v):
            if isinstance(v, D):
                return float(v)
            if hasattr(v, 'isoformat'):
                return v.isoformat()
            if hasattr(v, 'medicine_name'):   # Supplier or Medicine model
                return str(v)
            if isinstance(v, dict):
                return {kk: _safe(vv) for kk, vv in v.items()}
            if isinstance(v, list):
                return [_safe(i) for i in v]
            return v

        serialized = {k: _safe(v) for k, v in data.items() if k != 'medicine'}

        # Recommended supplier details
        rec_sup = data.get('recommended_supplier')
        if rec_sup:
            serialized['recommended_supplier'] = {
                'id': rec_sup.pk,
                'company_name': rec_sup.company_name,
            }

        return JsonResponse({'ok': True, 'optimization': serialized})
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=500)

