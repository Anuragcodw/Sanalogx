from django.contrib import admin
from .models import (
    PharmacyQRToken, MedicineRequest, MedicineRequestItem,
    DispensingInvoice, InvoiceItem
)


class MedicineRequestItemInline(admin.TabularInline):
    model = MedicineRequestItem
    extra = 0
    readonly_fields = ('requested_quantity', 'approved_quantity', 'dispensed_quantity')


@admin.register(MedicineRequest)
class MedicineRequestAdmin(admin.ModelAdmin):
    list_display = ('request_id', 'patient_name', 'patient_id', 'mobile_number', 'request_status', 'total_items', 'created_at')
    list_filter = ('request_status', 'created_at')
    search_fields = ('request_id', 'patient_name', 'patient_id', 'mobile_number')
    inlines = [MedicineRequestItemInline]


class InvoiceItemInline(admin.TabularInline):
    model = InvoiceItem
    extra = 0
    readonly_fields = ('medicine', 'batch', 'quantity', 'unit_price', 'total_price')


@admin.register(DispensingInvoice)
class DispensingInvoiceAdmin(admin.ModelAdmin):
    list_display = ('invoice_number', 'patient_name', 'patient_id', 'total_amount', 'payment_status', 'payment_method', 'created_at')
    list_filter = ('payment_status', 'payment_method', 'created_at')
    search_fields = ('invoice_number', 'patient_name', 'patient_id')
    inlines = [InvoiceItemInline]


@admin.register(PharmacyQRToken)
class PharmacyQRTokenAdmin(admin.ModelAdmin):
    list_display = ('name', 'token', 'is_active', 'created_at')
    list_filter = ('is_active',)
