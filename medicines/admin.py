from django.contrib import admin
from .models import Category, Medicine, MedicineBatch, BatchPriceHistory, MedicineImportLog


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'is_active', 'created_at']
    search_fields = ['name', 'code']
    list_filter = ['is_active']
    ordering = ['name']


@admin.register(Medicine)
class MedicineAdmin(admin.ModelAdmin):
    list_display = [
        'medicine_name', 'generic_name', 'brand_name',
        'category', 'manufacturer', 'verification_status', 'reorder_level', 'status'
    ]
    list_filter = ['category', 'status', 'verification_status']
    search_fields = ['medicine_name', 'generic_name', 'brand_name', 'barcode', 'manufacturer', 'atc_code']
    readonly_fields = ['created_at', 'updated_at']
    fieldsets = (
        ('Identification', {
            'fields': ('medicine_name', 'generic_name', 'brand_name', 'manufacturer', 'barcode', 'qr_code', 'image')
        }),
        ('Classification & Verification', {
            'fields': ('category', 'dosage_form', 'unit_of_measure', 'strength', 'atc_code', 'regulatory_category', 'verification_status', 'review_note', 'source_reference')
        }),
        ('Pricing & Taxes', {
            'fields': ('tax_rate', 'mrp', 'max_discount_pct')
        }),
        ('Thresholds & Storage', {
            'fields': ('reorder_level', 'minimum_stock', 'maximum_stock', 'safety_stock', 'storage_condition')
        }),
        ('Status & Description', {
            'fields': ('status', 'description')
        }),
        ('Audit', {
            'fields': ('created_at', 'updated_at')
        }),
    )


@admin.register(MedicineBatch)
class MedicineBatchAdmin(admin.ModelAdmin):
    list_display = [
        'medicine', 'batch_number', 'expiry_date',
        'quantity_available', 'purchase_price', 'unit_cost',
        'selling_price', 'mrp', 'status'
    ]
    list_filter = ['status', 'expiry_date']
    search_fields = ['medicine__medicine_name', 'batch_number']
    readonly_fields = ['created_at', 'updated_at']
    ordering = ['expiry_date']


@admin.register(BatchPriceHistory)
class BatchPriceHistoryAdmin(admin.ModelAdmin):
    list_display = ['medicine', 'batch', 'old_selling_price', 'new_selling_price', 'old_purchase_price', 'new_purchase_price', 'changed_by', 'changed_at']
    list_filter = ['changed_at']
    search_fields = ['medicine__medicine_name', 'batch__batch_number']
    readonly_fields = ['changed_at']


@admin.register(MedicineImportLog)
class MedicineImportLogAdmin(admin.ModelAdmin):
    list_display = ['import_type', 'filename', 'uploaded_by', 'total_rows', 'created_count', 'updated_count', 'duplicate_count', 'error_count', 'status', 'created_at']
    list_filter = ['import_type', 'status', 'created_at']
    search_fields = ['filename']
    readonly_fields = ['created_at']


