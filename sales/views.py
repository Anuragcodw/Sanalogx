"""
sales/views.py
Phase 5 — Sales, Billing, History, Returns, and Analytics Views
"""

import json
from decimal import Decimal
from datetime import timedelta

from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.http import JsonResponse
from django.db.models import Sum, Count, Q, Avg, F, ExpressionWrapper, DecimalField
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator

from inventory.views import admin_required, admin_or_pharmacist_required
from medicines.models import Medicine, MedicineBatch
from inventory.models import Inventory, StockTransaction, StockAlert
from .models import Sale, SaleItem, SaleReturn, SaleReturnItem
from . import services


@admin_or_pharmacist_required
def new_sale_view(request):
    """
    Pharmacist POS Workspace:
    - Search & barcode scan medicine
    - Multi-medicine cart management
    - Real-time stock & FEFO batch availability
    - Live billing math (Subtotal, GST, Discount, Total)
    """
    medicines = Medicine.objects.filter(status='ACTIVE').select_related('category').prefetch_related('batches')

    # Build active stock and batch data map for JS
    med_data = []
    today = timezone.now().date()

    for med in medicines:
        inv = getattr(med, 'inventory', None)
        avail_stock = inv.available_stock if inv else 0
        if avail_stock <= 0:
            continue

        # Get valid non-expired batches in FEFO order
        active_batches = med.batches.filter(
            quantity_available__gt=0,
            expiry_date__gt=today
        ).exclude(status__in=['EXPIRED', 'QUARANTINED', 'RETURNED']).order_by('expiry_date')

        first_batch = active_batches.first()

        med_data.append({
            'id': med.id,
            'medicine_name': med.medicine_name,
            'generic_name': med.generic_name,
            'dosage_form': med.dosage_form,
            'strength': med.strength,
            'barcode': med.barcode or '',
            'available_stock': avail_stock,
            'unit_price': float(first_batch.selling_price) if first_batch else 0.0,
            'batch_number': first_batch.batch_number if first_batch else '',
            'expiry_date': str(first_batch.expiry_date) if first_batch else '',
        })

    context = {
        'medicines': medicines,
        'med_data_json': json.dumps(med_data),
        'payment_methods': Sale.PaymentMethod.choices,
    }
    return render(request, 'sales/new_sale.html', context)


@admin_or_pharmacist_required
def barcode_scan_sale_api(request):
    """
    AJAX API: Scan Barcode / QR to identify medicine/batch.
    IDENTIFICATION ONLY — DOES NOT DEDUCT STOCK.
    Returns rich medicine details, unit price, available stock, and FEFO batch details.
    """
    code = request.GET.get('code', '').strip()
    if not code:
        return JsonResponse({'success': False, 'error': 'Barcode value is required.'})

    today = timezone.now().date()

    # Search medicine barcode first
    med = Medicine.objects.filter(Q(barcode__iexact=code) | Q(qr_code__iexact=code)).select_related('category').first()

    if med and med.status != Medicine.Status.ACTIVE:
        return JsonResponse({
            'success': False,
            'status_code': 'INACTIVE',
            'error': f"{med.medicine_name} is marked INACTIVE in the master catalogue."
        })

    batch = None
    if not med:
        # Search batch by batch number
        batch = MedicineBatch.objects.filter(batch_number__iexact=code).select_related('medicine', 'medicine__category').first()
        if batch:
            med = batch.medicine
            if batch.status == MedicineBatch.BatchStatus.QUARANTINED:
                return JsonResponse({
                    'success': False,
                    'status_code': 'QUARANTINED',
                    'error': f"Batch '{batch.batch_number}' is QUARANTINED and cannot be sold."
                })
            if batch.expiry_date <= today or batch.status == MedicineBatch.BatchStatus.EXPIRED:
                return JsonResponse({
                    'success': False,
                    'status_code': 'EXPIRED',
                    'error': f"Batch '{batch.batch_number}' expired on {batch.expiry_date.strftime('%d %b %Y')} (EXPIRED). Sale is blocked."
                })

    if not med:
        return JsonResponse({
            'success': False,
            'status_code': 'NOT_FOUND',
            'error': f"Scanned code '{code}' does not match any medicine or batch. Ensure it is registered in the Medicine Master."
        })

    inv = getattr(med, 'inventory', None)
    avail_stock = inv.available_stock if inv else 0

    if avail_stock <= 0:
        return JsonResponse({
            'success': False,
            'status_code': 'OUT_OF_STOCK',
            'error': f"{med.medicine_name} currently has ZERO available stock."
        })

    # FEFO Batch
    fefo_batch = batch if (batch and batch.quantity_available > 0 and batch.expiry_date > today) else None
    if not fefo_batch:
        fefo_batch = med.batches.filter(
            quantity_available__gt=0, expiry_date__gt=today
        ).exclude(status__in=['EXPIRED', 'QUARANTINED', 'RETURNED']).order_by('expiry_date').first()

    if not fefo_batch:
        return JsonResponse({
            'success': False,
            'status_code': 'NO_VALID_BATCH',
            'error': f"No non-expired active batches found for {med.medicine_name}."
        })

    return JsonResponse({
        'success': True,
        'medicine_id': med.id,
        'medicine_name': med.medicine_name,
        'generic_name': med.generic_name or '',
        'brand_name': med.brand_name or '',
        'strength': med.strength,
        'dosage_form': med.dosage_form,
        'manufacturer': med.manufacturer or 'N/A',
        'category': med.category.name if med.category else 'General',
        'barcode': med.barcode or '',
        'available_stock': avail_stock,
        'unit_price': float(fefo_batch.selling_price),
        'batch_number': fefo_batch.batch_number,
        'expiry_date': str(fefo_batch.expiry_date),
    })


@admin_or_pharmacist_required
def checkout_sale_api(request):
    """
    AJAX POST API: Atomic Sale Checkout & Stock Deduction.
    Processes cart JSON, executes FEFO allocation, deducts stock via services.create_sale_from_cart(),
    and returns JSON redirect to generated invoice.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed.'}, status=405)

    try:
        data = json.loads(request.body)
        idempotency_key = (data.get('idempotency_key') or '').strip()
        if idempotency_key:
            from django.core.cache import cache
            cached_res = cache.get(f"sale_idemp_{idempotency_key}")
            if cached_res:
                return JsonResponse(cached_res)

        patient_name = data.get('patient_name', 'Walk-in Customer').strip()
        patient_id = data.get('patient_id', '').strip()
        mobile_number = data.get('mobile_number', '').strip()
        doctor_name = data.get('doctor_name', '').strip()
        prescription_number = data.get('prescription_number', '').strip()
        payment_method = data.get('payment_method', Sale.PaymentMethod.CASH)
        discount_amount = Decimal(str(data.get('discount_amount', 0)))
        tax_amount = Decimal(str(data.get('tax_amount', 0)))
        notes = data.get('notes', '').strip()
        cart_items = data.get('cart_items', [])

        sale = services.create_sale_from_cart(
            patient_name=patient_name,
            patient_id=patient_id,
            mobile_number=mobile_number,
            doctor_name=doctor_name,
            prescription_number=prescription_number,
            cart_items=cart_items,
            payment_method=payment_method,
            discount_amount=discount_amount,
            tax_amount=tax_amount,
            notes=notes,
            pharmacist_user=request.user
        )

        messages.success(request, f"Sale {sale.sale_number} completed. Invoice {sale.invoice_number} generated.")
        response_payload = {
            'success': True,
            'sale_number': sale.sale_number,
            'invoice_number': sale.invoice_number,
            'total_amount': float(sale.total_amount),
            'redirect_url': f"/sales/{sale.id}/invoice/"
        }
        if idempotency_key:
            from django.core.cache import cache
            cache.set(f"sale_idemp_{idempotency_key}", response_payload, timeout=120)

        return JsonResponse(response_payload)

    except (ValidationError, Exception) as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)


@admin_or_pharmacist_required
def sales_history_view(request):
    """Filterable sales log (Date, Pharmacist, Payment Status, Customer)."""
    sales_qs = Sale.objects.select_related('sold_by').prefetch_related('items__medicine').order_by('-sale_date')

    status_filter = request.GET.get('status', '')
    payment_filter = request.GET.get('payment_method', '')
    search_q = request.GET.get('search', '').strip()
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    if status_filter:
        sales_qs = sales_qs.filter(status=status_filter)
    if payment_filter:
        sales_qs = sales_qs.filter(payment_method=payment_filter)
    if search_q:
        sales_qs = sales_qs.filter(
            Q(sale_number__icontains=search_q) |
            Q(invoice_number__icontains=search_q) |
            Q(patient_name__icontains=search_q) |
            Q(patient_id__icontains=search_q)
        )
    if date_from:
        sales_qs = sales_qs.filter(sale_date__date__gte=date_from)
    if date_to:
        sales_qs = sales_qs.filter(sale_date__date__lte=date_to)

    # Metrics (strictly database-derived)
    today = timezone.now().date()
    today_sales = Sale.objects.filter(sale_date__date=today, status=Sale.SaleStatus.COMPLETED)
    today_revenue = today_sales.aggregate(s=Sum('total_amount'))['s'] or Decimal('0.00')

    paginator = Paginator(sales_qs, 25)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        'sales': page_obj,
        'page_obj': page_obj,
        'total_sales_count': Sale.objects.count(),
        'filtered_sales_count': sales_qs.count(),
        'today_sales_count': today_sales.count(),
        'today_revenue': today_revenue,
        'status_filter': status_filter,
        'payment_filter': payment_filter,
        'search_q': search_q,
        'date_from': date_from,
        'date_to': date_to,
        'payment_methods': Sale.PaymentMethod.choices,
        'statuses': Sale.SaleStatus.choices,
    }
    return render(request, 'sales/sales_history.html', context)


@admin_or_pharmacist_required
def sale_detail_view(request, pk):
    """Detailed view of a single Sale with line items, cost basis, and FEFO allocations."""
    sale = get_object_or_404(Sale.objects.select_related('sold_by'), pk=pk)
    items = sale.items.select_related('medicine', 'batch').all()
    returns = sale.returns.prefetch_related('items__sale_item__medicine').all()

    context = {
        'sale': sale,
        'items': items,
        'returns': returns,
        'total_cost': sale.total_cost,
        'total_profit': sale.total_profit,
    }
    return render(request, 'sales/sale_detail.html', context)


@admin_or_pharmacist_required
def sale_invoice_view(request, pk):
    """Printable & Downloadable Sale Invoice page (INV-2026-XXXXX)."""
    sale = get_object_or_404(Sale.objects.select_related('sold_by'), pk=pk)
    items = sale.items.select_related('medicine', 'batch').all()

    context = {
        'sale': sale,
        'items': items,
        'company_name': "Sanalogx Hospital Pharmacy",
        'address': "Central Hospital Complex, Main Road",
        'gst_number': "27AAACM9942R1Z5",
        'phone': "+91 98765 43210",
    }
    return render(request, 'sales/sale_invoice.html', context)


@admin_or_pharmacist_required
def sale_return_view(request, pk):
    """Pharmacist view: Process sale return / refund and restore stock."""
    sale = get_object_or_404(Sale, pk=pk)
    items = sale.items.select_related('medicine', 'batch').all()

    if request.method == 'POST':
        reason = request.POST.get('reason', 'Customer Return').strip()
        notes = request.POST.get('notes', '').strip()

        return_items = []
        for item in items:
            qty_str = request.POST.get(f'return_qty_{item.id}', '0')
            ret_qty = int(qty_str)
            if ret_qty > 0:
                return_items.append({'item_id': item.id, 'quantity': ret_qty})

        try:
            if not return_items:
                raise ValidationError("Please specify return quantity for at least one medicine line item.")

            sale_return = services.process_sale_return(
                sale_id=sale.pk,
                return_items_data=return_items,
                reason=reason,
                notes=notes,
                pharmacist_user=request.user
            )
            messages.success(request, f"Return {sale_return.return_number} processed. Refund amount: ₹{sale_return.refund_amount}. Stock restored.")
            return redirect('sale_detail', pk=sale.pk)

        except (ValidationError, Exception) as exc:
            messages.error(request, str(exc))

    context = {
        'sale': sale,
        'items': items,
    }
    return render(request, 'sales/sale_return.html', context)


@admin_required
def sales_analytics_view(request):
    """
    Admin View: Sales & Profit Analytics.
    Calculates Revenue, Net COGS, Gross Profit, Daily Trends, and Top Selling Medicines from MySQL data.
    """
    period = request.GET.get('period', '30d')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    from .financial_service import get_financial_summary
    fin = get_financial_summary(period=period, date_from=date_from, date_to=date_to)

    context = {
        'period': fin['period'],
        'period_label': fin['period_label'],
        'date_from': fin['date_from'],
        'date_to': fin['date_to'],
        'total_sales_count': fin['invoices_count'],
        'total_revenue': fin['total_invoice_amount'],
        'net_sales': fin['net_sales_excl_tax'],
        'total_cost': fin['cogs_total'],
        'total_profit': fin['gross_profit'],
        'gross_margin_pct': fin['gross_margin_pct'],
        'pending_payments': fin['pending_payments'],
        'total_discounts': fin['total_discounts'],
        'total_taxes': fin['total_taxes'],
        'refunds': fin['total_refund_amount'],
        'top_medicines': fin['top_by_revenue'],
        'top_profit_medicines': fin['top_by_profit'],
        'daily_labels_json': json.dumps(fin['chart_labels']),
        'daily_revenue_json': json.dumps(fin['chart_revenue']),
        'daily_profit_json': json.dumps(fin['chart_profit']),
        'daily_cogs_json': json.dumps(fin['chart_cogs']),
    }
    return render(request, 'sales/sales_analytics.html', context)


@admin_or_pharmacist_required
def cancel_sale_view(request, pk):
    """Cancel a sale invoice and safely reverse stock."""
    sale = get_object_or_404(Sale, pk=pk)

    if request.method == 'POST':
        reason = request.POST.get('cancellation_reason', 'Order Cancellation').strip() or 'Order Cancellation'
        try:
            services.cancel_sale(sale.pk, cancelled_by=request.user, reason=reason)
            messages.success(request, f"Invoice {sale.invoice_number} ({sale.sale_number}) cancelled. Stock has been safely restored.")
            return redirect('sale_detail', pk=sale.pk)
        except (ValidationError, Exception) as exc:
            messages.error(request, f"Failed to cancel invoice: {exc}")
            return redirect('sale_detail', pk=sale.pk)

    return redirect('sale_detail', pk=sale.pk)


@admin_or_pharmacist_required
def financial_summary_api(request):
    """JSON API: Returns comprehensive financial and profit KPIs for AJAX charts & cards."""
    period = request.GET.get('period', '30d')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    from .financial_service import get_financial_summary
    fin = get_financial_summary(period=period, date_from=date_from, date_to=date_to)

    serializable = {
        'success': True,
        'period': fin['period'],
        'period_label': fin['period_label'],
        'date_from': fin['date_from'],
        'date_to': fin['date_to'],
        'today_sales_count': fin['today_sales_count'],
        'today_gross_sales': float(fin['today_gross_sales']),
        'today_gross_profit': float(fin['today_gross_profit']),
        'today_cogs': float(fin['today_cogs']),
        'invoices_count': fin['invoices_count'],
        'cancelled_count': fin.get('cancelled_count', 0),
        'total_invoice_amount': float(fin['total_invoice_amount']),
        'net_sales_excl_tax': float(fin['net_sales_excl_tax']),
        'gross_subtotal': float(fin['gross_subtotal']),
        'total_discounts': float(fin['total_discounts']),
        'total_taxes': float(fin['total_taxes']),
        'total_paid': float(fin['total_paid']),
        'pending_payments': float(fin['pending_payments']),
        'returns_count': fin['returns_count'],
        'total_refund_amount': float(fin['total_refund_amount']),
        'cogs_total': float(fin['cogs_total']),
        'gross_profit': float(fin['gross_profit']),
        'gross_margin_pct': float(fin['gross_margin_pct']),
        'total_purchases_amount': float(fin['total_purchases_amount']),
        'chart_labels': fin['chart_labels'],
        'chart_revenue': fin['chart_revenue'],
        'chart_cogs': fin['chart_cogs'],
        'chart_profit': fin['chart_profit'],
    }
    return JsonResponse(serializable)
