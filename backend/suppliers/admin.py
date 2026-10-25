from django.contrib import admin
from .models import Supplier, SupplierMedicine, SupplierPriceHistory


class SupplierMedicineInline(admin.TabularInline):
    model = SupplierMedicine
    extra = 1
    fields = ['medicine', 'unit_price', 'delivery_days', 'minimum_order_quantity',
              'is_available', 'is_preferred', 'composite_score']
    readonly_fields = ['composite_score']


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = [
        'company_name', 'contact_person', 'email', 'phone',
        'rating', 'average_delivery_days', 'on_time_delivery_rate', 'status'
    ]
    list_filter = ['status', 'payment_terms', 'country']
    search_fields = ['company_name', 'contact_person', 'email', 'drug_license_number']
    readonly_fields = ['created_at', 'updated_at']
    inlines = [SupplierMedicineInline]
    fieldsets = (
        ('Company Information', {
            'fields': ('company_name', 'contact_person', 'email', 'phone', 'alternate_phone')
        }),
        ('Address', {
            'fields': ('address_line1', 'address_line2', 'city', 'state', 'country', 'postal_code')
        }),
        ('Business & Regulatory', {
            'fields': ('registration_number', 'drug_license_number', 'gst_number', 'payment_terms', 'minimum_order_value')
        }),
        ('Performance Metrics', {
            'fields': ('rating', 'average_delivery_days', 'on_time_delivery_rate', 'fill_rate')
        }),
        ('Status', {
            'fields': ('status', 'notes', 'created_by', 'created_at', 'updated_at')
        }),
    )


@admin.register(SupplierMedicine)
class SupplierMedicineAdmin(admin.ModelAdmin):
    list_display = [
        'supplier', 'medicine', 'unit_price', 'delivery_days',
        'minimum_order_quantity', 'is_available', 'is_preferred', 'composite_score'
    ]
    list_filter = ['is_available', 'is_preferred', 'supplier']
    search_fields = ['supplier__company_name', 'medicine__generic_name']
    readonly_fields = ['last_price_update', 'created_at']


@admin.register(SupplierPriceHistory)
class SupplierPriceHistoryAdmin(admin.ModelAdmin):
    list_display = [
        'supplier', 'medicine', 'old_price', 'new_price',
        'price_change_pct', 'changed_by', 'changed_at'
    ]
    list_filter = ['supplier', 'changed_at']
    search_fields = ['supplier__company_name', 'medicine__generic_name']
    readonly_fields = ['changed_at', 'price_change_pct']
    ordering = ['-changed_at']
