"""
integrations/tests.py
Test suite for the Hospital EHR Integration module.

Tests cover:
- FHIR service: validation, medicine matching, idempotent ingestion, dispense generation
- API views: FHIR endpoints, capability statement, mock bundle
- Model integrity: ExternalHospitalSystem, HospitalMedicationOrder, IntegrationMessageLog
- UI views: dashboard, order review, mock simulator (authentication/RBAC)
"""

import json
from django.test import TestCase, Client
from django.core.exceptions import ValidationError

from accounts.models import User
from medicines.models import Medicine, Category
from integrations.models import (
    ExternalHospitalSystem, HospitalMedicationOrder, IntegrationMessageLog
)
from integrations.fhir_service import (
    validate_and_parse_fhir_medication_request,
    find_matching_medicine,
    ingest_fhir_medication_order,
    get_fhir_capability_statement,
)


# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------

SAMPLE_FHIR_MEDICATION_REQUEST = {
    "resourceType": "MedicationRequest",
    "id": "test-order-001",
    "status": "active",
    "intent": "order",
    "medicationCodeableConcept": {
        "text": "Paracetamol 500mg",
        "coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "161"}]
    },
    "subject": {
        "reference": "Patient/P-00123",
        "display": "Test Patient"
    },
    "dispenseRequest": {"quantity": {"value": 10, "unit": "tablet"}},
    "dosageInstruction": [{"text": "1 tablet every 6h"}]
}


def create_test_admin():
    return User.objects.create_user(
        username='admin_fhir_test',
        email='admin_fhir@test.com',
        password='testpass123',
        role=User.Role.ADMIN,
        status='APPROVED',
    )


def create_test_pharmacist():
    return User.objects.create_user(
        username='pharma_fhir_test',
        email='pharma_fhir@test.com',
        password='testpass123',
        role=User.Role.PHARMACIST,
        status='APPROVED',
        email_verified=True,
    )


def create_test_medicine(name="Paracetamol 500mg"):
    cat, _ = Category.objects.get_or_create(name="General")
    med, _ = Medicine.objects.get_or_create(
        medicine_name=name,
        defaults={
            'generic_name': 'Paracetamol',
            'category': cat,
            'unit_of_measure': 'tablet',
            'reorder_level': 100,
            'maximum_stock': 1000,
            'status': 'ACTIVE',
        }
    )
    return med


def create_test_hospital_system(name="Test EHR", code="TEST_EHR_01"):
    return ExternalHospitalSystem.objects.create(
        name=name,
        system_code=code,
        system_type='SIMULATOR',
        endpoint_url='http://localhost:9000',
        is_active=True,
        is_demo_simulator=True,
    )


# ---------------------------------------------------------------------------
# 1. FHIR Validation Tests
# ---------------------------------------------------------------------------

class FHIRValidationTests(TestCase):
    """Tests for validate_and_parse_fhir_medication_request()"""

    def test_valid_payload_returns_parsed_data(self):
        result = validate_and_parse_fhir_medication_request(SAMPLE_FHIR_MEDICATION_REQUEST)
        self.assertIn('raw_medication_name', result)
        self.assertIn('patient_uhid', result)
        self.assertIn('prescribed_quantity', result)
        self.assertEqual(result['raw_medication_name'], 'Paracetamol 500mg')

    def test_wrong_resource_type_raises_validation_error(self):
        bad_payload = dict(SAMPLE_FHIR_MEDICATION_REQUEST)
        bad_payload['resourceType'] = 'Patient'
        with self.assertRaises(ValidationError):
            validate_and_parse_fhir_medication_request(bad_payload)

    def test_empty_payload_raises_validation_error(self):
        with self.assertRaises((ValidationError, Exception)):
            validate_and_parse_fhir_medication_request({})

    def test_missing_medication_name_raises_validation_error(self):
        bad_payload = dict(SAMPLE_FHIR_MEDICATION_REQUEST)
        bad_payload['medicationCodeableConcept'] = {}
        with self.assertRaises(ValidationError):
            validate_and_parse_fhir_medication_request(bad_payload)

    def test_quantity_extracted_from_dispense_request(self):
        result = validate_and_parse_fhir_medication_request(SAMPLE_FHIR_MEDICATION_REQUEST)
        self.assertEqual(result['prescribed_quantity'], 10)

    def test_patient_uhid_extracted_from_subject_reference(self):
        result = validate_and_parse_fhir_medication_request(SAMPLE_FHIR_MEDICATION_REQUEST)
        self.assertIn('P-00123', result['patient_uhid'])

    def test_external_order_id_extracted(self):
        result = validate_and_parse_fhir_medication_request(SAMPLE_FHIR_MEDICATION_REQUEST)
        self.assertEqual(result['external_order_id'], 'test-order-001')


# ---------------------------------------------------------------------------
# 2. Medicine Matching Tests
# ---------------------------------------------------------------------------

class MedicineMatchingTests(TestCase):
    """Tests for find_matching_medicine()"""

    def setUp(self):
        self.med = create_test_medicine("Paracetamol 500mg")

    def test_exact_name_match(self):
        found = find_matching_medicine("Paracetamol 500mg")
        self.assertIsNotNone(found)
        self.assertEqual(found.pk, self.med.pk)

    def test_case_insensitive_match(self):
        found = find_matching_medicine("paracetamol 500mg")
        self.assertIsNotNone(found)
        self.assertEqual(found.pk, self.med.pk)

    def test_partial_name_match(self):
        found = find_matching_medicine("Paracetamol")
        self.assertIsNotNone(found)

    def test_no_match_returns_none(self):
        found = find_matching_medicine("NonExistentDrug XYZ 999")
        self.assertIsNone(found)

    def test_empty_string_returns_none(self):
        found = find_matching_medicine("")
        self.assertIsNone(found)


# ---------------------------------------------------------------------------
# 3. FHIR Ingestion / Idempotency Tests
# ---------------------------------------------------------------------------

class FHIRIngestionTests(TestCase):
    """Tests for ingest_fhir_medication_order() — idempotency and atomic creation"""

    def setUp(self):
        self.hospital_system = create_test_hospital_system("Ingest Test EHR", "INGEST_EHR_01")
        self.medicine = create_test_medicine()
        self.payload = dict(SAMPLE_FHIR_MEDICATION_REQUEST)
        self.payload['id'] = 'idem-test-order-001'

    def test_first_ingestion_creates_order(self):
        order, created = ingest_fhir_medication_order(
            self.payload, self.hospital_system
        )
        self.assertTrue(created)
        self.assertIsNotNone(order)
        self.assertEqual(order.raw_medication_name, 'Paracetamol 500mg')
        self.assertEqual(order.prescribed_quantity, 10)

    def test_second_ingestion_is_idempotent(self):
        order1, created1 = ingest_fhir_medication_order(
            self.payload, self.hospital_system
        )
        order2, created2 = ingest_fhir_medication_order(
            self.payload, self.hospital_system
        )
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(order1.pk, order2.pk)
        # Confirm only 1 DB record exists
        count = HospitalMedicationOrder.objects.filter(
            idempotency_key=order1.idempotency_key
        ).count()
        self.assertEqual(count, 1)

    def test_ingestion_maps_medicine_when_found(self):
        order, _ = ingest_fhir_medication_order(
            self.payload, self.hospital_system
        )
        self.assertIsNotNone(order.mapped_medicine)
        self.assertEqual(order.mapped_medicine.pk, self.medicine.pk)

    def test_ingestion_creates_order_in_pending_review_status(self):
        order, created = ingest_fhir_medication_order(
            self.payload, self.hospital_system
        )
        self.assertTrue(created)
        # Status must be PENDING_REVIEW — pharmacist must manually verify
        self.assertEqual(order.status, HospitalMedicationOrder.OrderStatus.PENDING_REVIEW)

    def test_ingestion_does_not_auto_dispense(self):
        """External order must NOT bypass pharmacist authorization."""
        order, _ = ingest_fhir_medication_order(
            self.payload, self.hospital_system
        )
        self.assertIsNone(order.dispensing_record)
        self.assertNotEqual(order.status, HospitalMedicationOrder.OrderStatus.DISPENSED)

    def test_ingestion_creates_linked_prescription(self):
        order, created = ingest_fhir_medication_order(
            self.payload, self.hospital_system
        )
        self.assertTrue(created)
        self.assertIsNotNone(order.prescription)


# ---------------------------------------------------------------------------
# 4. Model Tests
# ---------------------------------------------------------------------------

class ExternalHospitalSystemModelTests(TestCase):

    def test_create_hospital_system(self):
        sys = create_test_hospital_system("City Hospital EHR", "CITY_EHR_02")
        self.assertEqual(sys.name, "City Hospital EHR")
        self.assertEqual(sys.system_code, "CITY_EHR_02")
        self.assertTrue(sys.is_active)

    def test_system_code_unique_constraint(self):
        create_test_hospital_system("Hospital A", "CODE_UNIQUE_01")
        with self.assertRaises(Exception):
            create_test_hospital_system("Hospital B", "CODE_UNIQUE_01")


class IntegrationMessageLogModelTests(TestCase):

    def setUp(self):
        self.hospital_system = create_test_hospital_system("Log Test EHR", "LOG_EHR_01")

    def test_create_message_log(self):
        log = IntegrationMessageLog.objects.create(
            hospital_system=self.hospital_system,
            direction='INBOUND',
            resource_type='MedicationRequest',
            request_method='POST',
            request_path='/api/v1/fhir/MedicationRequest/',
            status_code=200,
            status='SUCCESS',
            payload={'test': True},
        )
        self.assertIsNotNone(log.pk)
        self.assertEqual(log.direction, 'INBOUND')

    def test_log_created_at_is_auto_set(self):
        log = IntegrationMessageLog.objects.create(
            hospital_system=self.hospital_system,
            direction='OUTBOUND',
            resource_type='MedicationDispense',
            request_method='POST',
            request_path='/api/v1/fhir/MedicationDispense/',
            status_code=201,
            status='SUCCESS',
            payload={},
        )
        self.assertIsNotNone(log.created_at)


# ---------------------------------------------------------------------------
# 5. FHIR Capability Statement Tests
# ---------------------------------------------------------------------------

class FHIRCapabilityStatementTests(TestCase):

    def test_capability_statement_returns_dict(self):
        result = get_fhir_capability_statement()
        self.assertIsInstance(result, dict)

    def test_capability_statement_has_resource_type(self):
        result = get_fhir_capability_statement()
        self.assertEqual(result.get('resourceType'), 'CapabilityStatement')

    def test_capability_statement_has_fhir_version(self):
        result = get_fhir_capability_statement()
        self.assertIn('fhirVersion', result)

    def test_capability_statement_date_is_dynamic(self):
        from django.utils import timezone
        result = get_fhir_capability_statement()
        today_str = timezone.now().date().isoformat()
        self.assertEqual(result.get('date'), today_str)


# ---------------------------------------------------------------------------
# 6. UI View Tests (authentication & RBAC)
# ---------------------------------------------------------------------------

class IntegrationDashboardViewTests(TestCase):

    def setUp(self):
        self.client = Client()
        self.admin = create_test_admin()
        self.pharmacist = create_test_pharmacist()

    def test_unauthenticated_redirects_to_login(self):
        response = self.client.get('/integrations/dashboard/')
        self.assertIn(response.status_code, [302, 301])

    def test_admin_can_access_dashboard(self):
        self.client.login(username='admin_fhir_test', password='testpass123')
        response = self.client.get('/integrations/dashboard/')
        self.assertEqual(response.status_code, 200)

    def test_pharmacist_can_access_dashboard(self):
        self.client.login(username='pharma_fhir_test', password='testpass123')
        response = self.client.get('/integrations/dashboard/')
        self.assertEqual(response.status_code, 200)

    def test_dashboard_header_and_workflow_elements(self):
        """Dashboard renders clean header, buttons, and the 5-stage visual workflow."""
        self.client.login(username='admin_fhir_test', password='testpass123')
        response = self.client.get('/integrations/dashboard/')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()

        # Header elements
        self.assertIn("Hospital & EHR Integration", content)
        self.assertIn("Receive medication orders, review prescriptions, and track dispensing status.", content)
        self.assertIn("Mock EHR Simulator", content)
        self.assertIn("FHIR Metadata", content)

        # 5-stage workflow
        self.assertIn("Receive Order", content)
        self.assertIn("Validate", content)
        self.assertIn("Pharmacist Review", content)
        self.assertIn("Dispensing", content)
        self.assertIn("Synchronization", content)

        # Connected systems and queue sections
        self.assertIn("Connected Hospital & EHR Systems", content)
        self.assertIn("Inbound Medication Orders Queue", content)

    def test_admin_sidebar_removes_django_backend(self):
        """Admin governance sidebar does not show Django Backend link."""
        self.client.login(username='admin_fhir_test', password='testpass123')
        response = self.client.get('/integrations/dashboard/')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertNotIn("Django Backend", content)
        self.assertIn("Pharmacist Review", content)
        self.assertIn("Hospital Integration", content)

    def test_order_review_hold_action(self):
        """Pharmacist can place an order on clinical hold without deducting stock or rejecting."""
        system = create_test_hospital_system()
        order = HospitalMedicationOrder.objects.create(
            external_order_id='HOLD-TEST-001',
            hospital_system=system,
            idempotency_key='HOLD_KEY_001',
            patient_uhid='UHID-HOLD-01',
            patient_name='Hold Patient',
            raw_medication_name='Amoxicillin 500mg',
            status=HospitalMedicationOrder.OrderStatus.PENDING_REVIEW,
        )
        self.client.login(username='pharma_fhir_test', password='testpass123')
        resp = self.client.post(f'/integrations/orders/{order.id}/review/', {
            'action': 'hold',
            'hold_reason': 'Awaiting creatinine clearance lab result from physician'
        })
        self.assertRedirects(resp, '/integrations/')
        order.refresh_from_db()
        self.assertEqual(order.status, HospitalMedicationOrder.OrderStatus.PENDING_REVIEW)
        self.assertIn("[Clinical Hold]", order.rejection_reason)



# ---------------------------------------------------------------------------
# 7. FHIR API Endpoint Tests
# ---------------------------------------------------------------------------

class FHIRAPIEndpointTests(TestCase):

    def setUp(self):
        self.client = Client()
        self.hospital_system = create_test_hospital_system("API Test EHR", "API_EHR_01")
        self.medicine = create_test_medicine()

    def test_capability_statement_endpoint(self):
        response = self.client.get('/api/v1/fhir/metadata/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data.get('resourceType'), 'CapabilityStatement')

    def test_post_valid_fhir_medication_request(self):
        payload = dict(SAMPLE_FHIR_MEDICATION_REQUEST)
        payload['id'] = 'api-test-order-unique-001'
        response = self.client.post(
            '/api/v1/fhir/MedicationRequest/',
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_X_MEDIVAULT_SYSTEM_CODE='API_EHR_01',
        )
        # Accept 200 (existing) or 201 (created) or 202 (accepted)
        self.assertIn(response.status_code, [200, 201, 202])

    def test_post_invalid_resource_type_rejected(self):
        bad_payload = {'resourceType': 'Patient', 'id': 'bad-001'}
        response = self.client.post(
            '/api/v1/fhir/MedicationRequest/',
            data=json.dumps(bad_payload),
            content_type='application/json',
        )
        self.assertIn(response.status_code, [400, 422])

    def test_post_empty_payload_rejected(self):
        response = self.client.post(
            '/api/v1/fhir/MedicationRequest/',
            data='{}',
            content_type='application/json',
        )
        self.assertIn(response.status_code, [400, 422])
