"""
reports/views.py
Phase 7 — Reports Center, Management Dashboard, Export, QR Verification, Report History
"""

import json
from datetime import date, timedelta
from decimal import Decimal

from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from django.db.models import Sum, Count, Q, Avg
from django.contrib.auth.decorators import login_required
from django.core.mail import EmailMessage
from django.conf import settings

from inventory.views import admin_or_pharmacist_required, admin_required
from inventory.models import Inventory, StockTransaction, StockAlert
from medicines.models import Medicine, MedicineBatch
from sales.models import Sale, SaleItem
from purchases.models import PurchaseOrder
from suppliers.models import Supplier
from accounts.models import User

from . import report_engine, csv_service
from .models import ReportRecord, ReportSchedule


# ─────────────────────────────────────────────────────────────────────────────
# HELPER: Parse date range from request
# ─────────────────────────────────────────────────────────────────────────────

def _parse_date_range(request):
    period = request.GET.get('period') or request.POST.get('period', '30d')
    custom_from = request.GET.get('date_from') or request.POST.get('date_from')
    custom_to = request.GET.get('date_to') or request.POST.get('date_to')

    try:
        from analytics.date_service import resolve_date_range
        resolved = resolve_date_range(period=period, date_from=custom_from, date_to=custom_to)
        return resolved['date_from'], resolved['date_to']
    except Exception:
        today = timezone.now().date()
        return today - timedelta(days=30), today


# ─────────────────────────────────────────────────────────────────────────────
# MANAGEMENT ANALYTICS DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

@admin_or_pharmacist_required
def management_dashboard_view(request):
    """
    Real-time production management analytics dashboard.
    Backed strictly by MySQL records through financial_service and valuation_service.
    """
    from sales.financial_service import get_financial_summary
    from inventory.valuation_service import get_inventory_valuation

    period = request.GET.get('period', '30d')
    custom_from = request.GET.get('date_from', '')
    custom_to = request.GET.get('date_to', '')

    today = timezone.now().date()

    # 1. Rigorous Financial Summary (Sales, Revenue, Taxes, COGS, Gross Profit, Pending, Returns)
    fin = get_financial_summary(period=period, date_from=custom_from, date_to=custom_to)

    # 2. Authoritative Batch-Wise Inventory Valuation
    val = get_inventory_valuation()

    # 3. Inventory Stock Counts
    inventories = Inventory.objects.all()
    total_medicines = Medicine.objects.filter(status='ACTIVE').count()
    low_stock_count = inventories.filter(stock_status='LOW_STOCK').count()
    out_of_stock_count = inventories.filter(stock_status='OUT_OF_STOCK').count()
    overstock_count = inventories.filter(stock_status='OVERSTOCK').count()

    # 4. Notifications count
    from notifications.models import Notification
    unread_count = Notification.objects.filter(
        Q(recipient=request.user) | Q(recipient__isnull=True),
        is_read=False
    ).count()

    context = {
        # Inventory Snapshot & Valuation
        'total_medicines': total_medicines,
        'low_stock_count': low_stock_count,
        'out_of_stock_count': out_of_stock_count,
        'overstock_count': overstock_count,
        'near_expiry_count': val['near_expiry_units'],
        'expired_count': val['expired_units'],
        'inventory_value': round(val['usable_cost_value'], 2),
        'retail_potential_value': round(val['usable_retail_value'], 2),
        'unrealized_margin': round(val['unrealized_margin'], 2),
        'unrealized_margin_pct': val['unrealized_margin_pct'],
        'expired_stock_value': round(val['expired_cost_value'], 2),
        'quarantined_stock_value': round(val['quarantined_cost_value'], 2),
        'category_breakdown': val['category_breakdown'],

        # Today's Snapshot
        'today_sales_count': fin['today_sales_count'],
        'today_revenue': round(fin['today_gross_sales'], 2),
        'today_profit': round(fin['today_gross_profit'], 2),
        'today_cogs': round(fin['today_cogs'], 2),

        # Period Financials
        'period_revenue': round(fin['total_invoice_amount'], 2),
        'period_net_sales': round(fin['net_sales_excl_tax'], 2),
        'period_gross_sales': round(fin['gross_subtotal'], 2),
        'period_discounts': round(fin['total_discounts'], 2),
        'period_taxes': round(fin['total_taxes'], 2),
        'period_paid': round(fin['total_paid'], 2),
        'pending_payments': round(fin['pending_payments'], 2),
        'period_returns_count': fin['returns_count'],
        'period_refunds': round(fin['total_refund_amount'], 2),
        'period_cogs': round(fin['cogs_total'], 2),
        'period_profit': round(fin['gross_profit'], 2),
        'gross_margin_pct': fin['gross_margin_pct'],

        # Purchases
        'total_purchases': round(fin['total_purchases_amount'], 2),
        'pending_orders': fin['pending_purchase_orders'],

        # Period Filter & Dates
        'period': fin['period'],
        'period_label': fin['period_label'],
        'date_from': fin['date_from'],
        'date_to': fin['date_to'],

        # Responsive Multi-Series Charts
        'chart_labels': json.dumps(fin['chart_labels']),
        'chart_revenue': json.dumps(fin['chart_revenue']),
        'chart_cogs': json.dumps(fin['chart_cogs']),
        'chart_profit': json.dumps(fin['chart_profit']),
        'top_meds': fin['top_by_revenue'],
        'top_profit_meds': fin['top_by_profit'],

        # Staff Context
        'unread_notifications': unread_count,
        'user_role': request.user.role,
    }
    return render(request, 'reports/dashboard.html', context)


# ─────────────────────────────────────────────────────────────────────────────
# REPORT CENTER
# ─────────────────────────────────────────────────────────────────────────────

@admin_or_pharmacist_required
def reports_center_view(request):
    """Report center — lists all available report types with Generate buttons."""
    recent_reports = ReportRecord.objects.filter(
        generated_by=request.user
    ).order_by('-generated_at')[:10]

    report_types = [
        {'key': 'INVENTORY', 'name': 'Inventory Report', 'icon': 'fas fa-boxes-stacked', 'desc': 'Current stock levels, inventory value, status', 'color': 'teal'},
        {'key': 'SALES', 'name': 'Sales Report', 'icon': 'fas fa-cash-register', 'desc': 'Sales transactions, revenue, payment methods', 'color': 'purple'},
        {'key': 'PURCHASE', 'name': 'Purchase Report', 'icon': 'fas fa-file-invoice', 'desc': 'Purchase orders, supplier deliveries, costs', 'color': 'blue'},
        {'key': 'SUPPLIER', 'name': 'Supplier Report', 'icon': 'fas fa-building-flag', 'desc': 'Supplier performance, spend, delivery metrics', 'color': 'navy'},
        {'key': 'EXPIRY', 'name': 'Expiry Report', 'icon': 'fas fa-hourglass-half', 'desc': 'Near-expiry and expired medicine batches', 'color': 'warning'},
        {'key': 'LOW_STOCK', 'name': 'Low Stock Report', 'icon': 'fas fa-triangle-exclamation', 'desc': 'Below-reorder medicines with AI forecasts', 'color': 'danger'},
        {'key': 'PROFIT', 'name': 'Profit Report', 'icon': 'fas fa-chart-line', 'desc': 'Gross profit using actual batch cost basis', 'color': 'success'},
        {'key': 'STOCK_MOVEMENT', 'name': 'Stock Movement Report', 'icon': 'fas fa-arrows-up-down', 'desc': 'All inventory transactions audit trail', 'color': 'teal'},
        {'key': 'PRICE_HISTORY', 'name': 'Price History Report', 'icon': 'fas fa-tags', 'desc': 'Supplier price changes and trends', 'color': 'amber'},
        {'key': 'AI_PREDICTION', 'name': 'AI Prediction Report', 'icon': 'fas fa-brain', 'desc': 'ML demand forecasts and reorder recommendations', 'color': 'purple'},
        {'key': 'CONSUMPTION', 'name': 'Consumption Analytics Report', 'icon': 'fas fa-chart-pie', 'desc': 'Historical 7/14/30/90-day medicine consumption velocity', 'color': 'teal'},
        {'key': 'OPTIMIZATION', 'name': 'Inventory Optimization Report', 'icon': 'fas fa-sliders', 'desc': 'Days of stock, overstock, dead stock, and risk diagnostics', 'color': 'indigo'},
    ]

    # Admin-only report types
    if request.user.role == 'ADMIN':
        report_types.append({
            'key': 'CONTROLLED', 'name': 'Controlled Medicine Report',
            'icon': 'fas fa-shield-alt', 'desc': 'Scheduled H/H1/X medicine dispensing audit',
            'color': 'danger'
        })

    context = {
        'report_types': report_types,
        'recent_reports': recent_reports,
        'user_role': request.user.role,
    }
    return render(request, 'reports/report_center.html', context)


# ─────────────────────────────────────────────────────────────────────────────
# REPORT PREVIEW (AJAX POST)
# ─────────────────────────────────────────────────────────────────────────────

@admin_or_pharmacist_required
def report_preview_view(request):
    """
    Generate report preview data.
    POST or GET (with report_type): report_type, date_from, date_to, format, filters...
    Creates ReportRecord, returns preview context.
    """
    req_data = request.POST if request.method == 'POST' else request.GET
    report_type = req_data.get('report_type')
    if not report_type:
        return redirect('reports_center')

    period = req_data.get('period', '30d')
    fmt = req_data.get('format', 'PDF').upper()
    if fmt not in ['PDF', 'EXCEL', 'CSV']:
        fmt = 'PDF'

    date_from, date_to = _parse_date_range(request)
    filters = {
        'category_id': req_data.get('category_id', ''),
        'medicine_id': req_data.get('medicine_id', ''),
        'supplier_id': req_data.get('supplier_id', ''),
        'stock_status': req_data.get('stock_status', ''),
    }
    filters = {k: v for k, v in filters.items() if v}

    # Permission check: pharmacists cannot access admin-only reports
    admin_only_types = ['PROFIT', 'CONTROLLED', 'SUPPLIER', 'PRICE_HISTORY']
    if report_type in admin_only_types and request.user.role != 'ADMIN':
        return render(request, 'auth/403.html', status=403)

    # Get data
    engine_map = {
        'INVENTORY': report_engine.get_inventory_report_data,
        'SALES': report_engine.get_sales_report_data,
        'PURCHASE': report_engine.get_purchase_report_data,
        'EXPIRY': report_engine.get_expiry_report_data,
        'PROFIT': report_engine.get_profit_report_data,
        'LOW_STOCK': report_engine.get_low_stock_report_data,
        'STOCK_MOVEMENT': report_engine.get_stock_movement_report_data,
        'SUPPLIER': report_engine.get_supplier_report_data,
        'PRICE_HISTORY': report_engine.get_price_history_report_data,
        'AI_PREDICTION': report_engine.get_ai_prediction_report_data,
        'CONSUMPTION': report_engine.get_consumption_report_data,
        'OPTIMIZATION': report_engine.get_optimization_report_data,
        'DISPENSING': report_engine.get_dispensing_report_data,
        'CONTROLLED': report_engine.get_controlled_report_data,
    }

    engine_fn = engine_map.get(report_type, report_engine.get_inventory_report_data)
    report_data = engine_fn(date_from=date_from, date_to=date_to, filters=filters)

    # Create ReportRecord
    record = ReportRecord.objects.create(
        report_type=report_type,
        format=fmt,
        date_from=date_from,
        date_to=date_to,
        filters_json=filters,
        generated_by=request.user,
    )

    # Notify user
    try:
        from notifications.service import notify_report_generated
        notify_report_generated(request.user, record)
    except Exception:
        pass

    context = {
        'record': record,
        'report_data': report_data,
        'report_type': report_type,
        'report_type_display': record.get_report_type_display(),
        'date_from': date_from,
        'date_to': date_to,
        'period': period,
        'format': fmt,
        'summary': report_data.get('summary', {}),
        'data_rows': report_data.get('data', [])[:50],  # Preview: first 50 rows
        'total_rows': len(report_data.get('data', [])),
        'columns': report_data.get('columns', []),
        'user_role': request.user.role,
    }
    return render(request, 'reports/report_preview.html', context)


# ─────────────────────────────────────────────────────────────────────────────
# DOWNLOAD REPORT (PDF / EXCEL / CSV)
# ─────────────────────────────────────────────────────────────────────────────

@admin_or_pharmacist_required
def download_report_view(request, report_id, fmt):
    """Download a generated report as PDF, Excel, or CSV."""
    record = get_object_or_404(ReportRecord, report_id=report_id, generated_by=request.user)

    # Re-generate data from stored filters
    admin_only_types = ['PROFIT', 'CONTROLLED', 'SUPPLIER', 'PRICE_HISTORY']
    if record.report_type in admin_only_types and request.user.role != 'ADMIN':
        return render(request, 'auth/403.html', status=403)

    engine_map = {
        'INVENTORY': report_engine.get_inventory_report_data,
        'SALES': report_engine.get_sales_report_data,
        'PURCHASE': report_engine.get_purchase_report_data,
        'EXPIRY': report_engine.get_expiry_report_data,
        'PROFIT': report_engine.get_profit_report_data,
        'LOW_STOCK': report_engine.get_low_stock_report_data,
        'STOCK_MOVEMENT': report_engine.get_stock_movement_report_data,
        'SUPPLIER': report_engine.get_supplier_report_data,
        'PRICE_HISTORY': report_engine.get_price_history_report_data,
        'AI_PREDICTION': report_engine.get_ai_prediction_report_data,
        'CONSUMPTION': report_engine.get_consumption_report_data,
        'OPTIMIZATION': report_engine.get_optimization_report_data,
        'DISPENSING': report_engine.get_dispensing_report_data,
        'CONTROLLED': report_engine.get_controlled_report_data,
    }
    engine_fn = engine_map.get(record.report_type, report_engine.get_inventory_report_data)
    report_data = engine_fn(
        date_from=record.date_from,
        date_to=record.date_to,
        filters=record.filters_json,
    )

    fmt = fmt.upper()
    filename_base = f"Sanalogx_{record.report_type}_{record.report_id}"

    if fmt == 'PDF':
        from .pdf_service import generate_pdf
        pdf_bytes = generate_pdf(record, report_data)
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename_base}.pdf"'

    elif fmt == 'EXCEL':
        from .excel_service import generate_excel
        excel_bytes = generate_excel(record, report_data)
        response = HttpResponse(excel_bytes, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = f'attachment; filename="{filename_base}.xlsx"'

    elif fmt == 'CSV':
        csv_bytes = csv_service.generate_csv_bytes(report_data)
        response = HttpResponse(csv_bytes, content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename_base}.csv"'

    else:
        return HttpResponse('Invalid format', status=400)

    record.record_download()
    return response


# ─────────────────────────────────────────────────────────────────────────────
# REPORT HISTORY
# ─────────────────────────────────────────────────────────────────────────────

@admin_or_pharmacist_required
def report_history_view(request):
    """List all reports generated by the current user."""
    reports = ReportRecord.objects.filter(
        generated_by=request.user
    ).order_by('-generated_at')

    if request.user.role == 'ADMIN':
        reports = ReportRecord.objects.all().order_by('-generated_at')

    context = {
        'reports': reports[:100],
        'user_role': request.user.role,
    }
    return render(request, 'reports/report_history.html', context)


# ─────────────────────────────────────────────────────────────────────────────
# QR REPORT VERIFICATION (PUBLIC)
# ─────────────────────────────────────────────────────────────────────────────

def report_verify_view(request, token):
    """
    Public QR code verification endpoint.
    Exposes ONLY: Report ID, Type, Generated Date, Status.
    NEVER exposes confidential report data publicly.
    """
    try:
        record = ReportRecord.objects.get(verification_token=token)
        context = {
            'valid': True,
            'report_id': record.report_id,
            'report_type': record.get_report_type_display(),
            'generated_at': record.generated_at,
            'date_from': record.date_from,
            'date_to': record.date_to,
            'status': record.get_status_display(),
        }
    except ReportRecord.DoesNotExist:
        context = {'valid': False}

    return render(request, 'reports/report_verify.html', context)


# ─────────────────────────────────────────────────────────────────────────────
# EMAIL REPORT
# ─────────────────────────────────────────────────────────────────────────────

@admin_required
def email_report_view(request, report_id):
    """Send a generated report via email to an authorized recipient."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed.'}, status=405)

    record = get_object_or_404(ReportRecord, report_id=report_id, generated_by=request.user)
    recipient_email = request.POST.get('email', '').strip()
    fmt = request.POST.get('format', record.format).upper()

    if not recipient_email:
        return JsonResponse({'success': False, 'error': 'Recipient email required.'})

    try:
        # Re-generate data
        engine_map = {
            'INVENTORY': report_engine.get_inventory_report_data,
            'SALES': report_engine.get_sales_report_data,
            'PURCHASE': report_engine.get_purchase_report_data,
            'EXPIRY': report_engine.get_expiry_report_data,
            'PROFIT': report_engine.get_profit_report_data,
            'LOW_STOCK': report_engine.get_low_stock_report_data,
            'STOCK_MOVEMENT': report_engine.get_stock_movement_report_data,
            'SUPPLIER': report_engine.get_supplier_report_data,
            'PRICE_HISTORY': report_engine.get_price_history_report_data,
            'AI_PREDICTION': report_engine.get_ai_prediction_report_data,
            'CONSUMPTION': report_engine.get_consumption_report_data,
            'OPTIMIZATION': report_engine.get_optimization_report_data,
            'DISPENSING': report_engine.get_dispensing_report_data,
            'CONTROLLED': report_engine.get_controlled_report_data,
        }
        engine_fn = engine_map.get(record.report_type, report_engine.get_inventory_report_data)
        report_data = engine_fn(date_from=record.date_from, date_to=record.date_to, filters=record.filters_json)

        subject = f"Sanalogx Report: {record.get_report_type_display()} ({record.report_id})"
        body = (
            f"Please find attached the {record.get_report_type_display()}.\n\n"
            f"Report ID: {record.report_id}\n"
            f"Period: {record.date_from} to {record.date_to}\n"
            f"Generated: {record.generated_at.strftime('%d %b %Y %H:%M')}\n\n"
            f"Verify this report at: {record.get_verification_url()}\n\n"
            f"-- Sanalogx Pharmacy Management System"
        )

        from accounts.email_service import get_from_email
        email = EmailMessage(
            subject=subject, body=body,
            from_email=get_from_email(),
            to=[recipient_email],
        )

        filename_base = f"Sanalogx_{record.report_type}_{record.report_id}"

        if fmt == 'PDF':
            from .pdf_service import generate_pdf
            email.attach(f"{filename_base}.pdf", generate_pdf(record, report_data), 'application/pdf')
        elif fmt == 'EXCEL':
            from .excel_service import generate_excel
            email.attach(f"{filename_base}.xlsx", generate_excel(record, report_data),
                         'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        else:
            email.attach(f"{filename_base}.csv", csv_service.generate_csv_bytes(report_data), 'text/csv')

        email.send(fail_silently=False)

        record.emailed_to = recipient_email
        record.emailed_at = timezone.now()
        record.status = ReportRecord.ReportStatus.EMAILED
        record.save(update_fields=['emailed_to', 'emailed_at', 'status'])

        return JsonResponse({'success': True, 'message': f'Report emailed to {recipient_email} successfully.'})

    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


# ─────────────────────────────────────────────────────────────────────────────
# LEGACY: reports_dashboard_view (redirect to management dashboard)
# ─────────────────────────────────────────────────────────────────────────────

@admin_or_pharmacist_required
def reports_dashboard_view(request):
    """Legacy redirect to management dashboard."""
    return redirect('management_dashboard')
