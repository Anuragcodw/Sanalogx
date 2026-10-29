from decimal import Decimal
from datetime import timedelta
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import User, PharmacistProfile, AuditLog
from medicines.models import Medicine, MedicineBatch, Category
from inventory.models import Inventory
from medicines import barcode_service


class MedicineBarcodeLookupTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        # 1. Admin user
        cls.admin_user = User.objects.create_user(
            username='admin_lookup',
            email='admin_lookup@hospital.com',
            password='Password@123',
            role=User.Role.ADMIN,
            status=User.Status.APPROVED,
            email_verified=True,
            is_staff=True,
        )

        # 2. Approved Pharmacist user
        cls.pharmacist_user = User.objects.create_user(
            username='pharm_lookup',
            email='pharm_lookup@hospital.com',
            password='Password@123',
            role=User.Role.PHARMACIST,
            status=User.Status.APPROVED,
            email_verified=True,
        )
        cls.pharmacist_profile = PharmacistProfile.objects.create(
            user=cls.pharmacist_user,
            registration_number='PH-LOOKUP-01',
            qualification='B.Pharm',
            is_demo=False,
            verification_status='VERIFIED',
        )

        # 3. Doctor user (unauthorized for pharmacy barcode lookup)
        cls.doctor_user = User.objects.create_user(
            username='doctor_lookup',
            email='doctor_lookup@hospital.com',
            password='Password@123',
            role=User.Role.DOCTOR,
            status=User.Status.APPROVED,
            email_verified=True,
        )

        # Category
        cls.category = Category.objects.create(
            name='Antibiotics & Antimicrobials',
            code='ANTIBIOTIC',
            description='Broad-spectrum anti-infective medications',
            notes='Verify patient renal function and hypersensitivity before dispensing.'
        )

        # Medicine 1: With complete clinical info and batches
        cls.med1 = Medicine.objects.create(
            medicine_name='Amoxicillin 500mg Capsules',
            generic_name='Amoxicillin',
            brand_name='Moxicip 500',
            category=cls.category,
            dosage_form='Capsule',
            strength='500mg',
            manufacturer='Cipla Healthcare Ltd',
            unit_of_measure='Strip of 10',
            barcode='8901112223334',  # Manufacturer barcode
            qr_code='QR-AMOX-500',
            description='Broad-spectrum beta-lactam antibiotic used for bacterial respiratory, skin, and urinary tract infections.',
            storage_condition='Store below 25°C in a dry place. Protect from moisture.',
            reorder_level=40,
            minimum_stock=20,
            maximum_stock=300,
            safety_stock=25,
            mrp=Decimal('85.00'),
            regulatory_category=Medicine.RegulatoryCategory.SCHEDULE_H,
            status=Medicine.Status.ACTIVE,
            verification_status=Medicine.VerificationStatus.VERIFIED,
        )

        # Inventory for med1
        cls.inv1 = Inventory.objects.create(
            medicine=cls.med1,
            current_stock=115,
            available_stock=70,
            minimum_stock=20,
            maximum_stock=300,
            reorder_level=40,
            safety_stock=25,
        )

        today = timezone.localdate()
        # Batch 1: Active, valid (50 units)
        cls.batch1 = MedicineBatch.objects.create(
            medicine=cls.med1,
            batch_number='AMX-2026-B1',
            quantity_received=100,
            quantity_available=50,
            expiry_date=today + timedelta(days=90),
            manufacturing_date=today - timedelta(days=60),
            purchase_price=Decimal('40.00'),
            selling_price=Decimal('75.00'),
            status=MedicineBatch.BatchStatus.ACTIVE,
            storage_condition='Store below 25°C',
        )

        # Batch 2: Near expiry, valid (20 units, FEFO priority #1)
        cls.batch2 = MedicineBatch.objects.create(
            medicine=cls.med1,
            batch_number='AMX-2026-B2',
            quantity_received=50,
            quantity_available=20,
            expiry_date=today + timedelta(days=15),
            manufacturing_date=today - timedelta(days=180),
            purchase_price=Decimal('38.00'),
            selling_price=Decimal('70.00'),
            status=MedicineBatch.BatchStatus.NEAR_EXPIRY,
            storage_condition='Store below 25°C',
        )

        # Batch 3: Expired (30 units)
        cls.batch3 = MedicineBatch.objects.create(
            medicine=cls.med1,
            batch_number='AMX-2025-EXP',
            quantity_received=50,
            quantity_available=30,
            expiry_date=today - timedelta(days=10),
            manufacturing_date=today - timedelta(days=365),
            purchase_price=Decimal('35.00'),
            selling_price=Decimal('65.00'),
            status=MedicineBatch.BatchStatus.EXPIRED,
            storage_condition='Store below 25°C',
        )

        # Batch 4: Quarantined (15 units)
        cls.batch4 = MedicineBatch.objects.create(
            medicine=cls.med1,
            batch_number='AMX-2026-QUAR',
            quantity_received=30,
            quantity_available=15,
            expiry_date=today + timedelta(days=120),
            manufacturing_date=today - timedelta(days=30),
            purchase_price=Decimal('42.00'),
            selling_price=Decimal('80.00'),
            status=MedicineBatch.BatchStatus.QUARANTINED,
            storage_condition='Quarantine rack',
        )

        # Medicine 2: Empty description, to test "Medicine-use information not available" fallback
        cls.category_empty = Category.objects.create(
            name='Dermatological Formulations',
            code='DERMA',
            description='',
            notes=''
        )
        cls.med2 = Medicine.objects.create(
            medicine_name='Clotrimazole 1% Cream',
            generic_name='Clotrimazole',
            brand_name='',
            category=cls.category_empty,
            dosage_form='Cream',
            strength='1% w/w',
            manufacturer='Sun Pharma Ltd',
            unit_of_measure='Tube',
            barcode=None,  # No manufacturer barcode assigned yet
            qr_code=None,
            description='',  # Empty description
            review_note='',  # Empty review note
            storage_condition='',
            reorder_level=50,
            minimum_stock=10,
            maximum_stock=200,
            safety_stock=15,
            mrp=Decimal('45.00'),
            regulatory_category=Medicine.RegulatoryCategory.NORMAL,
            status=Medicine.Status.ACTIVE,
            verification_status=Medicine.VerificationStatus.VERIFIED,
        )

    def setUp(self):
        self.client = Client()

    def test_phase1_barcode_resolves_correct_medicine(self):
        """Phase 1 barcode SAN-MED-000123 resolves the exact medicine record from MySQL."""
        self.med1.refresh_from_db()
        internal_code = self.med1.effective_internal_barcode
        self.assertTrue(internal_code.startswith('SAN-MED-'))

        self.client.force_login(self.pharmacist_user)
        # 1. Via Barcode Lookup API
        url = reverse('medicine_barcode_lookup_api') + f"?code={internal_code}"
        res = self.client.get(url)

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['type'], 'MEDICINE')
        self.assertEqual(data['barcode_type'], 'INTERNAL_SANALOGX')
        self.assertEqual(data['medicine']['id'], self.med1.id)
        self.assertEqual(data['medicine']['medicine_name'], 'Amoxicillin 500mg Capsules')
        self.assertEqual(data['medicine']['generic_name'], 'Amoxicillin')
        self.assertEqual(data['medicine']['strength'], '500mg')
        self.assertEqual(data['medicine']['dosage_form'], 'Capsule')
        self.assertEqual(data['medicine']['manufacturer'], 'Cipla Healthcare Ltd')
        self.assertEqual(data['medicine']['category'], 'Antibiotics & Antimicrobials')

    def test_stock_and_batch_expiry_details_match_database(self):
        """Stock calculations, usable stock, and segregated batches strictly match database state."""
        self.client.force_login(self.pharmacist_user)
        url = reverse('medicine_barcode_lookup_api') + f"?code={self.med1.effective_internal_barcode}"
        res = self.client.get(url)

        data = res.json()
        med = data['medicine']

        # Total Physical Stock: 50 + 20 + 30 + 15 = 115
        self.assertEqual(med['total_stock'], 115)
        # Usable Stock (strictly excluding expired & quarantined): 50 + 20 = 70
        self.assertEqual(med['usable_stock'], 70)
        # Expired Stock: 30
        self.assertEqual(med['expired_stock'], 30)
        # Quarantined Stock: 15
        self.assertEqual(med['quarantined_stock'], 15)
        # Unavailable Stock: 115 - 70 = 45
        self.assertEqual(med['unavailable_stock'], 45)

        # Batch FEFO breakdown
        batches = med['batches']
        self.assertEqual(len(batches), 4)

        # The earlier expiring batch (AMX-2026-B2, 15 days left) must be FEFO #1
        fefo_first = next(b for b in batches if b['is_fefo_first'])
        self.assertEqual(fefo_first['batch_number'], 'AMX-2026-B2')
        self.assertTrue(fefo_first['is_near_expiry'])
        self.assertFalse(fefo_first['is_expired'])

        # Expired batch
        exp_b = next(b for b in batches if b['batch_number'] == 'AMX-2025-EXP')
        self.assertTrue(exp_b['is_expired'])

        # Quarantined batch
        quar_b = next(b for b in batches if b['batch_number'] == 'AMX-2026-QUAR')
        self.assertTrue(quar_b['is_quarantined'])

    def test_verified_medicine_use_information_and_fallback(self):
        """Returns verified use info when present, and exact fallback string when unavailable."""
        self.client.force_login(self.pharmacist_user)

        # Medicine 1 has description
        res1 = self.client.get(reverse('medicine_barcode_lookup_api') + f"?code={self.med1.effective_internal_barcode}")
        data1 = res1.json()
        self.assertIn('Broad-spectrum beta-lactam antibiotic', data1['medicine']['verified_medicine_use'])

        # Medicine 2 has empty description
        self.med2.refresh_from_db()
        res2 = self.client.get(reverse('medicine_barcode_lookup_api') + f"?code={self.med2.effective_internal_barcode}")
        data2 = res2.json()
        self.assertEqual(data2['medicine']['verified_medicine_use'], 'Medicine-use information not available')

    def test_barcode_types_distinguished(self):
        """Distinguishes Internal Sanalogx, Manufacturer, Batch, and QR barcode types."""
        self.client.force_login(self.pharmacist_user)

        # 1. Internal Sanalogx code
        res_int = self.client.get(reverse('medicine_barcode_lookup_api') + f"?code={self.med1.effective_internal_barcode}")
        self.assertEqual(res_int.json()['barcode_type'], 'INTERNAL_SANALOGX')

        # 2. Manufacturer barcode
        res_mfr = self.client.get(reverse('medicine_barcode_lookup_api') + "?code=8901112223334")
        self.assertEqual(res_mfr.json()['barcode_type'], 'MANUFACTURER')

        # 3. Batch barcode
        res_batch = self.client.get(reverse('medicine_barcode_lookup_api') + "?code=AMX-2026-B1")
        self.assertEqual(res_batch.json()['barcode_type'], 'BATCH')
        self.assertEqual(res_batch.json()['type'], 'BATCH')
        self.assertEqual(res_batch.json()['batch']['batch_number'], 'AMX-2026-B1')

        # 4. QR Code
        res_qr = self.client.get(reverse('medicine_barcode_lookup_api') + "?code=QR-AMOX-500")
        self.assertEqual(res_qr.json()['barcode_type'], 'QR_CODE')

    def test_unknown_barcode_shows_safe_not_found(self):
        """Unknown code returns HTTP 404 with safe not-found JSON response."""
        self.client.force_login(self.pharmacist_user)
        res = self.client.get(reverse('medicine_barcode_lookup_api') + "?code=NONEXISTENT-CODE-999")

        self.assertEqual(res.status_code, 404)
        data = res.json()
        self.assertFalse(data['success'])
        self.assertEqual(data['status_code'], 'NOT_FOUND')
        self.assertIn('not found', data['message'].lower())

    def test_manual_barcode_mapping_api(self):
        """Allows authorized staff to map an unassigned manufacturer barcode, guarding against duplicates & overwrites."""
        self.client.force_login(self.pharmacist_user)

        # 1. Map new manufacturer barcode to med2 (which currently has barcode=None)
        post_data = {
            'medicine_id': self.med2.id,
            'barcode': '8907778889990'
        }
        res = self.client.post(
            reverse('medicine_barcode_map_api'),
            data=post_data,
            content_type='application/json'
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])

        self.med2.refresh_from_db()
        self.assertEqual(self.med2.barcode, '8907778889990')

        # 2. Duplicate prevention: attempt to map same barcode to med1
        res_dup = self.client.post(
            reverse('medicine_barcode_map_api'),
            data={'medicine_id': self.med1.id, 'barcode': '8907778889990'},
            content_type='application/json'
        )
        self.assertEqual(res_dup.status_code, 400)
        self.assertIn('already mapped', res_dup.json()['message'].lower())

        # 3. Overwrite prevention: attempt to overwrite med1's existing manufacturer barcode
        res_overwrite = self.client.post(
            reverse('medicine_barcode_map_api'),
            data={'medicine_id': self.med1.id, 'barcode': 'DIFFERENT-CODE-111'},
            content_type='application/json'
        )
        self.assertEqual(res_overwrite.status_code, 400)
        self.assertIn('not permitted', res_overwrite.json()['message'].lower())

    def test_scanning_does_not_modify_inventory_or_batches(self):
        """CRITICAL: Scanning operations are strictly read-only and never alter stock quantities."""
        self.client.force_login(self.pharmacist_user)
        initial_inv_stock = self.inv1.current_stock
        initial_b1_stock = self.batch1.quantity_available

        # Execute multiple lookup queries
        self.client.get(reverse('medicine_barcode_lookup_api') + f"?code={self.med1.effective_internal_barcode}")
        self.client.get(reverse('medicine_barcode_lookup_api') + "?code=8901112223334")
        self.client.get(reverse('medicine_barcode_lookup_api') + "?code=AMX-2026-B1")
        self.client.get(reverse('api_secure_barcode_scan') + f"?code={self.med1.effective_internal_barcode}")

        self.inv1.refresh_from_db()
        self.batch1.refresh_from_db()

        self.assertEqual(self.inv1.current_stock, initial_inv_stock)
        self.assertEqual(self.batch1.quantity_available, initial_b1_stock)

    def test_camera_scanner_ui_elements_in_secure_workspace(self):
        """Workspace renders live video preview, ZXing engine, and profile containers without file inputs."""
        self.client.force_login(self.pharmacist_user)
        res = self.client.get(reverse('secure_dispensing_workspace'))

        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Open Camera Scanner')
        self.assertContains(res, 'btnOpenCameraModal')
        self.assertContains(res, 'cameraScannerModal')
        self.assertContains(res, 'cameraVideo')  # Live video preview element
        self.assertContains(res, 'zxing.min.js')  # ZXing barcode engine
        self.assertContains(res, 'scannedMedicineProfileContainer')
        self.assertContains(res, 'unknownBarcodeContainer')
        self.assertContains(res, 'Manual Code Entry Fallback')

    def test_unauthenticated_and_unauthorized_access_blocked(self):
        """Guards against unauthenticated and unauthorized role access."""
        url = reverse('medicine_barcode_lookup_api') + f"?code={self.med1.effective_internal_barcode}"

        # 1. Unauthenticated -> 302 redirect to login
        res_anon = self.client.get(url)
        self.assertEqual(res_anon.status_code, 302)
        self.assertIn('/login/', res_anon.url)

        # 2. Doctor user -> 403 Forbidden
        self.client.force_login(self.doctor_user)
        res_doc = self.client.get(url)
        self.assertEqual(res_doc.status_code, 403)
