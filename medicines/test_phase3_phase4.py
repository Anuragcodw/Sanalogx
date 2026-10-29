"""
medicines/test_phase3_phase4.py
Comprehensive Test Suite for Sanalogx Phase 3 & Phase 4 Implementation

Tests Cover:
1. Phase 1 Internal barcode lookup returns exact MySQL database record.
2. Verified medicine profile displays all required metadata, storage instructions, and warnings.
3. Missing medicine-use information is not fabricated (displays "Medicine-use information not available").
4. Real-time batch-level stock, usable stock, expired stock, quarantined stock match MySQL state.
5. External product data service queries trusted source, handles offline/unconfigured gracefully,
   clearly distinguishes external data, and NEVER mutates local database records.
6. AI-Assisted Summary synthesizes strictly verified local DB data; never invents indications,
   dosages, ingredients, or batches; includes explicit clinical disclaimers.
7. OCR Packaging Recognition: text extraction, candidate field parsing (strength, form, batch, expiry),
   confidence scoring (HIGH/MEDIUM/LOW), and mandatory staff confirmation.
8. OCR and AI suggestions are non-deducting and NEVER mutate inventory or create records.
9. Unknown barcode mapping: authorized staff only, rejects duplicates, rejects overwriting, logs audit.
10. Role-based access control and security: Doctor role blocked (403), unauthenticated blocked,
    file size limits (>5MB rejected), and code-injection resilience.
11. End-to-end integration: Barcode Registry -> Generate Barcode -> Scan -> Fetch Profile -> Verify.
"""

import io
from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch, MagicMock
from PIL import Image, ImageDraw

from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile

from accounts.models import User, PharmacistProfile, AuditLog
from medicines.models import Medicine, MedicineBatch, Category
from inventory.models import Inventory
from medicines import barcode_service, ocr_service, product_info_service, ai_summary_service


class Phase3Phase4ComprehensiveTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        # 1. Admin user
        cls.admin_user = User.objects.create_user(
            username='admin_p34',
            email='admin_p34@hospital.com',
            password='Password@123',
            role=User.Role.ADMIN,
            status=User.Status.APPROVED,
            email_verified=True,
            is_staff=True,
        )

        # 2. Approved Pharmacist user
        cls.pharmacist_user = User.objects.create_user(
            username='pharm_p34',
            email='pharm_p34@hospital.com',
            password='Password@123',
            role=User.Role.PHARMACIST,
            status=User.Status.APPROVED,
            email_verified=True,
        )
        cls.pharmacist_profile = PharmacistProfile.objects.create(
            user=cls.pharmacist_user,
            registration_number='PH-P34-01',
            qualification='B.Pharm',
            is_demo=False,
            verification_status='VERIFIED',
        )

        # 3. Doctor user (unauthorized for pharmacy barcode / OCR management)
        cls.doctor_user = User.objects.create_user(
            username='doctor_p34',
            email='doctor_p34@hospital.com',
            password='Password@123',
            role=User.Role.DOCTOR,
            status=User.Status.APPROVED,
            email_verified=True,
        )

        # Category 1: Cardiology
        cls.category_cardio = Category.objects.create(
            name='Cardiovascular System',
            code='CARDIO_SYS',
            description='Cardiovascular and anti-hypertensive therapeutics',
            notes='Monitor blood pressure and electrolyte balance periodically.'
        )

        # Category 2: Dermatology with empty descriptions
        cls.category_empty = Category.objects.create(
            name='Dermatologicals',
            code='DERMA_GEN',
            description='',
            notes=''
        )

        today = timezone.localdate()

        # Medicine 1: Verified Medicine with complete clinical indications and multiple batches
        cls.med_atorvastatin = Medicine.objects.create(
            medicine_name='Atorvastatin 20mg Tablets',
            generic_name='Atorvastatin Calcium',
            brand_name='Atorva 20',
            category=cls.category_cardio,
            dosage_form='Tablet',
            strength='20mg',
            manufacturer='Sun Pharmaceutical Industries Ltd',
            unit_of_measure='Strip of 10',
            barcode='8901112229999',
            qr_code='QR-ATORVA-20',
            description='Treatment of hypercholesterolemia and prevention of cardiovascular events.',
            storage_condition='Store below 25°C in a dry place. Protect from moisture.',
            reorder_level=50,
            minimum_stock=20,
            maximum_stock=300,
            safety_stock=25,
            mrp=Decimal('120.00'),
            regulatory_category=Medicine.RegulatoryCategory.SCHEDULE_H,
            status=Medicine.Status.ACTIVE,
            verification_status=Medicine.VerificationStatus.VERIFIED,
        )

        # Inventory for Atorvastatin
        cls.inv_atorva = Inventory.objects.create(
            medicine=cls.med_atorvastatin,
            current_stock=160,
            available_stock=100,
            minimum_stock=20,
            maximum_stock=300,
            reorder_level=50,
            safety_stock=25,
        )

        # Batches for Atorvastatin
        # Batch 1: Active, valid (60 units)
        cls.batch_atorva_1 = MedicineBatch.objects.create(
            medicine=cls.med_atorvastatin,
            batch_number='ATV-2026-01',
            quantity_received=100,
            quantity_available=60,
            expiry_date=today + timedelta(days=180),
            manufacturing_date=today - timedelta(days=30),
            purchase_price=Decimal('50.00'),
            selling_price=Decimal('110.00'),
            status=MedicineBatch.BatchStatus.ACTIVE,
            storage_condition='Store below 25°C',
        )

        # Batch 2: Near Expiry (40 units, FEFO #1)
        cls.batch_atorva_2 = MedicineBatch.objects.create(
            medicine=cls.med_atorvastatin,
            batch_number='ATV-2026-FEFO',
            quantity_received=50,
            quantity_available=40,
            expiry_date=today + timedelta(days=20),
            manufacturing_date=today - timedelta(days=300),
            purchase_price=Decimal('48.00'),
            selling_price=Decimal('105.00'),
            status=MedicineBatch.BatchStatus.NEAR_EXPIRY,
            storage_condition='Store below 25°C',
        )

        # Batch 3: Expired (35 units)
        cls.batch_atorva_3 = MedicineBatch.objects.create(
            medicine=cls.med_atorvastatin,
            batch_number='ATV-2025-EXP',
            quantity_received=50,
            quantity_available=35,
            expiry_date=today - timedelta(days=15),
            manufacturing_date=today - timedelta(days=400),
            purchase_price=Decimal('45.00'),
            selling_price=Decimal('100.00'),
            status=MedicineBatch.BatchStatus.EXPIRED,
            storage_condition='Store below 25°C',
        )

        # Batch 4: Quarantined (25 units)
        cls.batch_atorva_4 = MedicineBatch.objects.create(
            medicine=cls.med_atorvastatin,
            batch_number='ATV-2026-QUAR',
            quantity_received=30,
            quantity_available=25,
            expiry_date=today + timedelta(days=90),
            manufacturing_date=today - timedelta(days=40),
            purchase_price=Decimal('48.00'),
            selling_price=Decimal('105.00'),
            status=MedicineBatch.BatchStatus.QUARANTINED,
            storage_condition='Quarantine storage',
        )

        # Medicine 2: Medicine without clinical indications or descriptions (to test exact fallback)
        cls.med_empty_info = Medicine.objects.create(
            medicine_name='Permethrin 5% Lotion',
            generic_name='Permethrin',
            brand_name='',
            category=cls.category_empty,
            dosage_form='Lotion',
            strength='5% w/v',
            manufacturer='Glenmark Pharmaceuticals',
            unit_of_measure='Bottle',
            barcode=None,
            qr_code=None,
            description='',
            review_note='',
            storage_condition='',
            reorder_level=30,
            minimum_stock=10,
            maximum_stock=150,
            safety_stock=15,
            mrp=Decimal('85.00'),
            regulatory_category=Medicine.RegulatoryCategory.NORMAL,
            status=Medicine.Status.ACTIVE,
            verification_status=Medicine.VerificationStatus.VERIFIED,
        )

    def setUp(self):
        self.client = Client()

    # ── 1. Phase 1 & 2 Integrity Checks ──────────────────────────────────────
    def test_internal_barcode_resolves_exact_mysql_record(self):
        """Scanning internal barcode SAN-MED-000... resolves exact medicine and batches."""
        self.client.force_login(self.pharmacist_user)
        self.med_atorvastatin.refresh_from_db()
        code = self.med_atorvastatin.effective_internal_barcode

        res = self.client.get(reverse('medicine_barcode_lookup_api') + f"?code={code}")
        self.assertEqual(res.status_code, 200)
        data = res.json()

        self.assertTrue(data['success'])
        self.assertEqual(data['type'], 'MEDICINE')
        self.assertEqual(data['barcode_type'], 'INTERNAL_SANALOGX')
        self.assertEqual(data['medicine']['id'], self.med_atorvastatin.id)
        self.assertEqual(data['medicine']['medicine_name'], 'Atorvastatin 20mg Tablets')
        self.assertEqual(data['medicine']['generic_name'], 'Atorvastatin Calcium')
        self.assertEqual(data['medicine']['strength'], '20mg')
        self.assertEqual(data['medicine']['dosage_form'], 'Tablet')
        self.assertEqual(data['medicine']['manufacturer'], 'Sun Pharmaceutical Industries Ltd')
        self.assertEqual(data['medicine']['category'], 'Cardiovascular System')

    def test_stock_calculations_and_batch_accuracy(self):
        """Batch-wise and usable stock strictly matches database records."""
        self.client.force_login(self.pharmacist_user)
        code = self.med_atorvastatin.effective_internal_barcode

        res = self.client.get(reverse('medicine_barcode_lookup_api') + f"?code={code}")
        data = res.json()['medicine']

        # Total Physical: 60 + 40 + 35 + 25 = 160
        self.assertEqual(data['total_stock'], 160)
        # Usable Stock (strictly excluding expired & quarantined): 60 + 40 = 100
        self.assertEqual(data['usable_stock'], 100)
        # Expired: 35
        self.assertEqual(data['expired_stock'], 35)
        # Quarantined: 25
        self.assertEqual(data['quarantined_stock'], 25)
        # Unavailable: 60
        self.assertEqual(data['unavailable_stock'], 60)

        # Batch FEFO breakdown
        batches = data['batches']
        self.assertEqual(len(batches), 4)
        fefo_first = next(b for b in batches if b['is_fefo_first'])
        self.assertEqual(fefo_first['batch_number'], 'ATV-2026-FEFO')

    # ── 2. Phase 3: Clinical Information & Non-Fabrication Fallback ───────────
    def test_verified_clinical_information_when_available(self):
        """Returns verified indications and clinical notes from database."""
        self.client.force_login(self.pharmacist_user)
        code = self.med_atorvastatin.effective_internal_barcode

        res = self.client.get(reverse('medicine_barcode_lookup_api') + f"?code={code}")
        data = res.json()['medicine']

        self.assertIn('Treatment of hypercholesterolemia', data['verified_medicine_use'])
        self.assertIn('Store below 25°C', data['storage_instructions'])
        self.assertEqual(len(data['warnings']), 3)  # Rx required + expired notice + quarantined notice

    def test_missing_medicine_use_information_fallback_never_fabricated(self):
        """When clinical information is absent, returns exact fallback string without inventing."""
        self.client.force_login(self.pharmacist_user)
        self.med_empty_info.refresh_from_db()
        code = self.med_empty_info.effective_internal_barcode

        res = self.client.get(reverse('medicine_barcode_lookup_api') + f"?code={code}")
        data = res.json()['medicine']

        self.assertEqual(data['verified_medicine_use'], 'Medicine-use information not available')

    # ── 3. Phase 3: External Product Information Service ──────────────────────
    def test_external_product_lookup_disabled_by_default(self):
        """When external lookup is not enabled, gracefully returns unavailable status."""
        ext_res = product_info_service.lookup_external_product_info(
            '8901112229999',
            generic_name='Atorvastatin Calcium'
        )
        self.assertFalse(ext_res['available'])
        self.assertTrue(ext_res['is_external'])
        self.assertFalse(ext_res['verified_locally'])
        self.assertIn('not configured or disabled', ext_res['message'])

    def test_external_product_lookup_with_mocked_provider_never_alters_db(self):
        """External provider returns reference data and NEVER mutates MySQL database records."""
        mock_response = {
            'results': [{
                'openfda': {
                    'brand_name': ['Lipitor'],
                    'generic_name': ['Atorvastatin Calcium'],
                    'manufacturer_name': ['Pfizer Laboratories Div Pfizer Inc'],
                    'substance_name': ['ATORVASTATIN CALCIUM'],
                    'route': ['ORAL']
                },
                'indications_and_usage': ['Lipitor is an HMG-CoA reductase inhibitor indicated for prevention of CV disease.'],
                'storage_and_handling': ['Store at 20-25C (68-77F); excursions permitted between 15-30C.']
            }]
        }

        with patch('medicines.product_info_service.is_external_lookup_enabled', return_value=True):
            with patch('urllib.request.urlopen') as mock_urlopen:
                mock_ctx = MagicMock()
                mock_ctx.status = 200
                import json
                mock_ctx.read.return_value = json.dumps(mock_response).encode('utf-8')
                mock_urlopen.return_value.__enter__.return_value = mock_ctx

                result = product_info_service.lookup_external_product_info(
                    '8901112229999',
                    generic_name='Atorvastatin'
                )

                self.assertTrue(result['available'])
                self.assertEqual(result['source'], 'OpenFDA Drug Label Database')
                self.assertTrue(result['is_external'])
                self.assertFalse(result['verified_locally'])
                self.assertEqual(result['data']['brand_name'], 'Lipitor')
                self.assertIn('HMG-CoA reductase inhibitor', result['data']['indications_summary'])

        # Verify local database was NOT altered
        self.med_atorvastatin.refresh_from_db()
        self.assertEqual(self.med_atorvastatin.brand_name, 'Atorva 20')  # Still original local brand!
        self.assertEqual(self.med_atorvastatin.manufacturer, 'Sun Pharmaceutical Industries Ltd')  # Still local mfr!

    # ── 4. Phase 3: AI-Assisted Clinical Summary Service ──────────────────────
    def test_ai_summary_uses_strictly_verified_database_data(self):
        """AI summary synthesizes only verified database fields and approved knowledge monographs."""
        summary = ai_summary_service.generate_medicine_ai_summary(self.med_atorvastatin)

        self.assertTrue(summary['is_ai_generated'])
        self.assertEqual(summary['badge'], 'AI-Assisted Formulary Summary')
        self.assertIn('Atorvastatin 20mg Tablets', summary['overview'])
        self.assertIn('Atorvastatin Calcium', summary['overview'])
        self.assertIn('20mg', summary['overview'])
        self.assertIn('Derived strictly from verified Sanalogx hospital catalogue records', summary['disclaimer'])
        self.assertIn('treating physician', summary['dosing_mandate'].lower())

    def test_ai_summary_does_not_invent_missing_indications(self):
        """When medicine indications are missing, AI does not fabricate clinical uses."""
        summary = ai_summary_service.generate_medicine_ai_summary(self.med_empty_info)

        self.assertIn('Medicine-use information not available in local formulary records', summary['overview'])
        self.assertIn('Permethrin', summary['overview'])
        self.assertIn('5% w/v', summary['overview'])

    def test_ai_summary_endpoint(self):
        """API endpoint returns structured AI summary for approved pharmacists."""
        self.client.force_login(self.pharmacist_user)
        url = reverse('medicine_ai_summary_api', kwargs={'pk': self.med_atorvastatin.pk})
        res = self.client.get(url)

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['medicine_id'], self.med_atorvastatin.id)
        self.assertTrue(data['summary']['is_ai_generated'])

    # ── 5. Phase 3: OCR Packaging Recognition Service ─────────────────────────
    def test_ocr_availability_check(self):
        """Detects whether Tesseract OCR is available in the current environment."""
        avail, msg = ocr_service.is_ocr_available()
        # In this Windows system, Tesseract is installed at C:\Program Files\Tesseract-OCR\tesseract.exe
        self.assertTrue(avail)
        self.assertIn('Tesseract OCR is available', msg)

    def test_ocr_field_parsing_strengths_batches_expiries(self):
        """Regex parser accurately extracts strengths, batch numbers, dosage forms, and expiries."""
        sample_text = """
        CIPLA HEALTHCARE LTD
        Amoxicillin Capsules IP 500 mg
        Batch No: AMX-2026-B1
        MFG. DATE: 01/2026
        EXP. DATE: 12/2028
        Store below 25 deg C.
        """
        fields = ocr_service.parse_candidate_fields(sample_text)

        self.assertIn('500 mg', fields['strengths'])
        self.assertIn('Capsules', fields['dosage_forms'])
        self.assertIn('AMX-2026-B1', fields['batch_numbers'])
        self.assertIn('12/2028', fields['expiry_dates'])

    def test_ocr_packaging_matching_against_mysql_catalogue(self):
        """Parsed OCR text matches existing medicines and scores confidence."""
        sample_text = """
        Atorva 20
        Atorvastatin Calcium Tablets 20mg
        Sun Pharmaceutical Industries Ltd
        Batch: ATV-2026-01  Exp: 10/2027
        """
        fields = ocr_service.parse_candidate_fields(sample_text)
        candidates = ocr_service.match_packaging_to_catalogue(sample_text, fields)

        self.assertTrue(len(candidates) >= 1)
        top_cand = candidates[0]
        self.assertEqual(top_cand['medicine_id'], self.med_atorvastatin.id)
        self.assertEqual(top_cand['confidence_level'], 'HIGH')
        self.assertTrue(top_cand['confidence_score'] >= 75)
        self.assertEqual(top_cand['matched_batch_number'], 'ATV-2026-01')
        self.assertTrue(top_cand['requires_confirmation'])
        self.assertTrue(top_cand['is_suggestion'])

    def test_ocr_upload_api_with_generated_image(self):
        """POST /medicines/barcodes/ocr/ processes image, extracts text, returns candidates."""
        self.client.force_login(self.pharmacist_user)

        # Create a simple synthetic image in memory with medicine name
        img = Image.new('RGB', (500, 150), color=(255, 255, 255))
        d = ImageDraw.Draw(img)
        d.text((20, 30), 'Atorvastatin 20mg Tablets', fill=(0, 0, 0))
        d.text((20, 70), 'Batch: ATV-2026-01', fill=(0, 0, 0))

        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)

        upload_file = SimpleUploadedFile(
            'packaging_test.png',
            buf.getvalue(),
            content_type='image/png'
        )

        res = self.client.post(
            reverse('medicine_ocr_scan_api'),
            {'image': upload_file}
        )

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertTrue(
            'Atorvastatin' in data['raw_text'] or 'orvastatin' in data['raw_text'] or '20mg' in data['raw_text']
        )
        self.assertTrue(len(data['candidates']) >= 1)
        self.assertEqual(data['candidates'][0]['medicine_id'], self.med_atorvastatin.id)
        self.assertTrue(data['candidates'][0]['requires_confirmation'])

    def test_ocr_file_size_limit_rejection(self):
        """Files exceeding 5MB are strictly rejected."""
        self.client.force_login(self.pharmacist_user)

        # 6 MB fake file
        huge_file = SimpleUploadedFile(
            'huge_image.png',
            b'x' * (6 * 1024 * 1024),
            content_type='image/png'
        )

        res = self.client.post(
            reverse('medicine_ocr_scan_api'),
            {'image': huge_file}
        )
        self.assertEqual(res.status_code, 400)
        data = res.json()
        self.assertFalse(data['success'])
        self.assertIn('exceeds maximum allowed limit', data['message'])

    def test_ocr_never_mutates_inventory_or_batches(self):
        """OCR scan operations are strictly read-only and never alter stock quantities."""
        self.client.force_login(self.pharmacist_user)
        initial_stock = self.med_atorvastatin.current_stock
        initial_batches_count = self.med_atorvastatin.batches.count()

        img = Image.new('RGB', (300, 100), color=(255, 255, 255))
        d = ImageDraw.Draw(img)
        d.text((10, 20), 'Atorvastatin 20mg Batch: ATV-NEW-99', fill=(0, 0, 0))

        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)

        upload_file = SimpleUploadedFile('test.png', buf.getvalue(), content_type='image/png')
        self.client.post(reverse('medicine_ocr_scan_api'), {'image': upload_file})

        # Verify stock and batches remained unchanged
        self.med_atorvastatin.refresh_from_db()
        self.assertEqual(self.med_atorvastatin.current_stock, initial_stock)
        self.assertEqual(self.med_atorvastatin.batches.count(), initial_batches_count)

    # ── 6. Phase 4: Unknown Barcode & Manual Mapping Workflow ────────────────
    def test_unknown_barcode_lookup_returns_not_found(self):
        """Unrecognized code returns 404 NOT_FOUND status with unknown type."""
        self.client.force_login(self.pharmacist_user)
        res = self.client.get(reverse('medicine_barcode_lookup_api') + "?code=UNKNOWN-999888777")

        self.assertEqual(res.status_code, 404)
        data = res.json()
        self.assertFalse(data['success'])
        self.assertEqual(data['status_code'], 'NOT_FOUND')

    def test_map_unknown_barcode_to_medicine_without_existing_barcode(self):
        """Authorized pharmacist maps a manufacturer barcode to a medicine that has none."""
        self.client.force_login(self.pharmacist_user)
        new_code = '8909990001112'

        res = self.client.post(
            reverse('medicine_barcode_map_api'),
            {'medicine_id': self.med_empty_info.id, 'barcode': new_code},
            content_type='application/json'
        )

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])

        # Verify database record updated
        self.med_empty_info.refresh_from_db()
        self.assertEqual(self.med_empty_info.barcode, new_code)

        # Verify AuditLog created
        log_entry = AuditLog.objects.filter(action='MEDICINE_UPDATED', user=self.pharmacist_user).first()
        self.assertIsNotNone(log_entry)
        self.assertIn(new_code, log_entry.description)

        # Verify scanning the newly mapped barcode now resolves the medicine
        scan_res = self.client.get(reverse('medicine_barcode_lookup_api') + f"?code={new_code}")
        self.assertEqual(scan_res.status_code, 200)
        self.assertEqual(scan_res.json()['medicine']['id'], self.med_empty_info.id)

    def test_map_barcode_rejects_duplicates(self):
        """Cannot map a barcode that is already assigned to another medicine."""
        self.client.force_login(self.pharmacist_user)
        existing_code = self.med_atorvastatin.barcode

        # Try to map Atorvastatin's barcode to Permethrin
        res = self.client.post(
            reverse('medicine_barcode_map_api'),
            {'medicine_id': self.med_empty_info.id, 'barcode': existing_code},
            content_type='application/json'
        )

        self.assertEqual(res.status_code, 400)
        data = res.json()
        self.assertFalse(data['success'])
        self.assertIn('already mapped', data['message'])

    def test_map_barcode_rejects_overwriting_existing_barcode(self):
        """Cannot overwrite a medicine's existing manufacturer barcode."""
        self.client.force_login(self.pharmacist_user)

        res = self.client.post(
            reverse('medicine_barcode_map_api'),
            {'medicine_id': self.med_atorvastatin.id, 'barcode': '9999999999999'},
            content_type='application/json'
        )

        self.assertEqual(res.status_code, 400)
        data = res.json()
        self.assertFalse(data['success'])
        self.assertIn('Overwriting existing manufacturer barcodes is not permitted', data['message'])

    # ── 7. Security & Role Permissions ────────────────────────────────────────
    def test_doctor_role_blocked_from_barcode_and_ocr_endpoints(self):
        """Doctors are prohibited from pharmacy barcode lookup, mapping, and OCR scanning."""
        self.client.force_login(self.doctor_user)

        # Lookup API
        res_lookup = self.client.get(reverse('medicine_barcode_lookup_api') + "?code=SAN-MED-000001")
        self.assertEqual(res_lookup.status_code, 403)

        # Map API
        res_map = self.client.post(reverse('medicine_barcode_map_api'), {'medicine_id': 1, 'barcode': '123'})
        self.assertEqual(res_map.status_code, 403)

        # OCR API
        res_ocr = self.client.post(reverse('medicine_ocr_scan_api'))
        self.assertEqual(res_ocr.status_code, 403)

    def test_unauthenticated_blocked(self):
        """Unauthenticated requests are redirected or denied."""
        res = self.client.get(reverse('medicine_barcode_lookup_api') + "?code=SAN-MED-000001")
        self.assertIn(res.status_code, [302, 401, 403])

    def test_sql_and_script_injection_sanitization(self):
        """Malicious SQL/Script strings in barcode parameter are safely rejected."""
        self.client.force_login(self.pharmacist_user)

        malicious_inputs = [
            "'; DROP TABLE medicines_medicine; --",
            "<script>alert('XSS')</script>",
            "' OR '1'='1",
            "A" * 200,  # exceeds 128 chars limit
        ]

        for code_str in malicious_inputs:
            res = self.client.get(reverse('medicine_barcode_lookup_api') + f"?code={code_str}")
            # Should safely return 400 or 404, never 500 error
            self.assertIn(res.status_code, [400, 404])
            data = res.json()
            self.assertFalse(data['success'])

    # ── 8. Phase 4: Full End-to-End Workflow Integration ─────────────────────
    def test_end_to_end_registry_generate_lookup_profile_roundtrip(self):
        """
        Full roundtrip:
        1. Query Medicine Barcode Registry page
        2. Verify unique internal Code 128 barcode exists and is persisted
        3. Scan / lookup the internal barcode via API
        4. Validate returned profile, usable stock, FEFO batches, AI summary
        5. Verify no side-effects on inventory stock
        """
        self.client.force_login(self.pharmacist_user)

        # 1. Registry Page
        registry_res = self.client.get(reverse('medicine_barcode_registry'))
        self.assertEqual(registry_res.status_code, 200)
        self.assertContains(registry_res, 'Medicine Barcode Registry')
        self.assertContains(registry_res, self.med_atorvastatin.effective_internal_barcode)
        self.assertContains(registry_res, 'Scan Barcode')
        self.assertContains(registry_res, 'OCR Packaging')

        # 2. Preview API
        preview_res = self.client.get(reverse('medicine_barcode_preview', kwargs={'pk': self.med_atorvastatin.pk}))
        self.assertEqual(preview_res.status_code, 200)
        prev_data = preview_res.json()
        self.assertTrue(prev_data['success'])
        self.assertIn('<svg', prev_data['barcode_svg'])

        # 3. Lookup via scanner endpoint
        lookup_res = self.client.get(reverse('medicine_barcode_lookup_api') + f"?code={self.med_atorvastatin.effective_internal_barcode}")
        self.assertEqual(lookup_res.status_code, 200)
        lookup_data = lookup_res.json()

        # 4. Profile verification
        med = lookup_data['medicine']
        self.assertEqual(med['id'], self.med_atorvastatin.id)
        self.assertEqual(med['usable_stock'], 100)
        self.assertIsNotNone(med['ai_summary'])
        self.assertTrue(med['ai_summary']['is_ai_generated'])
        self.assertIn('Atorvastatin', med['ai_summary']['overview'])

        # 5. Inventory verification
        self.med_atorvastatin.refresh_from_db()
        self.assertEqual(self.med_atorvastatin.current_stock, 160)
