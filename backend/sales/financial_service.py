"""
sales/financial_service.py
MediVault — Production Sales Revenue, Profit & Financial Calculation Engine

Guarantees:
- DecimalField and Decimal precision for all monetary calculations (No float money).
- Calculates Cost of Goods Sold (COGS) using actual batch cost allocated to each sold item.
- For FEFO multi-batch sales, calculates cost and profit separately for every batch allocation.
- Gross Profit = Net sales excluding collected statutory taxes, minus the corresponding COGS.
- Clearly labels metric as Gross Profit (as operating expenses/overheads are not tracked in inventory).
- Never uses latest purchase cost to recalculate historical sales.
- Supports all date filters: Today, Yesterday, 7 Days, 30 Days, This Month, Last Month, This Year, and Custom Range.
"""

from decimal import Decimal
from datetime import timedelta
from collections import defaultdict
from django.utils import timezone
from django.db.models import Sum, Count, Q

from sales.models import Sale, SaleItem, SaleReturn
from purchases.models import PurchaseOrder
from analytics.date_service import resolve_date_range


def get_financial_summary(period='30d', date_from=None, date_to=None):
    """
    Computes rigorous financial KPIs and metrics for management dashboards and reporting:
    1. Today's Metrics (Snapshot)
    2. Period Gross Revenue, Discounts, Taxes, and Net Sales
    3. Accurate Cost of Goods Sold (COGS) based on allocated batch unit costs
    4. Gross Profit & Gross Profit Margin %
    5. Pending Payments (unpaid / credit invoices)
    6. Returns & Refunds issued
    7. Purchase Orders Spend & Pending Orders
    8. Daily Trends (Revenue, COGS, Gross Profit) for charts
    9. Top Performing Medicines by Revenue and Gross Profit
    """
    range_info = resolve_date_range(period=period, date_from=date_from, date_to=date_to)
    start_dt = range_info['start_datetime']
    end_dt = range_info['end_datetime']
    today = range_info['as_of_date']

    # ── 1. TODAY'S SNAPSHOT ──────────────────────────────────────────────────
    today_sales_qs = Sale.objects.filter(
        sale_date__date=today,
        status__in=[Sale.SaleStatus.COMPLETED, Sale.SaleStatus.PARTIAL_RETURN]
    )
    today_sales_count = today_sales_qs.count()
    today_gross_sales = today_sales_qs.aggregate(s=Sum('total_amount'))['s'] or Decimal('0.00')

    # Today's net revenue and COGS
    today_items = SaleItem.objects.filter(sale__in=today_sales_qs)
    today_cogs = Decimal('0.00')
    today_taxable_rev = Decimal('0.00')
    for item in today_items:
        today_cogs += item.net_cost
        today_taxable_rev += item.net_taxable_amount
    today_gross_profit = max(Decimal('0.00'), today_taxable_rev - today_cogs)

    # ── 2. PERIOD SALES & REVENUE ─────────────────────────────────────────────
    period_sales = Sale.objects.filter(
        sale_date__gte=start_dt,
        sale_date__lte=end_dt
    )

    completed_sales = period_sales.filter(
        status__in=[Sale.SaleStatus.COMPLETED, Sale.SaleStatus.PARTIAL_RETURN]
    )

    cancelled_sales = period_sales.filter(status=Sale.SaleStatus.CANCELLED)

    total_invoices_count = completed_sales.count()
    cancelled_count = cancelled_sales.count()

    # Financial Aggregations
    gross_subtotal = completed_sales.aggregate(s=Sum('subtotal'))['s'] or Decimal('0.00')
    total_discounts = completed_sales.aggregate(s=Sum('discount_amount'))['s'] or Decimal('0.00')
    total_taxes = completed_sales.aggregate(s=Sum('tax_amount'))['s'] or Decimal('0.00')
    total_invoice_amount = completed_sales.aggregate(s=Sum('total_amount'))['s'] or Decimal('0.00')
    total_paid = completed_sales.aggregate(s=Sum('amount_paid'))['s'] or Decimal('0.00')
    pending_payments = completed_sales.aggregate(s=Sum('outstanding_amount'))['s'] or Decimal('0.00')

    # Period Returns & Refunds
    period_returns = SaleReturn.objects.filter(
        return_date__gte=start_dt,
        return_date__lte=end_dt
    )
    returns_count = period_returns.count()
    total_refund_amount = period_returns.aggregate(s=Sum('refund_amount'))['s'] or Decimal('0.00')
    tax_refund_amount = period_returns.aggregate(s=Sum('tax_refund_amount'))['s'] or Decimal('0.00')
    net_refund_excl_tax = max(Decimal('0.00'), total_refund_amount - tax_refund_amount)

    # Net Sales (Gross Invoice Amount minus Refunds)
    net_sales_gross = max(Decimal('0.00'), total_invoice_amount - total_refund_amount)

    # Net Sales Revenue (Pre-tax taxable sales minus pre-tax refunds)
    net_sales_excl_tax = max(Decimal('0.00'), (gross_subtotal - total_discounts) - net_refund_excl_tax)

    # ── 3. ACCURATE COST OF GOODS SOLD (COGS) & GROSS PROFIT ─────────────────
    completed_items = SaleItem.objects.filter(sale__in=completed_sales).select_related('medicine', 'batch')

    cogs_total = Decimal('0.00')
    med_profit_agg = defaultdict(lambda: {
        'medicine_name': '',
        'category': '',
        'quantity': 0,
        'revenue': Decimal('0.00'),
        'cogs': Decimal('0.00'),
        'gross_profit': Decimal('0.00')
    })

    for item in completed_items:
        item_cogs = item.net_cost
        item_rev = item.net_taxable_amount
        item_profit = item.gross_profit

        cogs_total += item_cogs

        m_id = item.medicine_id
        med_profit_agg[m_id]['medicine_name'] = item.medicine.medicine_name
        med_profit_agg[m_id]['category'] = item.medicine.category.name if item.medicine.category else 'General'
        med_profit_agg[m_id]['quantity'] += item.active_quantity
        med_profit_agg[m_id]['revenue'] += item_rev
        med_profit_agg[m_id]['cogs'] += item_cogs
        med_profit_agg[m_id]['gross_profit'] += item_profit

    gross_profit = max(Decimal('0.00'), net_sales_excl_tax - cogs_total)
    gross_margin_pct = round((gross_profit / net_sales_excl_tax) * Decimal('100.00'), 2) if net_sales_excl_tax > Decimal('0.00') else Decimal('0.00')

    # Top medicines by revenue and by gross profit
    sorted_by_rev = sorted(med_profit_agg.values(), key=lambda x: x['revenue'], reverse=True)[:8]
    sorted_by_profit = sorted(med_profit_agg.values(), key=lambda x: x['gross_profit'], reverse=True)[:8]

    # ── 4. PURCHASES IN PERIOD ───────────────────────────────────────────────
    po_qs = PurchaseOrder.objects.filter(
        created_at__gte=start_dt,
        created_at__lte=end_dt
    )
    total_purchases_amount = po_qs.aggregate(s=Sum('total_amount'))['s'] or Decimal('0.00')
    total_purchases_count = po_qs.count()
    pending_purchase_orders = PurchaseOrder.objects.filter(
        status__in=[PurchaseOrder.POStatus.DRAFT, PurchaseOrder.POStatus.SUBMITTED, PurchaseOrder.POStatus.APPROVED]
    ).count()

    # ── 5. DAILY REVENUE, COGS & PROFIT CHART SERIES ─────────────────────────
    days_count = range_info['days_count']
    chart_days = min(30, days_count)  # Up to 30 intervals for crisp visual rendering

    chart_labels = []
    chart_revenue = []
    chart_cogs = []
    chart_profit = []

    step = max(1, days_count // chart_days)
    current_day = range_info['date_from']

    while current_day <= range_info['date_to']:
        next_day = min(current_day + timedelta(days=step - 1), range_info['date_to'])
        label = current_day.strftime('%d %b') if current_day == next_day else f"{current_day.strftime('%d %b')}"

        day_sales = Sale.objects.filter(
            sale_date__date__gte=current_day,
            sale_date__date__lte=next_day,
            status__in=[Sale.SaleStatus.COMPLETED, Sale.SaleStatus.PARTIAL_RETURN]
        )
        day_items = SaleItem.objects.filter(sale__in=day_sales)

        day_rev = Decimal('0.00')
        day_cost = Decimal('0.00')
        for it in day_items:
            day_rev += it.net_taxable_amount
            day_cost += it.net_cost

        day_prof = max(Decimal('0.00'), day_rev - day_cost)

        chart_labels.append(label)
        chart_revenue.append(float(round(day_rev, 2)))
        chart_cogs.append(float(round(day_cost, 2)))
        chart_profit.append(float(round(day_prof, 2)))

        current_day = next_day + timedelta(days=1)

    return {
        'period': range_info['period'],
        'period_label': range_info['period_label'],
        'date_from': range_info['date_from'].isoformat(),
        'date_to': range_info['date_to'].isoformat(),
        # Today
        'today_sales_count': today_sales_count,
        'today_gross_sales': today_gross_sales,
        'today_cogs': today_cogs,
        'today_gross_profit': today_gross_profit,
        # Period Totals
        'invoices_count': total_invoices_count,
        'cancelled_count': cancelled_count,
        'gross_subtotal': gross_subtotal,
        'total_discounts': total_discounts,
        'total_taxes': total_taxes,
        'total_invoice_amount': total_invoice_amount,
        'total_paid': total_paid,
        'pending_payments': pending_payments,
        # Returns
        'returns_count': returns_count,
        'total_refund_amount': total_refund_amount,
        'net_refund_excl_tax': net_refund_excl_tax,
        # Net & Profit (Excluding Tax)
        'net_sales_gross': net_sales_gross,
        'net_sales_excl_tax': net_sales_excl_tax,
        'cogs_total': cogs_total,
        'gross_profit': gross_profit,
        'gross_margin_pct': gross_margin_pct,
        # Purchases
        'total_purchases_amount': total_purchases_amount,
        'total_purchases_count': total_purchases_count,
        'pending_purchase_orders': pending_purchase_orders,
        # Chart Data
        'chart_labels': chart_labels,
        'chart_revenue': chart_revenue,
        'chart_cogs': chart_cogs,
        'chart_profit': chart_profit,
        # Top Medicines
        'top_by_revenue': sorted_by_rev,
        'top_by_profit': sorted_by_profit,
    }
