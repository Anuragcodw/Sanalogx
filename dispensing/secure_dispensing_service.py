"""
MediVault — dispensing/secure_dispensing_service.py
Enterprise Secure Medication Dispensing, Traceability & Controlled Medicine Engine.

Provides:
- Non-deducting Barcode & QR code scanning & validation.
- Prescription verification lifecycle and clinical checklists.
- Strict FEFO multi-batch allocation with child record traceability.
- Controlled Medicine Register (Schedule H1, X, Narcotics) running audit ledger.
- Dispensing Reversal & Return workflows.
- Automated Discrepancy Detection & Queue Management.
- Physical Stock Count Reconciliation.
- Comprehensive audit logging and stock alerts.
"""

import secrets
from decimal import Decimal
from datetime import date
from django.db import transaction, models
from django.utils import timezone
from django.core.exceptions import ValidationError

from medicines.models import Medicine, MedicineBatch
from inventory.models import Inventory, StockTransaction, StockAlert
from inventory import services as inventory_services
from accounts.models import log_audit
from .models import (
    Patient, Doctor, Prescription, PrescriptionItem, DispensingRecord,
    DispensingAllocation, ControlledMedicineRegister,
    StockDiscrepancy, StockReconciliation,
    MedicineRequest, MedicineRequestItem, DispensingInvoice, InvoiceItem
)


# ==============================================================================
# 1. ID GENERATION HELPERS
# ==============================================================================

def generate_dispensing_id():
    """Generates unique Dispensing ID in format DISP-YYYY-NNNNN."""
    year = timezone.now().year
    count = DispensingRecord.objects.count() + 1
    for i in range(100):
        disp_id = f"DISP-{year}-{count + i:05d}"
        if not DispensingRecord.objects.filter(dispensing_id=disp_id).exists():
            return disp_id
    return f"DISP-{year}-{secrets.randbelow(99999):05d}"


def generate_controlled_register_id():
    """Generates unique Controlled Register ID in format CMR-YYYY-NNNNN."""
    year = timezone.now().year
    count = ControlledMedicineRegister.objects.count() + 1
    for i in range(100):
        reg_id = f"CMR-{year}-{count + i:05d}"
        if not ControlledMedicineRegister.objects.filter(register_id=reg_id).exists():
            return reg_id
    return f"CMR-{year}-{secrets.randbelow(99999):05d}"


def generate_discrepancy_id():
    """Generates unique Discrepancy ID in format DISC-YYYY-NNNNN."""
    year = timezone.now().year
    count = StockDiscrepancy.objects.count() + 1
    for i in range(100):
        disc_id = f"DISC-{year}-{count + i:05d}"
        if not StockDiscrepancy.objects.filter(discrepancy_id=disc_id).exists():
            return disc_id
    return f"DISC-{year}-{secrets.randbelow(99999):05d}"


def generate_reconciliation_session_id():
    """Generates unique Reconciliation Session ID in format REC-YYYY-NNNNN."""
    year = timezone.now().year
    count = StockReconciliation.objects.count() + 1
    for i in range(100):
        rec_id = f"REC-{year}-{count + i:05d}"
        if not StockReconciliation.objects.filter(session_id=rec_id).exists():
            return rec_id
    return f"REC-{year}-{secrets.randbelow(99999):05d}"


# ==============================================================================
# 2. PRESCRIPTION VERIFICATION & CLINICAL LIFECYCLE
# ==============================================================================

@transaction.atomic
def verify_prescription(prescription_id, pharmacist_user):
    """
    Verifies a clinical prescription before medication can be dispensed.
    Changes status to VERIFIED and records the verifying pharmacist.
    """
    try:
        rx = Prescription.objects.select_for_update().get(
            models.Q(prescription_id__iexact=prescription_id) | models.Q(id=prescription_id if str(prescription_id).isdigit() else 0)
        )
    except Prescription.DoesNotExist:
        raise ValidationError(f"Prescription '{prescription_id}' not found.")

    today = timezone.now().date()
    if rx.expiry_date and rx.expiry_date < today:
        rx.status = Prescription.Status.EXPIRED
        rx.save(update_fields=['status'])
        raise ValidationError(f"Prescription {rx.prescription_id} expired on {rx.expiry_date.strftime('%d %b %Y')}. Cannot verify.")

    if rx.status in [Prescription.Status.DISPENSED, Prescription.Status.CANCELLED, Prescription.Status.REJECTED]:
        raise ValidationError(f"Prescription {rx.prescription_id} is already {rx.get_status_display()} and cannot be verified.")

    rx.status = Prescription.Status.VERIFIED
    rx.verified_by = pharmacist_user
    rx.verified_at = timezone.now()
    rx.save(update_fields=['status', 'verified_by', 'verified_at'])

    # Update item statuses
    rx.items.filter(status=PrescriptionItem.Status.PENDING).update(status=PrescriptionItem.Status.VERIFIED)

    log_audit(
        user=pharmacist_user,
        action='PRESCRIPTION_VERIFIED',
        description=f"Prescription {rx.prescription_id} for {rx.patient_name} verified by {pharmacist_user.username}.",
        performed_by=pharmacist_user
    )

    return rx


@transaction.atomic
def reject_prescription(prescription_id, pharmacist_user, rejection_reason):
    """
    Rejects a prescription with a clinical/administrative reason.
    """
    if not rejection_reason or not rejection_reason.strip():
        raise ValidationError("Rejection reason is required.")

    try:
        rx = Prescription.objects.select_for_update().get(
            models.Q(prescription_id__iexact=prescription_id) | models.Q(id=prescription_id if str(prescription_id).isdigit() else 0)
        )
    except Prescription.DoesNotExist:
        raise ValidationError(f"Prescription '{prescription_id}' not found.")

    rx.status = Prescription.Status.REJECTED
    rx.rejection_reason = rejection_reason.strip()
    rx.save(update_fields=['status', 'rejection_reason'])

    rx.items.exclude(status=PrescriptionItem.Status.DISPENSED).update(status=PrescriptionItem.Status.REJECTED)

    log_audit(
        user=pharmacist_user,
        action='PRESCRIPTION_REJECTED',
        description=f"Prescription {rx.prescription_id} rejected by {pharmacist_user.username}. Reason: {rejection_reason}",
        performed_by=pharmacist_user
    )

    return rx


# ==============================================================================
# 3. BARCODE & QR SCANNING (NON-DEDUCTING VALIDATION)
# ==============================================================================

def scan_barcode_or_qr(barcode_value):
    """
    Scans and validates a Barcode or QR code.
    CRITICAL: This function NEVER deducts stock. It only looks up, decodes,
    and returns rich validation metadata for the pharmacist UI.
    """
    if not barcode_value:
        return {'success': False, 'message': 'Empty barcode value provided.'}

    code = str(barcode_value).strip()
    today = timezone.now().date()

    # Normalize QR payloads: parse JSON or URL paths if present
    if code.startswith('{') and code.endswith('}'):
        try:
            import json as _json
            parsed = _json.loads(code)
            if isinstance(parsed, dict):
                code = parsed.get('rx') or parsed.get('prescription_id') or parsed.get('prescription_number') or parsed.get('batch') or parsed.get('batch_number') or parsed.get('barcode') or parsed.get('medicine_id') or parsed.get('medicine') or code
                code = str(code).strip()
        except Exception:
            pass
    elif '/' in code:
        segments = [s.strip() for s in code.split('/') if s.strip()]
        for seg in reversed(segments):
            if seg.upper().startswith(('RX-', 'CR-', 'DISP-', 'BATCH-')) or (seg.isdigit() and len(seg) >= 4):
                code = seg
                break

    # 1. Check if it's a Prescription ID / Prescription QR
    rx = Prescription.objects.filter(
        models.Q(prescription_id__iexact=code) | models.Q(prescription_number__iexact=code)
    ).first()

    if rx:
        is_expired = rx.expiry_date and rx.expiry_date < today
        items_data = []
        for item in rx.items.select_related('medicine').all():
            total_active_stock = sum(
                b.quantity_available for b in item.medicine.batches.filter(
                    expiry_date__gt=today,
                    quantity_available__gt=0
                ).exclude(status__in=['EXPIRED', 'QUARANTINED', 'RETURNED'])
            )
            items_data.append({
                'item_id': item.id,
                'medicine_id': item.medicine.id,
                'medicine_name': item.medicine.medicine_name,
                'generic_name': item.medicine.generic_name,
                'strength': item.strength or item.medicine.strength,
                'dosage_form': item.dosage_form or item.medicine.dosage_form,
                'dosage': item.dosage,
                'frequency': item.frequency,
                'duration': item.duration,
                'prescribed_quantity': item.prescribed_quantity,
                'dispensed_quantity': item.dispensed_quantity,
                'remaining_quantity': max(0, item.prescribed_quantity - item.dispensed_quantity),
                'available_stock': total_active_stock,
                'status': item.status,
                'regulatory_category': item.medicine.regulatory_category,
                'is_controlled': item.medicine.is_controlled,
            })

        return {
            'success': True,
            'type': 'PRESCRIPTION',
            'prescription': {
                'id': rx.id,
                'prescription_id': rx.prescription_id,
                'prescription_number': rx.prescription_number,
                'patient_name': rx.patient_name,
                'patient_id': rx.patient_id,
                'doctor_name': rx.doctor_name,
                'doctor_registration': rx.doctor_registration,
                'prescription_date': rx.prescription_date.strftime('%Y-%m-%d'),
                'expiry_date': rx.expiry_date.strftime('%Y-%m-%d') if rx.expiry_date else None,
                'is_expired': is_expired,
                'status': rx.status,
                'status_display': rx.get_status_display(),
                'diagnosis': rx.diagnosis,
                'items': items_data
            },
            'message': f"Prescription {rx.prescription_id} loaded successfully."
        }

    # 2. Check if it's a specific MedicineBatch (by batch_number)
    batch = MedicineBatch.objects.filter(
        batch_number__iexact=code
    ).select_related('medicine', 'medicine__category').first()

    if batch:
        med = batch.medicine
        is_expired = batch.expiry_date <= today or batch.status == MedicineBatch.BatchStatus.EXPIRED
        is_quarantined = batch.status == MedicineBatch.BatchStatus.QUARANTINED
        is_out_of_stock = (batch.quantity_available <= 0)
        is_inactive = (med.status != Medicine.Status.ACTIVE)
        days_to_expiry = (batch.expiry_date - today).days

        # Determine precise status classification
        if is_quarantined:
            status_code = 'QUARANTINED'
            status_note = f"Batch '{batch.batch_number}' is QUARANTINED and cannot be dispensed."
        elif is_expired:
            status_code = 'EXPIRED'
            status_note = f"Batch '{batch.batch_number}' EXPIRED on {batch.expiry_date.strftime('%d %b %Y')}. Clinical dispensing blocked."
        elif is_inactive:
            status_code = 'INACTIVE'
            status_note = f"Medicine '{med.medicine_name}' is marked INACTIVE in the master catalog."
        elif is_out_of_stock:
            status_code = 'OUT_OF_STOCK'
            status_note = f"Batch '{batch.batch_number}' has 0 available units in stock."
        else:
            status_code = 'VALID'
            status_note = None

        # Check if batch is FEFO recommended
        fefo_batches = MedicineBatch.objects.filter(
            medicine=med,
            expiry_date__gt=today,
            quantity_available__gt=0
        ).exclude(status__in=['EXPIRED', 'QUARANTINED', 'RETURNED']).order_by('expiry_date')

        first_fefo = fefo_batches.first()
        is_fefo_first = (first_fefo and first_fefo.id == batch.id)
        fefo_warning = None
        if not is_fefo_first and first_fefo and not is_expired and not is_quarantined:
            fefo_warning = (
                f"FEFO Notice: Batch '{first_fefo.batch_number}' expires earlier "
                f"({first_fefo.expiry_date.strftime('%d %b %Y')}) and should preferably be dispensed first."
            )

        return {
            'success': True,
            'type': 'BATCH',
            'status_code': status_code,
            'status_note': status_note,
            'batch': {
                'id': batch.id,
                'batch_number': batch.batch_number,
                'barcode': batch.batch_number,
                'expiry_date': batch.expiry_date.strftime('%Y-%m-%d'),
                'is_expired': is_expired,
                'is_quarantined': is_quarantined,
                'is_out_of_stock': is_out_of_stock,
                'days_to_expiry': days_to_expiry,
                'quantity_available': batch.quantity_available,
                'unit_cost': str(batch.purchase_price) if batch.purchase_price else '0.00',
                'selling_price': str(batch.selling_price) if batch.selling_price else '0.00',
                'mrp': str(batch.selling_price) if batch.selling_price else '0.00',
                'status': batch.status,
                'is_fefo_first': is_fefo_first,
                'fefo_warning': fefo_warning,
            },
            'medicine': {
                'id': med.id,
                'medicine_name': med.medicine_name,
                'generic_name': med.generic_name,
                'brand_name': med.brand_name or '',
                'strength': med.strength,
                'dosage_form': med.dosage_form,
                'manufacturer': med.manufacturer or 'N/A',
                'category': med.category.name if med.category else 'General',
                'regulatory_category': med.regulatory_category,
                'regulatory_display': med.get_regulatory_category_display(),
                'is_controlled': med.is_controlled,
                'is_prescription_required': med.is_prescription_required,
                'is_active': (med.status == Medicine.Status.ACTIVE),
                'barcode': med.barcode or '',
            },
            'message': status_note or f"Batch '{batch.batch_number}' for {med.medicine_name} validated successfully."
        }

    # 3. Check if it's a Medicine Barcode / Medicine ID / MED-XXXX / Name
    med_q = models.Q(barcode__iexact=code) | models.Q(medicine_name__iexact=code)
    if code.isdigit():
        med_q |= models.Q(pk=int(code))
    elif code.upper().startswith('MED-'):
        clean_num = code.upper().replace('MED-', '').lstrip('0')
        if clean_num.isdigit():
            med_q |= models.Q(pk=int(clean_num))

    matching_meds = list(Medicine.objects.filter(med_q).select_related('category')[:10])

    if len(matching_meds) > 1:
        meds_summary = []
        for m in matching_meds:
            stock = sum(
                b.quantity_available for b in m.batches.filter(
                    expiry_date__gt=today, quantity_available__gt=0
                ).exclude(status__in=['EXPIRED', 'QUARANTINED', 'RETURNED'])
            )
            meds_summary.append({
                'id': m.id,
                'medicine_name': m.medicine_name,
                'generic_name': m.generic_name or '',
                'strength': m.strength or '',
                'dosage_form': m.dosage_form or '',
                'regulatory_category': m.regulatory_category,
                'regulatory_display': m.get_regulatory_category_display(),
                'available_stock': stock,
                'barcode': m.barcode or ''
            })
        return {
            'success': True,
            'type': 'AMBIGUOUS_MATCH',
            'code': code,
            'count': len(matching_meds),
            'medicines': meds_summary,
            'message': f"Multiple medicines ({len(matching_meds)}) match '{code}'. Please select the intended medicine."
        }
    elif len(matching_meds) == 1:
        med = matching_meds[0]
        active_batches = med.batches.filter(
            expiry_date__gt=today,
            quantity_available__gt=0
        ).exclude(status__in=['EXPIRED', 'QUARANTINED', 'RETURNED']).order_by('expiry_date')

        batches_list = []
        for b in active_batches:
            batches_list.append({
                'id': b.id,
                'batch_number': b.batch_number,
                'expiry_date': b.expiry_date.strftime('%Y-%m-%d'),
                'quantity_available': b.quantity_available,
                'selling_price': str(b.selling_price) if b.selling_price else '0.00',
            })

        total_stock = sum(b['quantity_available'] for b in batches_list)
        is_inactive = (med.status != Medicine.Status.ACTIVE)
        is_out_of_stock = (total_stock <= 0)

        if is_inactive:
            status_code = 'INACTIVE'
            status_note = f"Medicine '{med.medicine_name}' is marked INACTIVE in the master catalog."
        elif is_out_of_stock:
            status_code = 'OUT_OF_STOCK'
            status_note = f"Medicine '{med.medicine_name}' currently has ZERO available stock."
        else:
            status_code = 'VALID'
            status_note = None

        return {
            'success': True,
            'type': 'MEDICINE',
            'status_code': status_code,
            'status_note': status_note,
            'medicine': {
                'id': med.id,
                'medicine_name': med.medicine_name,
                'generic_name': med.generic_name,
                'brand_name': med.brand_name or '',
                'strength': med.strength,
                'dosage_form': med.dosage_form,
                'manufacturer': med.manufacturer or 'N/A',
                'category': med.category.name if med.category else 'General',
                'regulatory_category': med.regulatory_category,
                'regulatory_display': med.get_regulatory_category_display(),
                'is_controlled': med.is_controlled,
                'is_prescription_required': med.is_prescription_required,
                'is_active': (med.status == Medicine.Status.ACTIVE),
                'barcode': med.barcode or '',
                'total_stock': total_stock,
                'batches': batches_list
            },
            'message': status_note or f"Medicine '{med.medicine_name}' found with {total_stock} total units across {len(batches_list)} active batch(es)."
        }

    # 4. Check if it's a Patient (UHID, mobile number, national ID, or ID)
    patient_q = models.Q(uhid__iexact=code) | models.Q(mobile_number__iexact=code)
    if code.isdigit():
        patient_q |= models.Q(pk=int(code))
    elif code.upper().startswith('UHID-'):
        patient_q |= models.Q(uhid__iexact=code)

    patient = Patient.objects.filter(patient_q).first()
    if patient:
        active_rxs = Prescription.objects.filter(
            models.Q(patient_record=patient) | models.Q(patient_id__iexact=patient.uhid),
            status__in=[
                Prescription.Status.VERIFIED,
                Prescription.Status.READY_FOR_DISPENSING,
                Prescription.Status.PARTIALLY_DISPENSED
            ]
        ).order_by('-prescription_date')[:10]

        rx_list = []
        for p_rx in active_rxs:
            rx_list.append({
                'id': p_rx.id,
                'prescription_id': p_rx.prescription_id,
                'doctor_name': p_rx.doctor_name,
                'date': p_rx.prescription_date.strftime('%Y-%m-%d'),
                'status': p_rx.status,
                'status_display': p_rx.get_status_display(),
                'item_count': p_rx.items.count()
            })

        return {
            'success': True,
            'type': 'PATIENT',
            'patient': {
                'id': patient.id,
                'uhid': patient.uhid,
                'name': patient.name,
                'gender': patient.gender,
                'age': patient.age,
                'phone': patient.phone,
                'email': patient.email,
                'address': patient.address,
                'blood_group': patient.blood_group,
                'allergies': patient.allergies,
                'active_prescriptions': rx_list
            },
            'message': f"Patient record '{patient.name}' ({patient.uhid}) loaded with {len(rx_list)} active prescription(s)."
        }

    # 5. Check if it's a Doctor (doctor_code, registration_number)
    doc_q = models.Q(doctor_code__iexact=code) | models.Q(registration_number__iexact=code)
    if code.upper().startswith('DOC-'):
        doc_q |= models.Q(doctor_code__iexact=code)

    doctor = Doctor.objects.filter(doc_q).first()
    if doctor:
        return {
            'success': True,
            'type': 'DOCTOR',
            'doctor': {
                'id': doctor.id,
                'doctor_code': doctor.doctor_code,
                'name': doctor.name,
                'department': doctor.department,
                'specialization': doctor.specialization,
                'registration_number': doctor.registration_number,
                'phone': doctor.phone
            },
            'message': f"Doctor '{doctor.name}' ({doctor.doctor_code}) verified."
        }

    return {
        'success': False,
        'type': 'UNKNOWN',
        'status_code': 'NOT_FOUND',
        'code': code,
        'message': f"No matching medicine, batch, prescription, or patient found for '{code}'.",
        'suggestions': [
            'Verify code for typos or truncated barcode digits.',
            'If this is a new medicine, register it in the Medicine Master catalogue.',
            'If this is a new patient, register them via the Patient Registration module.'
        ]
    }


# ==============================================================================
# 4. SECURE DISPENSING ELIGIBILITY VALIDATOR
# ==============================================================================

def validate_dispensing_eligibility(medicine, requested_qty, prescription=None, pharmacist_user=None, specific_batch=None, verification_checklist=None):
    """
    Validates regulatory requirements, clinical eligibility, safety checklist, and stock availability before dispensing.
    Returns (is_valid, error_message).
    """
    today = timezone.now().date()

    if requested_qty <= 0:
        return False, "Quantity to dispense must be greater than zero."

    # 1. Clinical Safety Checklist Validation (if checklist is submitted)
    if verification_checklist is not None:
        if not isinstance(verification_checklist, dict):
            return False, "Verification checklist must be a valid object."

        if not verification_checklist.get('patient_identity_verified'):
            return False, "Mandatory safety check failed: Patient identity must be verified before dispensing."
        if not verification_checklist.get('dosage_checked'):
            return False, "Mandatory safety check failed: Dosage, frequency, and strength must be validated before dispensing."
        if not verification_checklist.get('batch_expiry_verified'):
            return False, "Mandatory safety check failed: Batch expiry and physical packaging must be confirmed before dispensing."
        if medicine.is_controlled and not verification_checklist.get('controlled_drug_registered'):
            return False, f"Mandatory safety check failed: Controlled drug register entry confirmation is required for {medicine.get_regulatory_category_display()}."

    # 2. Regulatory & Controlled Medicine Checks (Strict OTC vs Rx Enforcement)
    RESTRICTED_OTC_CATEGORIES = [
        Medicine.RegulatoryCategory.SCHEDULE_H,
        Medicine.RegulatoryCategory.SCHEDULE_H1,
        Medicine.RegulatoryCategory.SCHEDULE_X,
        Medicine.RegulatoryCategory.NARCOTIC,
        Medicine.RegulatoryCategory.CONTROLLED_OTHER
    ]

    if not prescription and (medicine.is_prescription_required or medicine.is_controlled or medicine.regulatory_category in RESTRICTED_OTC_CATEGORIES):
        return False, f"Medication '{medicine.medicine_name}' is classified as {medicine.get_regulatory_category_display()} and requires a verified prescription (cannot be dispensed Over-The-Counter (OTC)). A verified prescription is mandatory."

    if medicine.is_prescription_required or medicine.is_controlled:
        if not prescription:
            return False, f"Controlled/Prescription medication '{medicine.medicine_name}' ({medicine.get_regulatory_category_display()}) requires a verified prescription. OTC dispensing is prohibited."

        if prescription.status not in [
            Prescription.Status.VERIFIED,
            Prescription.Status.READY_FOR_DISPENSING,
            Prescription.Status.PARTIALLY_DISPENSED
        ]:
            return False, f"Prescription {prescription.prescription_id} is in '{prescription.get_status_display()}' status. Only VERIFIED prescriptions can be dispensed."

        if prescription.expiry_date and prescription.expiry_date < today:
            return False, f"Prescription {prescription.prescription_id} has expired on {prescription.expiry_date.strftime('%d %b %Y')}."

        # Check if item exists in prescription
        rx_item = prescription.items.filter(medicine=medicine).first()
        if not rx_item:
            return False, f"Medicine '{medicine.medicine_name}' is not listed in Prescription {prescription.prescription_id}."

        remaining_prescribed = max(0, rx_item.prescribed_quantity - rx_item.dispensed_quantity)
        if remaining_prescribed <= 0:
            return False, f"All prescribed units ({rx_item.prescribed_quantity}) for '{medicine.medicine_name}' in Prescription {prescription.prescription_id} have already been dispensed."
        if requested_qty > remaining_prescribed:
            return False, f"Requested quantity ({requested_qty}) exceeds remaining authorized quantity ({remaining_prescribed}) for Prescription {prescription.prescription_id}."

    # 3. Stock Availability Check
    if specific_batch:
        if specific_batch.expiry_date <= today:
            return False, f"Selected Batch '{specific_batch.batch_number}' has expired on {specific_batch.expiry_date.strftime('%d %b %Y')}."
        if specific_batch.status in ['EXPIRED', 'QUARANTINED', 'RETURNED']:
            return False, f"Selected Batch '{specific_batch.batch_number}' is marked as {specific_batch.get_status_display()}."
        if specific_batch.quantity_available < requested_qty:
            return False, f"Insufficient stock in Batch '{specific_batch.batch_number}'. Requested: {requested_qty}, Available: {specific_batch.quantity_available}."
    else:
        active_batches = medicine.batches.filter(
            expiry_date__gt=today,
            quantity_available__gt=0
        ).exclude(status__in=['EXPIRED', 'QUARANTINED', 'RETURNED'])
        total_avail = sum(b.quantity_available for b in active_batches)
        if total_avail < requested_qty:
            return False, f"Insufficient active stock for '{medicine.medicine_name}'. Requested: {requested_qty}, Total Available: {total_avail}."

    return True, None


def preview_fefo_allocation(medicine, requested_qty, specific_batch=None):
    """
    Simulates FEFO batch allocation for a medicine and requested quantity
    without row locks or database modifications.
    Used for real-time frontend preview before execution.
    """
    if isinstance(medicine, (int, str)):
        try:
            medicine = Medicine.objects.get(pk=medicine)
        except (Medicine.DoesNotExist, ValueError):
            return {
                'success': False,
                'message': f"Medicine with identifier '{medicine}' not found."
            }

    if specific_batch and isinstance(specific_batch, (int, str)):
        try:
            specific_batch = MedicineBatch.objects.get(pk=specific_batch)
        except (MedicineBatch.DoesNotExist, ValueError):
            specific_batch = None

    try:
        requested_qty = int(requested_qty)
    except (ValueError, TypeError):
        return {'success': False, 'message': 'Requested quantity must be a positive integer.'}

    if requested_qty <= 0:
        return {'success': False, 'message': 'Requested quantity must be greater than zero.'}

    today = timezone.now().date()
    allocated_batches = []
    remaining = requested_qty
    fefo_warnings = []

    # If specific batch preferred
    if specific_batch:
        if specific_batch.expiry_date <= today:
            fefo_warnings.append(f"Preferred Batch '{specific_batch.batch_number}' has expired ({specific_batch.expiry_date.strftime('%d %b %Y')}).")
        elif specific_batch.status in ['EXPIRED', 'QUARANTINED', 'RETURNED']:
            fefo_warnings.append(f"Preferred Batch '{specific_batch.batch_number}' is {specific_batch.get_status_display()}.")
        elif specific_batch.quantity_available <= 0:
            fefo_warnings.append(f"Preferred Batch '{specific_batch.batch_number}' has zero available stock.")
        else:
            take = min(remaining, specific_batch.quantity_available)
            days_to_exp = (specific_batch.expiry_date - today).days
            unit_price = specific_batch.selling_price or Decimal('0.00')
            allocated_batches.append({
                'batch_id': specific_batch.id,
                'batch_number': specific_batch.batch_number,
                'expiry_date': specific_batch.expiry_date.strftime('%Y-%m-%d'),
                'days_to_expiry': days_to_exp,
                'quantity_available': specific_batch.quantity_available,
                'alloc_qty': take,
                'unit_price': str(unit_price),
                'total_price': str(Decimal(str(take)) * unit_price),
                'is_preferred': True,
                'is_fefo_first': False
            })
            remaining -= take

    # FEFO search across active batches
    batches_query = MedicineBatch.objects.filter(
        medicine=medicine,
        expiry_date__gt=today,
        quantity_available__gt=0
    ).exclude(status__in=['EXPIRED', 'QUARANTINED', 'RETURNED']).order_by('expiry_date', 'id')

    if specific_batch:
        batches_query = batches_query.exclude(id=specific_batch.id)

    for idx, b in enumerate(batches_query):
        if remaining <= 0:
            break
        take = min(remaining, b.quantity_available)
        days_to_exp = (b.expiry_date - today).days
        unit_price = b.selling_price or Decimal('0.00')
        is_first = (idx == 0 and not specific_batch)

        if days_to_exp <= 30:
            fefo_warnings.append(f"Batch '{b.batch_number}' expires in {days_to_exp} day(s) ({b.expiry_date.strftime('%d %b %Y')}).")

        allocated_batches.append({
            'batch_id': b.id,
            'batch_number': b.batch_number,
            'expiry_date': b.expiry_date.strftime('%Y-%m-%d'),
            'days_to_expiry': days_to_exp,
            'quantity_available': b.quantity_available,
            'alloc_qty': take,
            'unit_price': str(unit_price),
            'total_price': str(Decimal(str(take)) * unit_price),
            'is_preferred': False,
            'is_fefo_first': is_first
        })
        remaining -= take

    total_allocated = requested_qty - remaining
    is_sufficient = (remaining == 0)
    total_amount = sum(Decimal(a['total_price']) for a in allocated_batches)

    return {
        'success': True,
        'medicine_id': medicine.id,
        'medicine_name': medicine.medicine_name,
        'generic_name': medicine.generic_name or '',
        'brand_name': medicine.brand_name or '',
        'strength': medicine.strength or '',
        'dosage_form': medicine.dosage_form or '',
        'regulatory_category': medicine.regulatory_category,
        'regulatory_display': medicine.get_regulatory_category_display(),
        'is_controlled': medicine.is_controlled,
        'is_prescription_required': medicine.is_prescription_required,
        'requested_qty': requested_qty,
        'total_allocated': total_allocated,
        'shortage': remaining,
        'is_sufficient': is_sufficient,
        'total_amount': str(total_amount),
        'allocations': allocated_batches,
        'fefo_warnings': fefo_warnings
    }


# ==============================================================================
# 5. ATOMIC SECURE DISPENSING ENGINE (FEFO MULTI-BATCH DEDUCTION)
# ==============================================================================

@transaction.atomic
def execute_secure_dispensing(
    medicine,
    requested_qty,
    pharmacist_user,
    prescription=None,
    patient_name='',
    patient_id='',
    verification_checklist=None,
    specific_batch=None,
    notes=''
):
    """
    Executes an atomic, secure medication dispensing transaction.

    Features:
    1. Row-level locks on Inventory and MedicineBatch (select_for_update).
    2. Strict FEFO multi-batch allocation breakdown.
    3. Creation of master DispensingRecord.
    4. Creation of DispensingAllocation child records linking exact batches & stock transactions.
    5. Controlled Medicine Register ledger recording if Schedule H1/X/Narcotic.
    6. StockTransaction creation with type DISPENSE.
    7. Prescription lifecycle status updates.
    8. Recalculates Inventory and triggers reorder stock alerts if needed.
    9. Complete audit logging.
    """
    if isinstance(medicine, (int, str)):
        medicine = Medicine.objects.get(pk=medicine)

    if prescription and isinstance(prescription, (int, str)):
        prescription = Prescription.objects.get(
            models.Q(prescription_id__iexact=str(prescription)) | models.Q(id=int(prescription) if str(prescription).isdigit() else 0)
        )

    if specific_batch and isinstance(specific_batch, (int, str)):
        specific_batch = MedicineBatch.objects.get(pk=specific_batch)

    requested_qty = int(requested_qty)
    today = timezone.now().date()

    # 1. Validation
    is_valid, err_msg = validate_dispensing_eligibility(
        medicine=medicine,
        requested_qty=requested_qty,
        prescription=prescription,
        pharmacist_user=pharmacist_user,
        specific_batch=specific_batch,
        verification_checklist=verification_checklist
    )
    if not is_valid:
        raise ValidationError(err_msg)

    # Derive patient info and link patient_record
    patient_record = None
    if prescription:
        patient_name = patient_name or prescription.patient_name
        patient_id = patient_id or prescription.patient_id
        if prescription.patient_record:
            patient_record = prescription.patient_record

    if not patient_record and patient_id:
        patient_record = Patient.objects.filter(uhid__iexact=patient_id).first()

    if not patient_name:
        patient_name = "Walk-in Patient / OTC"
        patient_id = f"PAT-{timezone.now().strftime('%m%d%H%M%S')}"

    dispensing_id = generate_dispensing_id()

    # 2. Acquire lock on Inventory
    inventory, _ = Inventory.objects.select_for_update().get_or_create(medicine=medicine)
    prev_inv_stock = inventory.current_stock

    # 3. Determine Batch Allocations (FEFO / Specific Batch)
    allocated_batches_plan = []
    remaining_to_allocate = requested_qty

    if specific_batch:
        locked_batch = MedicineBatch.objects.select_for_update().get(pk=specific_batch.id)
        alloc_qty = min(remaining_to_allocate, locked_batch.quantity_available)
        allocated_batches_plan.append((locked_batch, alloc_qty))
        remaining_to_allocate -= alloc_qty

    if remaining_to_allocate > 0:
        # FEFO order: earliest expiry date first
        fefo_query = MedicineBatch.objects.select_for_update().filter(
            medicine=medicine,
            expiry_date__gt=today,
            quantity_available__gt=0
        ).exclude(status__in=['EXPIRED', 'QUARANTINED', 'RETURNED']).order_by('expiry_date', 'id')

        if specific_batch:
            fefo_query = fefo_query.exclude(id=specific_batch.id)

        for b in fefo_query:
            if remaining_to_allocate <= 0:
                break
            take_qty = min(remaining_to_allocate, b.quantity_available)
            allocated_batches_plan.append((b, take_qty))
            remaining_to_allocate -= take_qty

    if remaining_to_allocate > 0:
        raise ValidationError(f"Could not allocate full requested quantity ({requested_qty}) across active batches.")

    # 4. Create Master DispensingRecord
    disp_record = DispensingRecord.objects.create(
        dispensing_id=dispensing_id,
        prescription=prescription,
        patient_record=patient_record,
        patient_name=patient_name.strip(),
        patient_id=patient_id.strip(),
        medicine=medicine,
        total_quantity=requested_qty,
        pharmacist=pharmacist_user,
        regulatory_category=medicine.regulatory_category,
        verification_checklist=verification_checklist or {
            'patient_identity_verified': True,
            'prescription_valid': True if prescription else False,
            'dosage_checked': True,
            'drug_interaction_checked': True,
            'controlled_drug_registered': medicine.is_controlled,
            'batch_expiry_verified': True,
            'counseling_provided': True
        },
        notes=notes.strip() if notes else '',
        status=DispensingRecord.Status.COMPLETED
    )

    created_allocations = []

    # 5. Apply Deductions, Child Allocations & StockTransactions via central StockService
    for batch_obj, alloc_qty in allocated_batches_plan:
        tx_ref = f"TX-{dispensing_id}-{batch_obj.batch_number}"
        stock_tx = inventory_services.remove_stock(
            medicine=medicine,
            batch=batch_obj,
            quantity=alloc_qty,
            transaction_type=StockTransaction.TransactionType.DISPENSE,
            performed_by=pharmacist_user,
            reference_number=dispensing_id,
            notes=f"Secure Dispensing #{dispensing_id} for {patient_name} (Batch {batch_obj.batch_number})",
            transaction_number=tx_ref
        )
        batch_obj.refresh_from_db()

        unit_price = batch_obj.selling_price or Decimal('0.00')
        total_price = Decimal(str(alloc_qty)) * unit_price

        # Dispensing Allocation child record
        child_alloc = DispensingAllocation.objects.create(
            dispensing=disp_record,
            batch=batch_obj,
            quantity=alloc_qty,
            unit_price=unit_price,
            total_price=total_price,
            stock_transaction=stock_tx
        )
        created_allocations.append(child_alloc)

    # 5.5 Auto-generate MedicineRequest + DispensingInvoice + InvoiceItems for audit & print traceability
    from . import services as legacy_dispensing_services
    req_id = legacy_dispensing_services.generate_unique_request_id()
    med_req = MedicineRequest.objects.create(
        request_id=req_id,
        patient_name=patient_name.strip(),
        patient_id=patient_id.strip(),
        mobile_number=patient_record.phone if (patient_record and patient_record.phone) else '9999999999',
        prescription_number=prescription.prescription_id if prescription else '',
        doctor_name=prescription.doctor_name if prescription else 'Physician Authorized',
        priority='NORMAL',
        request_status=MedicineRequest.RequestStatus.COMPLETED,
        total_items=1
    )

    MedicineRequestItem.objects.create(
        request=med_req,
        medicine=medicine,
        requested_quantity=requested_qty,
        approved_quantity=requested_qty,
        dispensed_quantity=requested_qty,
        item_status=MedicineRequestItem.ItemStatus.DISPENSED,
        dispensed_batch=allocated_batches_plan[0][0] if allocated_batches_plan else None
    )

    inv_num = legacy_dispensing_services.generate_unique_invoice_number()
    subtotal = sum(child_alloc.total_price for child_alloc in created_allocations)
    gst_amount = (subtotal * Decimal('0.05')).quantize(Decimal('0.01'))
    total_amount = subtotal + gst_amount

    disp_invoice = DispensingInvoice.objects.create(
        invoice_number=inv_num,
        request=med_req,
        patient_name=patient_name.strip(),
        patient_id=patient_id.strip(),
        subtotal=subtotal,
        gst_amount=gst_amount,
        discount=Decimal('0.00'),
        total_amount=total_amount,
        payment_status=DispensingInvoice.PaymentStatus.PAID,
        payment_method=DispensingInvoice.PaymentMethod.CASH,
        created_by=pharmacist_user if (pharmacist_user and pharmacist_user.is_authenticated) else None
    )

    for child_alloc in created_allocations:
        InvoiceItem.objects.create(
            invoice=disp_invoice,
            medicine=medicine,
            batch=child_alloc.batch,
            quantity=child_alloc.quantity_allocated,
            unit_price=child_alloc.unit_price,
            total_price=child_alloc.total_price
        )

    disp_record.invoice = disp_invoice
    disp_record.save(update_fields=['invoice'])

    # 6. Recalculate Master Inventory
    inventory_services.recalculate_inventory(inventory)
    inventory.refresh_from_db()

    # 7. Controlled Medicine Register Entry
    if medicine.is_controlled:
        for child_alloc in created_allocations:
            alloc_batch = child_alloc.batch
            alloc_qty = child_alloc.quantity
            batch_closing = alloc_batch.quantity_available
            batch_opening = batch_closing + alloc_qty

            ControlledMedicineRegister.objects.create(
                entry_id=ControlledMedicineRegister.generate_entry_id(),
                medicine=medicine,
                batch=alloc_batch,
                transaction_type=ControlledMedicineRegister.TransactionType.DISPENSE,
                opening_balance=batch_opening,
                quantity_in=0,
                quantity_out=alloc_qty,
                closing_balance=batch_closing,
                patient_name=patient_name.strip(),
                patient_id=patient_id.strip(),
                doctor_name=prescription.doctor_name if prescription else "Physician Authorized",
                prescription_number=prescription.prescription_id if prescription else f"AUTH-{dispensing_id}",
                pharmacist=pharmacist_user,
                dispensing_record=disp_record,
                reference_number=dispensing_id,
                reason=f"Dispensed under record #{dispensing_id}: {alloc_qty} units from batch {alloc_batch.batch_number}."
            )

    # 8. Update Prescription & Item Statuses if attached
    if prescription:
        rx_item = prescription.items.filter(medicine=medicine).first()
        if rx_item:
            rx_item.dispensed_quantity += requested_qty
            if rx_item.dispensed_quantity >= rx_item.prescribed_quantity:
                rx_item.status = PrescriptionItem.Status.DISPENSED
            else:
                rx_item.status = PrescriptionItem.Status.PARTIALLY_DISPENSED
            rx_item.save(update_fields=['dispensed_quantity', 'status'])

        # Check all items of prescription
        all_items = prescription.items.all()
        all_dispensed = all(it.status == PrescriptionItem.Status.DISPENSED for it in all_items)
        any_dispensed = any(it.dispensed_quantity > 0 for it in all_items)

        if all_dispensed:
            prescription.status = Prescription.Status.DISPENSED
        elif any_dispensed:
            prescription.status = Prescription.Status.PARTIALLY_DISPENSED
        prescription.save(update_fields=['status'])

    # 9. Stock Alert Evaluation
    inventory_services.generate_reorder_alerts(inventory)

    # 10. Audit Log
    batch_summary = ", ".join([f"{a.quantity_allocated} from {a.batch_number}" for a in created_allocations])
    log_audit(
        user=pharmacist_user,
        action='SECURE_DISPENSE',
        description=(
            f"Dispensed {requested_qty} units of {medicine.medicine_name} ({batch_summary}) "
            f"for {patient_name} (ID: {patient_id}) under #{dispensing_id}."
        ),
        performed_by=pharmacist_user
    )

    return {
        'success': True,
        'dispensing_record': disp_record,
        'allocations': created_allocations,
        'dispensing_id': dispensing_id,
        'invoice_number': disp_invoice.invoice_number,
        'invoice_id': disp_invoice.id,
        'request_id': med_req.request_id,
        'message': f"Dispensed {requested_qty} units of {medicine.medicine_name} successfully."
    }


# ==============================================================================
# 6. DISPENSING REVERSAL / RETURN WORKFLOW
# ==============================================================================

@transaction.atomic
def reverse_dispensing(dispensing_id, pharmacist_user, reason=''):
    """
    Reverses an existing DispensingRecord atomically.
    Returns allocated quantities back to their respective batches,
    creates RETURN StockTransactions, and updates the DispensingRecord status to REVERSED.
    """
    try:
        record = DispensingRecord.objects.select_for_update().get(
            models.Q(dispensing_id__iexact=dispensing_id) | models.Q(id=int(dispensing_id) if str(dispensing_id).isdigit() else 0)
        )
    except DispensingRecord.DoesNotExist:
        raise ValidationError(f"Dispensing record '{dispensing_id}' not found.")

    if record.status == DispensingRecord.Status.REVERSED:
        raise ValidationError(f"Dispensing record #{record.dispensing_id} is already reversed.")

    medicine = record.medicine
    inventory, _ = Inventory.objects.select_for_update().get_or_create(medicine=medicine)

    allocations = record.allocations.select_related('batch').all()
    if not allocations.exists():
        raise ValidationError("No allocation records found to reverse.")

    for alloc in allocations:
        batch = MedicineBatch.objects.get(pk=alloc.batch_id)
        tx_ref = f"REV-{record.dispensing_id}-{batch.batch_number}"
        inventory_services.record_return(
            medicine=medicine,
            batch=batch,
            quantity=alloc.quantity_allocated,
            performed_by=pharmacist_user,
            reason=reason or 'Patient Return / Clinical Reversal',
            reference_number=f"REV-{record.dispensing_id}",
            notes=f"Reversal of Dispensing #{record.dispensing_id}: {reason or 'Patient Return / Clinical Reversal'}",
            transaction_number=tx_ref
        )

    # Recalculate inventory
    inventory_services.recalculate_inventory(inventory)

    # Update DispensingRecord status
    record.status = DispensingRecord.Status.REVERSED
    record.notes = (record.notes + f"\n[REVERSED] by {pharmacist_user.username} on {timezone.now().strftime('%Y-%m-%d %H:%M')}: {reason}").strip()
    record.save(update_fields=['status', 'notes'])

    # Roll back prescription items if linked
    if record.prescription:
        rx = record.prescription
        rx_item = rx.items.filter(medicine=medicine).first()
        if rx_item:
            rx_item.dispensed_quantity = max(0, rx_item.dispensed_quantity - record.total_quantity)
            if rx_item.dispensed_quantity == 0:
                rx_item.status = PrescriptionItem.Status.VERIFIED
            else:
                rx_item.status = PrescriptionItem.Status.PARTIALLY_DISPENSED
            rx_item.save(update_fields=['dispensed_quantity', 'status'])

        all_items = rx.items.all()
        if all(it.dispensed_quantity == 0 for it in all_items):
            rx.status = Prescription.Status.VERIFIED
        else:
            rx.status = Prescription.Status.PARTIALLY_DISPENSED
        rx.save(update_fields=['status'])

    # If controlled medicine, add adjustment return to ledger
    if medicine.is_controlled:
        for alloc in allocations:
            alloc_batch = alloc.batch
            alloc_qty = alloc.quantity_allocated
            batch_closing = alloc_batch.quantity_available
            batch_opening = batch_closing - alloc_qty
            ControlledMedicineRegister.objects.create(
                entry_id=ControlledMedicineRegister.generate_entry_id(),
                medicine=medicine,
                batch=alloc_batch,
                transaction_type=ControlledMedicineRegister.TransactionType.RETURN,
                opening_balance=batch_opening,
                quantity_in=alloc_qty,
                quantity_out=0,
                closing_balance=batch_closing,
                patient_name=record.patient_name,
                patient_id=record.patient_id,
                doctor_name="Reversal Authorization",
                prescription_number=f"REV-{record.dispensing_id}",
                pharmacist=pharmacist_user,
                dispensing_record=record,
                reference_number=f"REV-{record.dispensing_id}",
                reason=f"Reversal returned {alloc_qty} units to batch {alloc_batch.batch_number}: {reason}"
            )

    log_audit(
        user=pharmacist_user,
        action='DISPENSE_REVERSED',
        description=f"Reversed Dispensing #{record.dispensing_id} ({record.total_quantity} units of {medicine.medicine_name}). Reason: {reason}",
        performed_by=pharmacist_user
    )

    return {
        'success': True,
        'message': f"Dispensing #{record.dispensing_id} successfully reversed and stock restored."
    }


# ==============================================================================
# 7. AUTOMATED DISCREPANCY DETECTION & QUEUE MANAGEMENT
# ==============================================================================

def detect_discrepancies():
    """
    Automated discrepancy scanner:
    1. Compares Inventory current_stock vs Sum of all Batch quantity_available (physical consistency).
    2. Checks Controlled Medicine Register vs Batch physical balance.
    Generates StockDiscrepancy queue items with stable deduplication.
    """
    created_discrepancies = []

    # 1. Batch vs Inventory current_stock mismatch
    for inv in Inventory.objects.select_related('medicine').all():
        med = inv.medicine
        all_batches = list(med.batches.all())
        total_physical = sum(b.quantity_available for b in all_batches)

        if inv.current_stock != total_physical:
            variance = inv.current_stock - total_physical
            # Check if open/investigating discrepancy already exists
            existing = StockDiscrepancy.objects.filter(
                medicine=med,
                batch=None,
                discrepancy_type=StockDiscrepancy.DiscrepancyType.PHYSICAL_VS_SYSTEM,
                status__in=[StockDiscrepancy.Status.OPEN, StockDiscrepancy.Status.UNDER_REVIEW]
            ).first()

            if not existing:
                disc = StockDiscrepancy.objects.create(
                    discrepancy_id=generate_discrepancy_id(),
                    medicine=med,
                    batch=None,
                    discrepancy_type=StockDiscrepancy.DiscrepancyType.PHYSICAL_VS_SYSTEM,
                    expected_quantity=total_physical,
                    actual_quantity=inv.current_stock,
                    difference=variance,
                    severity=StockDiscrepancy.Severity.CRITICAL if med.is_controlled else StockDiscrepancy.Severity.WARNING,
                    reason=f"Inventory aggregate ({inv.current_stock}) deviates from sum of all batch stocks ({total_physical}) by {variance} units.",
                    status=StockDiscrepancy.Status.OPEN
                )
                created_discrepancies.append(disc)
            else:
                if existing.expected_quantity != total_physical or existing.actual_quantity != inv.current_stock:
                    existing.expected_quantity = total_physical
                    existing.actual_quantity = inv.current_stock
                    existing.difference = variance
                    existing.save(update_fields=['expected_quantity', 'actual_quantity', 'difference'])

        # 2. Controlled Medicine Register balance vs Batch quantity
        if med.is_controlled:
            for b in all_batches:
                latest_reg = ControlledMedicineRegister.objects.filter(
                    medicine=med,
                    batch=b
                ).order_by('-recorded_at', '-id').first()

                if latest_reg and latest_reg.closing_balance != b.quantity_available:
                    variance = b.quantity_available - latest_reg.closing_balance
                    existing = StockDiscrepancy.objects.filter(
                        medicine=med,
                        batch=b,
                        discrepancy_type=StockDiscrepancy.DiscrepancyType.CONTROLLED_MISMATCH,
                        status__in=[StockDiscrepancy.Status.OPEN, StockDiscrepancy.Status.UNDER_REVIEW]
                    ).first()

                    if not existing:
                        disc = StockDiscrepancy.objects.create(
                            discrepancy_id=generate_discrepancy_id(),
                            medicine=med,
                            batch=b,
                            discrepancy_type=StockDiscrepancy.DiscrepancyType.CONTROLLED_MISMATCH,
                            expected_quantity=latest_reg.closing_balance,
                            actual_quantity=b.quantity_available,
                            difference=variance,
                            severity=StockDiscrepancy.Severity.CRITICAL,
                            reason=f"Controlled register closing balance ({latest_reg.closing_balance}) does not match physical batch stock ({b.quantity_available}) for batch {b.batch_number}.",
                            status=StockDiscrepancy.Status.OPEN
                        )
                        created_discrepancies.append(disc)

    return created_discrepancies


@transaction.atomic
def resolve_discrepancy(discrepancy_id, pharmacist_user, resolution_notes, action_taken=''):
    """
    Resolves a recorded StockDiscrepancy in the audit queue.
    """
    try:
        disc = StockDiscrepancy.objects.select_for_update().get(
            models.Q(discrepancy_id__iexact=discrepancy_id) | models.Q(id=int(discrepancy_id) if str(discrepancy_id).isdigit() else 0)
        )
    except StockDiscrepancy.DoesNotExist:
        raise ValidationError(f"Discrepancy '{discrepancy_id}' not found.")

    disc.status = StockDiscrepancy.Status.RESOLVED
    disc.resolved_by = pharmacist_user
    disc.resolved_at = timezone.now()
    disc.resolution_notes = (f"{action_taken} | Notes: {resolution_notes}").strip() if action_taken else resolution_notes
    disc.save(update_fields=['status', 'resolved_by', 'resolved_at', 'resolution_notes'])

    log_audit(
        user=pharmacist_user,
        action='DISCREPANCY_RESOLVED',
        description=f"Discrepancy #{disc.discrepancy_id} for {disc.medicine.medicine_name} resolved by {pharmacist_user.username}. Notes: {disc.resolution_notes}",
        performed_by=pharmacist_user
    )

    return disc


# ==============================================================================
# 8. PHYSICAL STOCK RECONCILIATION ENGINE
# ==============================================================================

@transaction.atomic
def reconcile_batch_stock(medicine_id, batch_id, physical_quantity, reason, pharmacist_user):
    """
    Reconciles physical count for a batch vs system records.
    Adjusts batch quantity, creates ADJUSTMENT StockTransaction,
    creates StockReconciliation record, and recalculates Inventory.
    """
    physical_quantity = int(physical_quantity)
    if physical_quantity < 0:
        raise ValidationError("Physical count cannot be negative.")

    try:
        medicine = Medicine.objects.get(pk=medicine_id)
        batch = MedicineBatch.objects.select_for_update().get(pk=batch_id, medicine=medicine)
    except (Medicine.DoesNotExist, MedicineBatch.DoesNotExist):
        raise ValidationError("Medicine or Batch not found.")

    system_quantity = batch.quantity_available
    variance = physical_quantity - system_quantity

    if variance > 0:
        variance_type = StockReconciliation.VarianceType.SURPLUS
    elif variance < 0:
        variance_type = StockReconciliation.VarianceType.DEFICIT
    else:
        variance_type = StockReconciliation.VarianceType.MATCH

    session_id = StockReconciliation.generate_reconciliation_id()

    # Create authoritative Reconciliation session entry with real DB columns
    rec = StockReconciliation.objects.create(
        reconciliation_id=session_id,
        performed_by=pharmacist_user,
        reconciliation_date=timezone.now().date(),
        medicine=medicine,
        batch=batch,
        system_quantity=system_quantity,
        physical_quantity=physical_quantity,
        variance=variance,
        variance_type=variance_type,
        status=StockReconciliation.Status.COMPLETED,
        total_items_checked=1,
        discrepancies_found=1 if variance != 0 else 0,
        notes=reason.strip() if reason else f'Physical audit: Batch {batch.batch_number} {system_quantity} -> {physical_quantity} (Variance: {variance})'
    )

    if variance != 0:
        tx_ref = f"ADJ-{session_id}-{batch.batch_number}"
        adj_type = 'Increase' if variance > 0 else 'Decrease'
        txn = inventory_services.adjust_stock(
            medicine=medicine,
            batch=batch,
            adjustment_type=adj_type,
            quantity=abs(variance),
            performed_by=pharmacist_user,
            reason=f"Physical Reconciliation #{session_id}",
            notes=f"Physical Reconciliation #{session_id}: System {system_quantity} -> Physical {physical_quantity} ({reason})",
            transaction_number=tx_ref,
            reference_number=session_id
        )

        rec.stock_transaction = txn
        rec.save(update_fields=['stock_transaction'])

        # If controlled medicine, record reconciliation entry in ledger
        if medicine.is_controlled:
            ControlledMedicineRegister.objects.create(
                entry_id=ControlledMedicineRegister.generate_entry_id(),
                medicine=medicine,
                batch=batch,
                transaction_type=ControlledMedicineRegister.TransactionType.ADJUSTMENT,
                opening_balance=system_quantity,
                quantity_in=variance if variance > 0 else 0,
                quantity_out=abs(variance) if variance < 0 else 0,
                closing_balance=physical_quantity,
                patient_name="Physical Inventory Audit",
                patient_id="AUDIT-RECON",
                doctor_name="Inventory Auditor",
                prescription_number=f"RECON-{session_id}",
                pharmacist=pharmacist_user,
                reference_number=session_id,
                reason=f"Reconciliation Adjustment Session #{session_id}: Variance {variance} units. ({reason})"
            )

    # Automatically resolve any open StockDiscrepancy for this batch
    open_discs = StockDiscrepancy.objects.filter(
        medicine=medicine,
        batch=batch,
        status__in=[StockDiscrepancy.Status.OPEN, StockDiscrepancy.Status.UNDER_REVIEW]
    )
    for od in open_discs:
        od.status = StockDiscrepancy.Status.RESOLVED
        od.resolved_by = pharmacist_user
        od.resolved_at = timezone.now()
        od.resolution_notes = f"Auto-resolved via Physical Reconciliation #{session_id} (Batch {batch.batch_number} adjusted to {physical_quantity} units)."
        od.save(update_fields=['status', 'resolved_by', 'resolved_at', 'resolution_notes'])

    log_audit(
        user=pharmacist_user,
        action='STOCK_RECONCILIATION',
        description=f"Stock reconciliation for {medicine.medicine_name} (Batch {batch.batch_number}): System {system_quantity} -> Physical {physical_quantity} (Variance: {variance}). Reason: {reason}",
        performed_by=pharmacist_user
    )

    return rec
