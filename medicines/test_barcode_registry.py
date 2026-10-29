from decimal import Decimal
from django.test import TestCase, Client
from django.urls import reverse
from django.core.management import call_command

from accounts.models import User, PharmacistProfile
from medicines.models import Medicine, Category
from inventory.models import Inventory
from medicines import barcode_service


class MedicineBarcodeRegistryTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        # 1. Admin user
        cls.admin_user = User.objects.create_user(
            username='admin_barcode',
            email='admin_barcode@hospital.com',
            password='Password@123',
            role=User.Role.ADMIN,
            status=User.Status.APPROVED,
            email_verified=True,
            is_staff=True,
        )

        # 2. Approved Pharmacist user
        cls.pharmacist_user = User.objects.create_user(
            username='pharm_barcode',
            email='pharm_barcode@hospital.com',
            password='Password@123',
            role=User.Role.PHARMACIST,
            status=User.Status.APPROVED,
            email_verified=True,
        )
        cls.pharmacist_profile = PharmacistProfile.objects.create(
            user=cls.pharmacist_user,
            registration_number='PH-BARCODE-01',
            qualification='B.Pharm',
            is_demo=False,
            verification_status='VERIFIED',
        )

        # 3. Pending Pharmacist user
        cls.pending_pharmacist = User.objects.create_user(
            username='pharm_barcode_pending',
            email='pending_barcode@hospital.com',
            password='Password@123',
            role=User.Role.PHARMACIST,
            status=User.Status.PENDING,
            email_verified=False,
        )
        cls.pending_profile = PharmacistProfile.objects.create(
            user=cls.pending_pharmacist,
            registration_number='PH-BARCODE-02',
            qualification='D.Pharm',
            is_demo=True,
            verification_status='DEMO_UNVERIFIED',
        )

        # 4. Doctor user (unauthorized for pharmacy barcode registry)
        cls.doctor_user = User.objects.create_user(
            username='doctor_barcode',
            email='doctor_barcode@hospital.com',
            password='Password@123',
            role=User.Role.DOCTOR,
            status=User.Status.APPROVED,
            email_verified=True,
        )

        # Category
        cls.category = Category.objects.create(
            name='Analgesics & Antipyretics',
            code='ANALGESIC',
            description='Pain relief and fever reduction medicines'
        )

        # Medicines
        cls.med1 = Medicine.objects.create(
            medicine_name='Paracetamol 500mg Tablets',
            generic_name='Paracetamol',
            category=cls.category,
            dosage_form='Tablet',
            strength='500mg',
            manufacturer='Sun Pharma Ltd',
            unit_of_measure='Strip',
            barcode='8901234567890',  # External / manufacturer barcode
            mrp=Decimal('25.00'),
            regulatory_category=Medicine.RegulatoryCategory.NORMAL,
            status=Medicine.Status.ACTIVE,
            verification_status=Medicine.VerificationStatus.VERIFIED,
        )

        cls.med2 = Medicine.objects.create(
            medicine_name='Amoxicillin 250mg Capsules',
            generic_name='Amoxicillin',
            category=cls.category,
            dosage_form='Capsule',
            strength='250mg',
            manufacturer='Cipla Ltd',
            unit_of_measure='Box',
            barcode='8909876543210',  # External / manufacturer barcode
            mrp=Decimal('60.00'),
            regulatory_category=Medicine.RegulatoryCategory.SCHEDULE_H,
            status=Medicine.Status.ACTIVE,
            verification_status=Medicine.VerificationStatus.VERIFIED,
        )

        cls.med3 = Medicine.objects.create(
            medicine_name='Ibuprofen 400mg Tablets',
            generic_name='Ibuprofen',
            category=cls.category,
            dosage_form='Tablet',
            strength='400mg',
            manufacturer='Abbott India',
            unit_of_measure='Strip',
            barcode=None,  # No external barcode
            mrp=Decimal('35.00'),
            regulatory_category=Medicine.RegulatoryCategory.NORMAL,
            status=Medicine.Status.ACTIVE,
            verification_status=Medicine.VerificationStatus.VERIFIED,
        )

        # Inventory setup for med1
        cls.inventory1 = Inventory.objects.create(
            medicine=cls.med1,
            current_stock=150,
            available_stock=140,
            minimum_stock=20,
            maximum_stock=500,
            reorder_level=50,
            safety_stock=30,
        )

    def setUp(self):
        self.client = Client()

    def test_existing_medicines_receive_unique_barcodes(self):
        """All medicines receive unique SAN-MED-000123 formatted internal barcodes."""
        self.med1.refresh_from_db()
        self.med2.refresh_from_db()
        self.med3.refresh_from_db()

        self.assertTrue(self.med1.internal_barcode.startswith('SAN-MED-'))
        self.assertTrue(self.med2.internal_barcode.startswith('SAN-MED-'))
        self.assertTrue(self.med3.internal_barcode.startswith('SAN-MED-'))

        self.assertEqual(self.med1.internal_barcode, f"SAN-MED-{self.med1.pk:06d}")
        self.assertEqual(self.med2.internal_barcode, f"SAN-MED-{self.med2.pk:06d}")
        self.assertEqual(self.med3.internal_barcode, f"SAN-MED-{self.med3.pk:06d}")

        # Unique assertion
        barcodes = [self.med1.internal_barcode, self.med2.internal_barcode, self.med3.internal_barcode]
        self.assertEqual(len(barcodes), len(set(barcodes)))

    def test_barcodes_stable_after_reload(self):
        """Internal barcode remains constant and stable across reloads and queries."""
        self.med1.refresh_from_db()
        code1 = self.med1.internal_barcode

        # Re-fetch fresh instance
        fresh_med = Medicine.objects.get(pk=self.med1.pk)
        self.assertEqual(fresh_med.internal_barcode, code1)

        # Save again without changing barcode
        fresh_med.save()
        fresh_med.refresh_from_db()
        self.assertEqual(fresh_med.internal_barcode, code1)

    def test_backfill_idempotency_no_duplicates(self):
        """Backfill command runs idempotently and creates no duplicate barcodes."""
        # Run backfill first time
        res1 = barcode_service.backfill_all_medicine_barcodes(force=False)
        self.assertEqual(res1['updated'], 0)
        self.assertGreaterEqual(res1['skipped'], 3)

        # Run management command
        call_command('backfill_medicine_barcodes')

        # Check all medicines have internal_barcode
        all_barcodes = list(Medicine.objects.values_list('internal_barcode', flat=True))
        self.assertEqual(len(all_barcodes), len(set(all_barcodes)))
        for code in all_barcodes:
            self.assertTrue(code.startswith('SAN-MED-'))

    def test_manufacturer_barcode_preserved(self):
        """Manufacturer external barcode is completely preserved and never overwritten."""
        self.med1.refresh_from_db()
        self.assertEqual(self.med1.barcode, '8901234567890')

        # Run backfill with force
        barcode_service.backfill_all_medicine_barcodes(force=True)

        self.med1.refresh_from_db()
        self.assertEqual(self.med1.barcode, '8901234567890')
        self.assertEqual(self.med1.internal_barcode, f"SAN-MED-{self.med1.pk:06d}")

    def test_png_download_valid_file(self):
        """GET /medicines/barcodes/{pk}/png/ returns a valid Code 128 PNG image."""
        self.client.force_login(self.pharmacist_user)
        url = reverse('medicine_barcode_png', kwargs={'pk': self.med1.pk})
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')
        self.assertIn(f'{self.med1.internal_barcode}.png', response['Content-Disposition'])
        # PNG magic number: \x89PNG\r\n\x1a\n
        self.assertTrue(response.content.startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertGreater(len(response.content), 1000)

    def test_pdf_download_valid_file(self):
        """GET /medicines/barcodes/{pk}/pdf/ returns a valid 3.5x2 inch printable label PDF."""
        self.client.force_login(self.pharmacist_user)
        url = reverse('medicine_barcode_pdf', kwargs={'pk': self.med1.pk})
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn(f'{self.med1.internal_barcode}_label.pdf', response['Content-Disposition'])
        # PDF magic number: %PDF-
        self.assertTrue(response.content.startswith(b'%PDF-'))
        self.assertGreater(len(response.content), 1000)

    def test_unauthorized_user_cannot_access(self):
        """Unauthenticated requests are redirected to the login page (302)."""
        urls = [
            reverse('medicine_barcode_registry'),
            reverse('medicine_barcode_png', kwargs={'pk': self.med1.pk}),
            reverse('medicine_barcode_pdf', kwargs={'pk': self.med1.pk}),
            reverse('medicine_barcode_print', kwargs={'pk': self.med1.pk}),
            reverse('medicine_barcode_preview', kwargs={'pk': self.med1.pk}),
        ]
        for u in urls:
            res = self.client.get(u)
            self.assertEqual(res.status_code, 302)
            self.assertIn('/login/', res.url)

    def test_doctor_cannot_access(self):
        """Doctor role without pharmacy access receives HTTP 403 Forbidden."""
        self.client.force_login(self.doctor_user)
        res = self.client.get(reverse('medicine_barcode_registry'))
        self.assertEqual(res.status_code, 403)

    def test_pending_pharmacist_redirected(self):
        """Pending unverified pharmacist is redirected to the pending status page."""
        self.client.force_login(self.pending_pharmacist)
        res = self.client.get(reverse('medicine_barcode_registry'))
        self.assertEqual(res.status_code, 302)
        self.assertIn('/pending/', res.url)

    def test_no_stock_modified_by_barcode_operations(self):
        """Barcode registry operations are strictly read-only regarding inventory stock."""
        self.client.force_login(self.pharmacist_user)
        initial_stock = self.inventory1.current_stock
        initial_avail = self.inventory1.available_stock

        # Execute registry view
        self.client.get(reverse('medicine_barcode_registry'))
        # Execute preview API
        self.client.get(reverse('medicine_barcode_preview', kwargs={'pk': self.med1.pk}))
        # Execute PNG download
        self.client.get(reverse('medicine_barcode_png', kwargs={'pk': self.med1.pk}))
        # Execute PDF download
        self.client.get(reverse('medicine_barcode_pdf', kwargs={'pk': self.med1.pk}))
        # Execute Print view
        self.client.get(reverse('medicine_barcode_print', kwargs={'pk': self.med1.pk}))

        # Refresh inventory from DB
        self.inventory1.refresh_from_db()
        self.assertEqual(self.inventory1.current_stock, initial_stock)
        self.assertEqual(self.inventory1.available_stock, initial_avail)

    def test_registry_page_renders(self):
        """Authenticated pharmacist receives HTTP 200 with complete registry UI elements."""
        self.client.force_login(self.pharmacist_user)
        response = self.client.get(reverse('medicine_barcode_registry'))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'medicines/barcode_registry.html')
        self.assertContains(response, 'Medicine Barcode Registry')
        self.assertContains(response, 'Paracetamol 500mg Tablets')
        self.assertContains(response, self.med1.internal_barcode)
        self.assertContains(response, '<svg')  # Live SVG barcode rendering

    def test_search_filters_work(self):
        """Search query correctly filters medicines by internal barcode and medicine name."""
        self.client.force_login(self.pharmacist_user)

        # Search by internal barcode
        res1 = self.client.get(reverse('medicine_barcode_registry') + f"?q={self.med1.internal_barcode}")
        self.assertEqual(res1.status_code, 200)
        self.assertContains(res1, self.med1.medicine_name)
        self.assertNotContains(res1, self.med2.medicine_name)

        # Search by medicine name
        res2 = self.client.get(reverse('medicine_barcode_registry') + "?q=Amoxicillin")
        self.assertEqual(res2.status_code, 200)
        self.assertContains(res2, self.med2.medicine_name)
        self.assertNotContains(res2, self.med1.medicine_name)

    def test_print_page_renders(self):
        """Print page renders standalone label formatted for thermal/standard printing."""
        self.client.force_login(self.pharmacist_user)
        response = self.client.get(reverse('medicine_barcode_print', kwargs={'pk': self.med1.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'medicines/barcode_print.html')
        self.assertContains(response, self.med1.medicine_name)
        self.assertContains(response, self.med1.internal_barcode)
        self.assertContains(response, '<svg')
        self.assertContains(response, 'window.print()')

    def test_preview_api_returns_json(self):
        """Preview API returns JSON structure needed by the client-side modal."""
        self.client.force_login(self.pharmacist_user)
        response = self.client.get(reverse('medicine_barcode_preview', kwargs={'pk': self.med1.pk}))

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['internal_barcode'], self.med1.internal_barcode)
        self.assertEqual(data['manufacturer_barcode'], '8901234567890')
        self.assertIn('<svg', data['barcode_svg'])
        self.assertEqual(data['png_url'], f"/medicines/barcodes/{self.med1.pk}/png/")
        self.assertEqual(data['pdf_url'], f"/medicines/barcodes/{self.med1.pk}/pdf/")
        self.assertEqual(data['print_url'], f"/medicines/barcodes/{self.med1.pk}/print/")

    def test_scanner_button_ui_elements(self):
        """Registry page contains the Scan Barcode button and all required scanner UI elements."""
        self.client.force_login(self.pharmacist_user)
        response = self.client.get(reverse('medicine_barcode_registry'))

        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")

        # Scan Barcode button must exist
        self.assertIn("Scan Barcode", html)

        # Button must wire to the correct JS function
        self.assertIn("openRegistryCameraScanner", html)

        # Camera scanner modal anchor must be present (injected by template include)
        self.assertIn("cameraScannerModal", html)

        # Result modal for showing lookup results must be present
        self.assertIn("registryScanResultModal", html)

        # ZXing barcode scanning library must be loaded
        self.assertIn("zxing.min.js", html)

        # Main scanner JS module must be loaded
        self.assertIn("medivault_scanner.js", html)

        # CSRF token must be embedded for the JS lookup call
        self.assertIn("CSRF_TOKEN", html)

