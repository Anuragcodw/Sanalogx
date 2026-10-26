"""
integrations/models.py
MediVault — Hospital & EHR Integration Architecture (Oracle Health / Cerner & FHIR R4 Ready)

Models:
1. ExternalHospitalSystem: Tracks configured hospital EHR connections (Oracle Health, Cerner, Epic mocks).
2. HospitalMedicationOrder: Inbound FHIR R4 MedicationRequest orders awaiting pharmacist review.
3. IntegrationMessageLog: Comprehensive audit ledger for all incoming and outgoing integration events.
"""

import secrets
from django.db import models
from django.conf import settings
from django.utils import timezone

from medicines.models import Medicine
from dispensing.models import Prescription, DispensingRecord


def generate_system_api_key():
    return f"mv_ehr_{secrets.token_urlsafe(32)}"


class ExternalHospitalSystem(models.Model):
    """
    Hospital EHR partner profile (Oracle Health, Cerner, Epic mock adapters).
    """
    class SystemType(models.TextChoices):
        ORACLE_CERNER = 'ORACLE_CERNER', 'Oracle Health / Cerner Millennium'
        EPIC_EHR = 'EPIC_EHR', 'Epic Systems EHR'
        CUSTOM_FHIR = 'CUSTOM_FHIR', 'Custom FHIR R4 Compliant Hospital'
        SIMULATOR = 'SIMULATOR', 'MediVault Mock EHR Simulator'

    name = models.CharField(max_length=200, help_text="e.g. City General Hospital EHR (Cerner Mock)")
    system_code = models.CharField(max_length=50, unique=True, help_text="Unique identifier e.g. CERNER_CGH_01")
    system_type = models.CharField(max_length=30, choices=SystemType.choices, default=SystemType.ORACLE_CERNER)
    api_key = models.CharField(max_length=100, unique=True, default=generate_system_api_key)
    endpoint_url = models.URLField(blank=True, default='', help_text="External EHR outbound webhook endpoint")
    is_active = models.BooleanField(default=True)
    is_demo_simulator = models.BooleanField(default=False, help_text="Indicates test/mock demo records")
    last_sync_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "External Hospital System"
        verbose_name_plural = "External Hospital Systems"
        ordering = ['name']

    def __str__(self):
        return f"{self.name} [{self.system_code}]"


class HospitalMedicationOrder(models.Model):
    """
    Inbound MedicationRequest from external Hospital/EHR system.
    Strictly enforces pharmacist verification prior to dispensing (never auto-dispenses).
    """
    class OrderStatus(models.TextChoices):
        PENDING_REVIEW = 'PENDING_REVIEW', 'Awaiting Pharmacist Review'
        VERIFIED = 'VERIFIED', 'Verified & Ready for Dispensing'
        DISPENSED = 'DISPENSED', 'Dispensing Completed'
        REJECTED = 'REJECTED', 'Rejected by Pharmacist'
        CANCELLED = 'CANCELLED', 'Cancelled by Hospital EHR'
        FAILED = 'FAILED', 'Integration / Validation Failure'

    external_order_id = models.CharField(max_length=100, db_index=True, help_text="External EHR MedicationRequest ID")
    hospital_system = models.ForeignKey(
        ExternalHospitalSystem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='medication_orders'
    )
    resource_type = models.CharField(max_length=50, default='MedicationRequest')
    fhir_version = models.CharField(max_length=10, default='R4')
    idempotency_key = models.CharField(max_length=120, unique=True, db_index=True)

    # Patient Details (Minimization principle)
    patient_uhid = models.CharField(max_length=100, db_index=True, help_text="Patient Identifier / UHID")
    patient_name = models.CharField(max_length=255)
    patient_dob = models.DateField(null=True, blank=True)
    patient_gender = models.CharField(max_length=20, blank=True, default='')

    # Clinical Order Details
    prescribing_practitioner = models.CharField(max_length=255, default='Dr. Attending Physician')
    practitioner_license = models.CharField(max_length=100, blank=True, default='')
    department = models.CharField(max_length=100, blank=True, default='Inpatient Care')
    diagnosis = models.CharField(max_length=255, blank=True, default='')

    # Medication Requested
    raw_medication_name = models.CharField(max_length=255, help_text="Medication name as sent by EHR")
    medication_code = models.CharField(max_length=100, blank=True, default='', help_text="RxNorm / NDC / SNOMED code")
    mapped_medicine = models.ForeignKey(
        Medicine,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='hospital_orders',
        help_text="Mapped internal MediVault Medicine Master record"
    )
    prescribed_quantity = models.PositiveIntegerField(default=1)
    dosage_instruction = models.TextField(blank=True, default='')
    priority = models.CharField(max_length=20, choices=[('routine', 'Routine'), ('urgent', 'Urgent'), ('asap', 'ASAP'), ('stat', 'STAT')], default='routine')

    # Lifecycle & Audit
    status = models.CharField(max_length=30, choices=OrderStatus.choices, default=OrderStatus.PENDING_REVIEW)
    rejection_reason = models.TextField(blank=True, default='')
    fhir_payload = models.JSONField(default=dict, help_text="Raw FHIR R4 JSON MedicationRequest resource")

    # MediVault Clinical Links
    prescription = models.ForeignKey(
        Prescription,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='hospital_orders'
    )
    dispensing_record = models.ForeignKey(
        DispensingRecord,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='hospital_orders'
    )

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reviewed_hospital_orders'
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Hospital Medication Order"
        verbose_name_plural = "Hospital Medication Orders"
        ordering = ['-received_at']
        indexes = [
            models.Index(fields=['status', '-received_at']),
            models.Index(fields=['patient_uhid']),
            models.Index(fields=['external_order_id']),
        ]

    def __str__(self):
        return f"[{self.get_status_display()}] {self.external_order_id} — {self.patient_name} ({self.raw_medication_name})"


class IntegrationMessageLog(models.Model):
    """
    Immutable audit trail for all incoming/outgoing FHIR payloads and integration events.
    """
    class Direction(models.TextChoices):
        INBOUND = 'INBOUND', 'Inbound (EHR → MediVault)'
        OUTBOUND = 'OUTBOUND', 'Outbound (MediVault → EHR)'

    class Status(models.TextChoices):
        SUCCESS = 'SUCCESS', 'Success'
        VALIDATION_ERROR = 'VALIDATION_ERROR', 'Validation Error'
        FAILED = 'FAILED', 'Failed'
        RETRY = 'RETRY', 'Pending Retry'

    direction = models.CharField(max_length=10, choices=Direction.choices)
    hospital_system = models.ForeignKey(
        ExternalHospitalSystem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='message_logs'
    )
    resource_type = models.CharField(max_length=50, default='MedicationRequest')
    request_method = models.CharField(max_length=10, default='POST')
    request_path = models.CharField(max_length=255)
    status_code = models.IntegerField(default=200)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SUCCESS)
    external_reference = models.CharField(max_length=100, blank=True, default='')

    payload = models.JSONField(default=dict)
    response_data = models.JSONField(default=dict)
    error_message = models.TextField(blank=True, default='')

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Integration Message Log"
        verbose_name_plural = "Integration Message Logs"
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.direction} | {self.resource_type} | {self.status} ({self.status_code}) at {self.created_at}"
