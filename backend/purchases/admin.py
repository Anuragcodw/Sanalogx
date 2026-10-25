from django.contrib import admin
from .models import PurchaseOrder, PurchaseOrderItem, GoodsReceipt, GoodsReceiptItem


class PurchaseOrderItemInline(admin.TabularInline):
    model = PurchaseOrderItem
    extra = 1
    fields = ['medicine', 'quantity_ordered', 'quantity_received', 'quantity_pending', 'unit_price', 'total_price']
    readonly_fields = ['total_price', 'quantity_pending']


class GoodsReceiptItemInline(admin.TabularInline):
    model = GoodsReceiptItem
    extra = 1
    fields = ['medicine', 'batch_number', 'expiry_date', 'quantity_received',
              'quantity_accepted', 'quantity_rejected', 'unit_price', 'medicine_batch']
    readonly_fields = ['medicine_batch']


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = [
        'po_number', 'supplier', 'status', 'priority',
        'order_date', 'expected_delivery_date', 'total_amount',
        'created_by', 'approved_by', 'created_at'
    ]
    list_filter = ['status', 'priority', 'supplier']
    search_fields = ['po_number', 'supplier__company_name']
    readonly_fields = ['po_number', 'created_at', 'updated_at', 'approved_at']
    inlines = [PurchaseOrderItemInline]
    fieldsets = (
        ('Order Details', {
            'fields': ('po_number', 'supplier', 'status', 'priority')
        }),
        ('Dates', {
            'fields': ('order_date', 'expected_delivery_date', 'actual_delivery_date')
        }),
        ('Financials', {
            'fields': ('subtotal', 'tax_amount', 'discount_amount', 'shipping_cost', 'total_amount')
        }),
        ('Workflow', {
            'fields': ('created_by', 'approved_by', 'approved_at', 'cancelled_by',
                       'cancellation_reason', 'triggered_by_alert')
        }),
        ('Notes', {
            'fields': ('notes', 'terms_and_conditions', 'created_at', 'updated_at')
        }),
    )


@admin.register(GoodsReceipt)
class GoodsReceiptAdmin(admin.ModelAdmin):
    list_display = [
        'grn_number', 'purchase_order', 'supplier', 'receipt_date',
        'status', 'invoice_number', 'received_by', 'verified_by', 'created_at'
    ]
    list_filter = ['status', 'receipt_date']
    search_fields = ['grn_number', 'invoice_number', 'purchase_order__po_number']
    readonly_fields = ['grn_number', 'created_at', 'updated_at', 'verified_at']
    inlines = [GoodsReceiptItemInline]
