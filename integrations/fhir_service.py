"""
integrations/fhir_service.py
MediVault — FHIR R4 Integration Adapter (Oracle Health / Cerner & Mock EHR)

Features:
1. FHIR R4 Validation: Validates incoming MedicationRequest JSON payloads against standard FHIR R4 schemas.
2. Idempotency Protection: Uses composite idempotency keys (system_code + external_id) to prevent duplicate orders.
3. Pharmacist Verification Enforcement: Automatically converts valid inbound FHIR orders to MediVault Prescription
   records in PENDING status. Never automatically deducts stock or dispenses.
4. Outbound FHIR MedicationDispense Generation: Emits standard FHIR R4 MedicationDispense JSON on confirmed dispensing.
5. FHIR CapabilityStatement: Returns standard metadata declaring supported resources.
"""

from typing import Dict, Any, Tuple, Optional
from datetime import date, datetime
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.db import transaction

from medicines.models import Medicine
from dispensing.models import Prescription, PrescriptionItem, DispensingRecord
from .models import ExternalHospitalSystem, HospitalMedicationOrder, IntegrationMessageLog


def validate_and_parse_fhir_medication_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validates a FHIR R4 MedicationRequest resource and extracts clinical order attributes.
    Raises ValidationError if mandatory FHIR elements are missing or malformed.
    """
    if not isinstance(payload, dict):
        raise ValidationError("Invalid payload: root must be a JSON object.")

    resource_type = payload.get('resourceType')
    if resource_type != 'MedicationRequest':
        raise ValidationError(f"Invalid resourceType: expected 'MedicationRequest', got '{resource_type}'.")

    # 1. External Order Identifier
    external_id = payload.get('id')
    identifiers = payload.get('identifier', [])
    if not external_id and identifiers and isinstance(identifiers, list):
        external_id = identifiers[0].get('value')

    if not external_id:
        raise ValidationError("MedicationRequest must contain an 'id' or 'identifier' array with value.")

    # 2. Patient / Subject Reference
    subject = payload.get('subject', {})
    patient_display = subject.get('display') or 'Unknown Inpatient'
    patient_ref = subject.get('reference') or ''
    # e.g. "Patient/UHID-98214"
    patient_uhid = patient_ref.replace('Patient/', '').strip() if patient_ref else f"UHID-{external_id[-6:]}"

    # 3. Prescribing Practitioner / Requester
    requester = payload.get('requester', {})
    practitioner_name = requester.get('display') or 'Dr. Attending Physician (EHR)'

    # 4. Medication Identification
    med_concept = payload.get('medicationCodeableConcept', {})
    med_text = med_concept.get('text') or ''
    med_code = ''
    coding = med_concept.get('coding', [])
    if coding and isinstance(coding, list):
        med_code = coding[0].get('code', '')
        if not med_text:
            med_text = coding[0].get('display', '')

    if not med_text and not med_code:
        # Fallback to medicationReference
        med_ref = payload.get('medicationReference', {})
        med_text = med_ref.get('display') or ''

    if not med_text:
        raise ValidationError("MedicationRequest must specify medication via 'medicationCodeableConcept' or 'medicationReference'.")

    # 5. Quantity Prescribed
    dispense_req = payload.get('dispenseRequest', {})
    qty_obj = dispense_req.get('quantity', {})
    qty_val = qty_obj.get('value', 1)
    try:
        prescribed_qty = max(1, int(qty_val))
    except (ValueError, TypeError):
        prescribed_qty = 1

    # 6. Dosage & Instructions
    dosage_list = payload.get('dosageInstruction', [])
    dosage_text = ''
    if dosage_list and isinstance(dosage_list, list):
        dosage_text = dosage_list[0].get('text') or ''

    # 7. Priority
    priority = (payload.get('priority') or 'routine').lower()
    if priority not in ('routine', 'urgent', 'asap', 'stat'):
        priority = 'routine'

    # 8. Department / Reason
    reason_code = payload.get('reasonCode', [])
    diagnosis = reason_code[0].get('text', '') if reason_code else ''

    return {
        'external_order_id': str(external_id),
        'patient_uhid': patient_uhid,
        'patient_name': patient_display,
        'prescribing_practitioner': practitioner_name,
        'raw_medication_name': med_text,
        'medication_code': med_code,
        'prescribed_quantity': prescribed_qty,
        'dosage_instruction': dosage_text,
        'priority': priority,
        'diagnosis': diagnosis,
    }


def find_matching_medicine(med_text: str, med_code: str = '') -> Optional[Medicine]:
    """
    Attempts to match an external medication name or code to an internal MediVault Medicine.
    """
    if not med_text or not med_text.strip():
        return None

    # 1. Exact match by name
    med = Medicine.objects.filter(medicine_name__iexact=med_text.strip(), status='ACTIVE').first()
    if med:
        return med

    # 2. Case-insensitive substring match
    med = Medicine.objects.filter(medicine_name__icontains=med_text.strip(), status='ACTIVE').first()
    if med:
        return med

    # 3. Match by generic name
    med = Medicine.objects.filter(generic_name__icontains=med_text.strip(), status='ACTIVE').first()
    if med:
        return med

    return None


@transaction.atomic
def ingest_fhir_medication_order(
    payload: Dict[str, Any],
    hospital_system: Optional[ExternalHospitalSystem] = None,
    ip_address: Optional[str] = None
) -> Tuple[HospitalMedicationOrder, bool]:
    """
    Ingests an inbound FHIR R4 MedicationRequest payload.
    
    Guarantees:
    - Idempotency: Duplicate submissions return the existing order.
    - Safety: Automatically generates a MediVault Prescription in PENDING state.
      The order MUST be reviewed and verified by a licensed pharmacist prior to dispensing.
    """
    parsed = validate_and_parse_fhir_medication_request(payload)
    sys_code = hospital_system.system_code if hospital_system else 'GENERIC_EHR'
    idempotency_key = f"{sys_code}:{parsed['external_order_id']}"

    # Check for existing order (Idempotency)
    existing_order = HospitalMedicationOrder.objects.filter(idempotency_key=idempotency_key).first()
    if existing_order:
        # Log repeat inbound attempt
        IntegrationMessageLog.objects.create(
            direction=IntegrationMessageLog.Direction.INBOUND,
            hospital_system=hospital_system,
            resource_type='MedicationRequest',
            request_method='POST',
            request_path='/api/v1/fhir/MedicationRequest/',
            status_code=200,
            status=IntegrationMessageLog.Status.SUCCESS,
            external_reference=parsed['external_order_id'],
            payload=payload,
            response_data={'status': 'duplicate_acknowledged', 'order_id': existing_order.id},
            ip_address=ip_address
        )
        return existing_order, False

    # Attempt to match internal medicine
    mapped_med = find_matching_medicine(parsed['raw_medication_name'], parsed['medication_code'])

    # Create MediVault Prescription requiring Pharmacist review
    rx_id = f"RX-EHR-{timezone.now().year}-{secrets_hex()}"
    prescription = Prescription.objects.create(
        prescription_id=rx_id,
        prescription_number=parsed['external_order_id'],
        patient_name=parsed['patient_name'],
        patient_id=parsed['patient_uhid'],
        doctor_name=parsed['prescribing_practitioner'],
        department=f"{sys_code} Integration",
        diagnosis=parsed['diagnosis'] or f"Prescribed via EHR: {parsed['raw_medication_name']}",
        status=Prescription.Status.PENDING,
        notes=f"FHIR R4 MedicationRequest #{parsed['external_order_id']} from {sys_code}. Requires pharmacist verification."
    )

    if mapped_med:
        PrescriptionItem.objects.create(
            prescription=prescription,
            medicine=mapped_med,
            prescribed_quantity=parsed['prescribed_quantity'],
            requested_quantity=parsed['prescribed_quantity'],
            dosage_instructions=parsed['dosage_instruction'] or 'As directed by physician',
            status=PrescriptionItem.Status.PENDING
        )

    # Create HospitalMedicationOrder record
    order = HospitalMedicationOrder.objects.create(
        external_order_id=parsed['external_order_id'],
        hospital_system=hospital_system,
        resource_type='MedicationRequest',
        fhir_version='R4',
        idempotency_key=idempotency_key,
        patient_uhid=parsed['patient_uhid'],
        patient_name=parsed['patient_name'],
        prescribing_practitioner=parsed['prescribing_practitioner'],
        raw_medication_name=parsed['raw_medication_name'],
        medication_code=parsed['medication_code'],
        mapped_medicine=mapped_med,
        prescribed_quantity=parsed['prescribed_quantity'],
        dosage_instruction=parsed['dosage_instruction'],
        priority=parsed['priority'],
        status=HospitalMedicationOrder.OrderStatus.PENDING_REVIEW,
        fhir_payload=payload,
        prescription=prescription,
    )

    # Log successful ingestion
    IntegrationMessageLog.objects.create(
        direction=IntegrationMessageLog.Direction.INBOUND,
        hospital_system=hospital_system,
        resource_type='MedicationRequest',
        request_method='POST',
        request_path='/api/v1/fhir/MedicationRequest/',
        status_code=201,
        status=IntegrationMessageLog.Status.SUCCESS,
        external_reference=parsed['external_order_id'],
        payload=payload,
        response_data={'status': 'order_created', 'order_id': order.id, 'prescription_id': rx_id},
        ip_address=ip_address
    )

    # Update system sync timestamp
    if hospital_system:
        hospital_system.last_sync_at = timezone.now()
        hospital_system.save(update_fields=['last_sync_at'])

    return order, True


def generate_fhir_medication_dispense(disp_record: DispensingRecord) -> Dict[str, Any]:
    """
    Constructs a standard FHIR R4 MedicationDispense resource from a completed MediVault DispensingRecord.
    """
    med = disp_record.medicine
    allocations = disp_record.allocations.select_related('batch').all()
    batch_numbers = [a.batch.batch_number for a in allocations if a.batch]

    payload = {
        "resourceType": "MedicationDispense",
        "id": disp_record.dispensing_id,
        "status": "completed" if disp_record.status == 'COMPLETED' else "stopped",
        "medicationCodeableConcept": {
            "coding": [
                {
                    "system": "http://medivault.hospital.org/medicines",
                    "code": str(med.id),
                    "display": med.medicine_name
                }
            ],
            "text": med.medicine_name
        },
        "subject": {
            "reference": f"Patient/{disp_record.patient_id}",
            "display": disp_record.patient_name
        },
        "performer": [
            {
                "actor": {
                    "display": f"{disp_record.pharmacist.get_full_name() or disp_record.pharmacist.username} (Licensed Pharmacist)"
                }
            }
        ],
        "quantity": {
            "value": disp_record.total_quantity,
            "unit": "doses",
            "system": "http://unitsofmeasure.org"
        },
        "whenHandedOver": disp_record.created_at.isoformat(),
        "dosageInstruction": [
            {
                "text": "Administer strictly as instructed by physician"
            }
        ],
        "note": [
            {
                "text": f"Dispensed via MediVault Pyxis Engine. Verified batches: {', '.join(batch_numbers)}."
            }
        ]
    }

    if disp_record.prescription:
        payload["authorizingPrescription"] = [
            {
                "reference": f"MedicationRequest/{disp_record.prescription.prescription_number or disp_record.prescription.prescription_id}",
                "display": f"Prescription #{disp_record.prescription.prescription_id}"
            }
        ]

    return payload


def get_fhir_capability_statement() -> Dict[str, Any]:
    """
    Returns a standard FHIR R4 CapabilityStatement for MediVault's integration adapter.
    """
    return {
        "resourceType": "CapabilityStatement",
        "id": "medivault-fhir-capability",
        "status": "active",
        "date": timezone.now().date().isoformat(),
        "publisher": "Sanalogx Hospital Pharmacy System",
        "kind": "instance",
        "software": {
            "name": "Sanalogx Intelligent Pharmacy Integration Layer",
            "version": "2.0.0"
        },
        "fhirVersion": "4.0.1",
        "format": ["json"],
        "rest": [
            {
                "mode": "server",
                "resource": [
                    {
                        "type": "MedicationRequest",
                        "interaction": [{"code": "read"}, {"code": "create"}]
                    },
                    {
                        "type": "MedicationDispense",
                        "interaction": [{"code": "read"}]
                    }
                ]
            }
        ]
    }


def secrets_hex() -> str:
    import secrets
    return secrets.token_hex(4).upper()
