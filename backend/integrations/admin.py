"""
integrations/admin.py
Django admin configuration for Hospital EHR Integration models.

Provides management interfaces for:
- ExternalHospitalSystem — configure connected EHR systems
- HospitalMedicationOrder — review FHIR medication orders, map medicines
- IntegrationMessageLog — immutable audit trail (read-only)
"""

from django.contrib import admin
from django.utils.html import format_html

from .models import ExternalHospitalSystem, HospitalMedicationOrder, IntegrationMessageLog


@admin.register(ExternalHospitalSystem)
class ExternalHospitalSystemAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'system_code', 'system_type', 'is_active',
        'is_demo_simulator', 'last_sync_at', 'created_at',
    )
    list_filter = ('system_type', 'is_active', 'is_demo_simulator')
    search_fields = ('name', 'system_code', 'endpoint_url')
    readonly_fields = ('created_at', 'updated_at')
    fieldsets = (
        ('System Identity', {
            'fields': ('name', 'system_code', 'system_type', 'endpoint_url'),
        }),
        ('Authentication', {
            'fields': ('api_key',),
            'classes': ('collapse',),
            'description': 'API key is used in X-MediVault-API-Key header or Authorization: Bearer <key>',
        }),
        ('Status', {
            'fields': ('is_active', 'is_demo_simulator', 'last_sync_at'),
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )
    ordering = ('name',)


@admin.register(HospitalMedicationOrder)
class HospitalMedicationOrderAdmin(admin.ModelAdmin):
    list_display = (
        'external_order_id', 'patient_uhid', 'patient_name',
        'raw_medication_name', 'mapped_medicine_display',
        'prescribed_quantity', 'status_badge', 'hospital_system',
        'received_at',
    )
    list_filter = ('status', 'hospital_system', 'priority', 'received_at')
    search_fields = (
        'external_order_id', 'patient_uhid', 'patient_name',
        'raw_medication_name', 'idempotency_key',
    )
    readonly_fields = (
        'idempotency_key', 'fhir_payload', 'received_at', 'updated_at',
        'prescription', 'dispensing_record', 'reviewed_by', 'reviewed_at',
    )
    raw_id_fields = ('mapped_medicine',)
    date_hierarchy = 'received_at'
    ordering = ('-received_at',)
    fieldsets = (
        ('Order Identity', {
            'fields': (
                'hospital_system', 'external_order_id', 'idempotency_key',
                'resource_type', 'fhir_version', 'status', 'priority',
            ),
        }),
        ('Patient Details', {
            'fields': (
                'patient_uhid', 'patient_name', 'patient_dob', 'patient_gender',
            ),
        }),
        ('Clinical Order', {
            'fields': (
                'prescribing_practitioner', 'practitioner_license',
                'department', 'diagnosis',
                'raw_medication_name', 'medication_code',
                'mapped_medicine', 'prescribed_quantity', 'dosage_instruction',
            ),
        }),
        ('Lifecycle', {
            'fields': ('rejection_reason', 'reviewed_by', 'reviewed_at'),
        }),
        ('MediVault Links', {
            'fields': ('prescription', 'dispensing_record'),
        }),
        ('FHIR Payload (Raw)', {
            'fields': ('fhir_payload',),
            'classes': ('collapse',),
        }),
        ('Timestamps', {
            'fields': ('received_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    def mapped_medicine_display(self, obj):
        if obj.mapped_medicine:
            return format_html(
                '<span style="color:green;">{}</span>', obj.mapped_medicine.medicine_name
            )
        return format_html('<span style="color:orange;">⚠ Unmatched</span>')
    mapped_medicine_display.short_description = 'Mapped Medicine'

    def status_badge(self, obj):
        colors = {
            'PENDING_REVIEW': '#f0ad4e',
            'VERIFIED': '#5bc0de',
            'DISPENSED': '#5cb85c',
            'REJECTED': '#d9534f',
            'CANCELLED': '#999',
            'FAILED': '#d9534f',
        }
        color = colors.get(obj.status, '#999')
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;border-radius:12px;font-size:11px;">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = 'Status'
    status_badge.admin_order_field = 'status'


@admin.register(IntegrationMessageLog)
class IntegrationMessageLogAdmin(admin.ModelAdmin):
    """Read-only admin for the immutable FHIR audit trail."""
    list_display = (
        'id', 'hospital_system', 'direction', 'resource_type',
        'status', 'status_code', 'external_reference', 'created_at',
    )
    list_filter = ('direction', 'status', 'resource_type', 'hospital_system', 'created_at')
    search_fields = ('external_reference', 'hospital_system__name', 'resource_type', 'error_message')
    readonly_fields = (
        'hospital_system', 'direction', 'resource_type',
        'request_method', 'request_path',
        'payload', 'response_data', 'error_message',
        'status_code', 'status', 'external_reference',
        'ip_address', 'created_at',
    )
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)

    def has_add_permission(self, request):
        """Audit log is append-only — no manual additions via admin."""
        return False

    def has_change_permission(self, request, obj=None):
        """Audit log is immutable — no edits via admin."""
        return False

    def has_delete_permission(self, request, obj=None):
        """Audit log must never be deleted from admin."""
        return False
