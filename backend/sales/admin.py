from django.contrib import admin
from .models import Sale, SaleItem


class SaleItemInline(admin.TabularInline):
    model = SaleItem
    extra = 1
    fields = ['medicine', 'batch', 'quantity', 'unit_price', 'discount_pct', 'total_price', 'quantity_returned']
    readonly_fields = ['total_price']


@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    list_display = [
        'sale_number', 'patient_name', 'patient_id', 'doctor_name',
        'total_amount', 'payment_method', 'status', 'sold_by', 'sale_date'
    ]
    list_filter = ['status', 'payment_method', 'sale_date']
    search_fields = ['sale_number', 'patient_name', 'patient_id', 'doctor_name', 'prescription_number']
    readonly_fields = ['sale_number', 'sale_date', 'updated_at']
    inlines = [SaleItemInline]
    fieldsets = (
        ('Sale Reference', {
            'fields': ('sale_number', 'status')
        }),
        ('Patient Details', {
            'fields': ('patient_name', 'patient_id', 'doctor_name', 'prescription_number')
        }),
        ('Financials', {
            'fields': ('subtotal', 'discount_amount', 'tax_amount', 'total_amount', 'amount_paid', 'outstanding_amount', 'refund_amount', 'payment_method')
        }),
        ('Audit', {
            'fields': ('sold_by', 'notes', 'sale_date', 'updated_at')
        }),
    )
