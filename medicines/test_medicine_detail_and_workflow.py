from decimal import Decimal
from datetime import timedelta
from django.test import TestCase, Client
from django.utils import timezone
from django.urls import reverse

from accounts.models import User, PharmacistProfile, AuditLog
from medicines.models import Medicine, MedicineBatch, Category
from suppliers.models import Supplier, SupplierMedicine
from purchases.models import PurchaseOrder, PurchaseOrderItem, GoodsReceipt, GoodsReceiptItem


class MedicineDetailAndWorkflowTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Admin user
        cls.admin_user = User.objects.create_user(
            username='admin_test',
            email='admin_test@hospital.com',
            password='Password@123',
            role=User.Role.ADMIN,
            status=User.Status.APPROVED,
            email_verified=True,
            is_staff=True,
        )

        # Approved Pharmacist
        cls.pharmacist_user = User.objects.create_user(
            username='pharm_test',
            email='pharm_test@hospital.com',
            password='Password@123',
            role=User.Role.PHARMACIST,
            status=User.Status.APPROVED,
            email_verified=True,
        )
        cls.pharmacist_profile = PharmacistProfile.objects.create(
            user=cls.pharmacist_user,
            registration_number='PH-9876',
            qualification='B.Pharm',
            is_demo=False,
            verification_status='VERIFIED',
        )

        # Pending Pharmacist (Unverified)
        cls.pending_pharmacist = User.objects.create_user(
            username='pharm_pending',
            email='pending@hospital.com',
            password='Password@123',
            role=User.Role.PHARMACIST,
            status=User.Status.PENDING,
            email_verified=False,
        )
        cls.pending_profile = PharmacistProfile.objects.create(
            user=cls.pending_pharmacist,
            registration_number='PH-1234',
            qualification='D.Pharm',
            is_demo=True,
            verification_status='DEMO_UNVERIFIED',
        )

        # Rejected Pharmacist
        cls.rejected_pharmacist = User.objects.create_user(
            username='pharm_rejected',
            email='rejected@hospital.com',
            password='Password@123',
            role=User.Role.PHARMACIST,
            status=User.Status.REJECTED,
            email_verified=True,
            rejection_reason='Expired council registration certificate.',
        )
        cls.rejected_profile = PharmacistProfile.objects.create(
            user=cls.rejected_pharmacist,
            registration_number='PH-0000',
            qualification='B.Pharm',
            is_demo=False,
            verification_status='VERIFICATION_FAILED',
        )

        # Category
        cls.category = Category.objects.create(
            name='Cardiovascular Agents',
            code='CARDIOVASCULAR',
            description='Heart and blood vessel medicines'
        )

        # Supplier
        cls.supplier = Supplier.objects.create(
            company_name='Apex Healthcare Pharma Ltd',
            registration_number='DL-MH-2024-9988',
            drug_license_number='DL-9988-A',
            gst_number='27AABCU9603R1ZM',
            email='orders@apexpharma.com',
            phone='+91 98200 11223',
            status=Supplier.SupplierStatus.ACTIVE,
        )

        # 1. Medicine with batches WITHOUT suppliers (reproducing Digoxin #21)
        cls.med_without_supplier = Medicine.objects.create(
            medicine_name='Digoxin Cardiotonic 0.25mg',
            generic_name='Digoxin',
            category=cls.category,
            dosage_form='Tablet',
            strength='0.25mg',
            manufacturer='GlaxoSmithKline',
            unit_of_measure='Strip',
            regulatory_category=Medicine.RegulatoryCategory.SCHEDULE_H,
            status=Medicine.Status.ACTIVE,
            verification_status=Medicine.VerificationStatus.VERIFIED,
            reorder_level=50,
            safety_stock=20,
            mrp=Decimal('45.00'),
        )
        cls.batch_no_sup_1 = MedicineBatch.objects.create(
            medicine=cls.med_without_supplier,
            batch_number='DGX-24K21',
            manufacturing_date=timezone.now().date() - timedelta(days=60),
            expiry_date=timezone.now().date() + timedelta(days=300),
            purchase_price=Decimal('25.00'),
            selling_price=Decimal('40.00'),
            quantity_received=100,
            quantity_available=80,
            storage_condition='Store below 25°C',
            status=MedicineBatch.BatchStatus.ACTIVE,
        )
        cls.batch_no_sup_2 = MedicineBatch.objects.create(
            medicine=cls.med_without_supplier,
            batch_number='DGX-25A21',
            manufacturing_date=timezone.now().date() - timedelta(days=30),
            expiry_date=timezone.now().date() + timedelta(days=450),
            purchase_price=Decimal('26.00'),
            selling_price=Decimal('42.00'),
            quantity_received=150,
            quantity_available=150,
            storage_condition='Store below 25°C',
            status=MedicineBatch.BatchStatus.ACTIVE,
        )

        # 2. Medicine WITH supplier catalogue and GRN batch
        cls.med_with_supplier = Medicine.objects.create(
            medicine_name='Atorvastatin Calcium 10mg',
            generic_name='Atorvastatin',
            category=cls.category,
            dosage_form='Tablet',
            strength='10mg',
            manufacturer='Pfizer',
            unit_of_measure='Strip',
            regulatory_category=Medicine.RegulatoryCategory.SCHEDULE_H,
            status=Medicine.Status.ACTIVE,
            verification_status=Medicine.VerificationStatus.VERIFIED,
            reorder_level=30,
            safety_stock=10,
            mrp=Decimal('90.00'),
        )
        cls.sm = SupplierMedicine.objects.create(
            supplier=cls.supplier,
            medicine=cls.med_with_supplier,
            unit_price=Decimal('50.00'),
            delivery_days=3,
            is_preferred=True,
        )
        cls.batch_with_sup = MedicineBatch.objects.create(
            medicine=cls.med_with_supplier,
            batch_number='ATV-2026-X1',
            manufacturing_date=timezone.now().date() - timedelta(days=90),
            expiry_date=timezone.now().date() + timedelta(days=600),
            purchase_price=Decimal('50.00'),
            selling_price=Decimal('85.00'),
            quantity_received=200,
            quantity_available=180,
            status=MedicineBatch.BatchStatus.ACTIVE,
        )

        # 3. Medicine WITHOUT batches (Empty batch state)
        cls.med_no_batches = Medicine.objects.create(
            medicine_name='Amiodarone HCl 200mg',
            generic_name='Amiodarone',
            category=cls.category,
            dosage_form='Tablet',
            strength='200mg',
            manufacturer='Sanofi',
            unit_of_measure='Strip',
            regulatory_category=Medicine.RegulatoryCategory.SCHEDULE_H,
            status=Medicine.Status.ACTIVE,
            verification_status=Medicine.VerificationStatus.VERIFIED,
            reorder_level=20,
            safety_stock=5,
            mrp=Decimal('120.00'),
        )

        # 4. Draft medicine awaiting review
        cls.med_draft = Medicine.objects.create(
            medicine_name='Metoprolol Tartrate 50mg (Imported Draft)',
            generic_name='Metoprolol',
            category=cls.category,
            dosage_form='Tablet',
            strength='50mg',
            manufacturer='AstraZeneca',
            unit_of_measure='Strip',
            regulatory_category=Medicine.RegulatoryCategory.SCHEDULE_H,
            status=Medicine.Status.PENDING_REVIEW,
            verification_status=Medicine.VerificationStatus.PENDING_REVIEW,
            reorder_level=40,
            safety_stock=15,
            mrp=Decimal('65.00'),
        )

    def setUp(self):
        self.client = Client()

    def test_medicine_detail_batch_without_supplier_no_crash(self):
        """
        Verify that accessing a medicine with batches lacking suppliers (e.g. Digoxin /medicines/21/)
        returns HTTP 200 and renders 'Supplier not recorded' instead of raising VariableDoesNotExist.
        """
        self.client.force_login(self.admin_user)
        response = self.client.get(f'/medicines/{self.med_without_supplier.id}/')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('Digoxin Cardiotonic 0.25mg', content)
        self.assertIn('DGX-24K21', content)
        self.assertIn('DGX-25A21', content)
        self.assertIn('Supplier not recorded', content)

    def test_medicine_detail_batch_with_supplier_renders_supplier_info(self):
        """
        Verify that a batch associated with a supplier displays the supplier's company name.
        """
        self.client.force_login(self.admin_user)
        response = self.client.get(f'/medicines/{self.med_with_supplier.id}/')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('Atorvastatin Calcium 10mg', content)
        self.assertIn('Apex Healthcare Pharma Ltd', content)

    def test_medicine_detail_medicine_without_batches_renders_empty_state(self):
        """
        Verify that a medicine with 0 batches renders cleanly with the dedicated empty state.
        """
        self.client.force_login(self.admin_user)
        response = self.client.get(f'/medicines/{self.med_no_batches.id}/')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('Amiodarone HCl 200mg', content)
        self.assertIn('No Inventory Batches Allocated', content)

    def test_medicine_detail_with_med_prefixed_id(self):
        """
        Verify that requesting /medicines/MED-000X/ resolves identically to integer PK.
        """
        self.client.force_login(self.pharmacist_user)
        med_id = self.med_without_supplier.medicine_id
        response = self.client.get(f'/medicines/{med_id}/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Digoxin Cardiotonic 0.25mg', response.content.decode('utf-8'))

    def test_medicine_detail_invalid_id_returns_404(self):
        """
        Verify that non-existent medicine IDs return 404 cleanly.
        """
        self.client.force_login(self.admin_user)
        response = self.client.get('/medicines/999999/')
        self.assertEqual(response.status_code, 404)
        response_prefixed = self.client.get('/medicines/MED-999999/')
        self.assertEqual(response_prefixed.status_code, 404)

    def test_medicine_detail_anonymous_redirected(self):
        """
        Verify unauthenticated users cannot view medicine details.
        """
        response = self.client.get(f'/medicines/{self.med_without_supplier.id}/')
        self.assertEqual(response.status_code, 302)

    def test_medicine_list_view_status_scopes(self):
        """
        Verify that medicine_list_view handles status=active, pending, inactive, all.
        """
        self.client.force_login(self.admin_user)

        # Active
        r_active = self.client.get('/medicines/?status=active')
        self.assertEqual(r_active.status_code, 200)
        self.assertIn('Digoxin Cardiotonic 0.25mg', r_active.content.decode('utf-8'))

        # Pending
        r_pending = self.client.get('/medicines/?status=pending')
        self.assertEqual(r_pending.status_code, 200)
        self.assertIn('Metoprolol Tartrate 50mg (Imported Draft)', r_pending.content.decode('utf-8'))

        # All
        r_all = self.client.get('/medicines/?status=all')
        self.assertEqual(r_all.status_code, 200)
        content_all = r_all.content.decode('utf-8')
        self.assertIn('Digoxin Cardiotonic 0.25mg', content_all)
        self.assertIn('Metoprolol Tartrate 50mg (Imported Draft)', content_all)

    def test_medicine_list_view_search_filter(self):
        """
        Verify medicine search by name and generic formula.
        """
        self.client.force_login(self.admin_user)
        response = self.client.get('/medicines/?status=all&search=Digoxin')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('Digoxin Cardiotonic 0.25mg', content)
        self.assertNotIn('Atorvastatin Calcium 10mg', content)

    def test_draft_review_approve_workflow(self):
        """
        Verify that an Admin can approve a PENDING_REVIEW draft into the active catalogue.
        """
        self.client.force_login(self.admin_user)
        post_data = {
            'action': 'approve',
            'medicine_name': self.med_draft.medicine_name,
            'generic_name': self.med_draft.generic_name,
            'brand_name': self.med_draft.brand_name,
            'category': self.category.id,
            'dosage_form': self.med_draft.dosage_form,
            'strength': self.med_draft.strength,
            'manufacturer': self.med_draft.manufacturer,
            'unit_of_measure': self.med_draft.unit_of_measure,
            'regulatory_category': self.med_draft.regulatory_category,
            'reorder_level': 40,
            'minimum_stock': 10,
            'maximum_stock': 500,
            'safety_stock': 15,
            'tax_rate': Decimal('5.00'),
            'mrp': Decimal('65.00'),
            'max_discount_pct': Decimal('10.00'),
        }
        response = self.client.post(f'/medicines/drafts/{self.med_draft.id}/review/', post_data)
        self.assertEqual(response.status_code, 302)

        self.med_draft.refresh_from_db()
        self.assertEqual(self.med_draft.status, Medicine.Status.ACTIVE)
        self.assertEqual(self.med_draft.verification_status, Medicine.VerificationStatus.VERIFIED)

        # Check Audit Log
        audit = AuditLog.objects.filter(action='MEDICINE_APPROVED').first()
        self.assertIsNotNone(audit)
        self.assertIn('Metoprolol', audit.description)

    def test_draft_review_reject_workflow(self):
        """
        Verify that an Admin can reject a PENDING_REVIEW draft medicine with a note.
        """
        self.client.force_login(self.admin_user)
        post_data = {
            'action': 'reject',
            'rejection_note': 'Safety documentation incomplete for institutional formulary.',
        }
        response = self.client.post(f'/medicines/drafts/{self.med_draft.id}/review/', post_data)
        self.assertEqual(response.status_code, 302)

        self.med_draft.refresh_from_db()
        self.assertEqual(self.med_draft.status, Medicine.Status.INACTIVE)
        self.assertEqual(self.med_draft.verification_status, Medicine.VerificationStatus.REJECTED)
        self.assertIn('Safety documentation incomplete', self.med_draft.review_note)

        # Check Audit Log
        audit = AuditLog.objects.filter(action='MEDICINE_REJECTED').first()
        self.assertIsNotNone(audit)
        self.assertIn('Safety documentation incomplete', audit.description)

    def test_pharmacist_approval_screen_replaces_demo_badges(self):
        """
        Verify that /auth/admin/approvals/ renders meaningful verification status labels
        and does not show raw misleading 'DEMO' text.
        """
        self.client.force_login(self.admin_user)
        response = self.client.get('/auth/admin/approvals/')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')

        # Check for meaningful replacement labels
        self.assertTrue(
            'License verification pending' in content or
            'Verification required' in content or
            'Verified' in content or
            'Verification failed' in content or
            'Manual verification required' in content
        )
        # Verify that raw misleading '<i class="fas fa-flask"></i> DEMO / UNVERIFIED' is gone
        self.assertNotIn('<i class="fas fa-flask"></i> DEMO / UNVERIFIED', content)
        self.assertNotIn('<i class="fas fa-flask"></i> DEMO</span>', content)
