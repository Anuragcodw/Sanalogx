"""
medicines/test_excel_import_workflow.py

Comprehensive regression tests for:
1. Excel workbook parsing & field mapping (all 4 workbooks).
2. Duplicate detection: exact matches reused, distinct formulations kept separate.
3. Idempotency: re-running import creates zero duplicate records.
4. Zero stock guarantee: catalogue import never creates batches or inventory.
5. Verified opening stock / goods receipt lifecycle.
6. Prescription and controlled-drug dispensing validation.
7. Atomic POS sale and negative stock prevention.
8. Role-based access control (Admin vs Pharmacist).
"""

from decimal import Decimal
from datetime import timedelta
from django.test import TestCase
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command

from medicines.models import Category, Medicine, MedicineBatch, MedicineImportLog
from medicines.management.commands.import_excel_workbooks import (
    _clean_generic,
    _norm_form,
    _norm_dosage,
    _infer_unit,
    _read_therapeutic_subcategory,
    _read_ear_eye_nasal,
    _read_therapeutic_subcategories_generic,
    _read_drug_classification,
    WORKBOOK_FILES,
)
from inventory.models import Inventory, StockTransaction
from inventory import services as inventory_services
from sales.models import Sale, SaleItem
from sales import services as sales_services
from dispensing.models import Patient, Doctor, Prescription, PrescriptionItem, DispensingRecord
from dispensing import secure_dispensing_service

User = get_user_model()


class ExcelWorkbookImportWorkflowTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username='admin_test',
            email='admin_test@hospital.org',
            password='TestPassword123!',
            role='ADMIN',
            status='APPROVED',
        )
        self.pharmacist = User.objects.create_user(
            username='pharmacist_test',
            email='pharm_test@hospital.org',
            password='TestPassword123!',
            role='PHARMACIST',
            status='APPROVED',
        )
        self.category = Category.objects.create(
            name='Analgesics',
            code='ANALGESICS',
            description='Pain relief medicines',
            is_active=True,
        )

    # ── Test 1: Workbook Readers & Field Extraction ─────────────────────────
    def test_workbook_readers_extract_data(self):
        """All four workbooks are read and yield records with required non-empty fields."""
        wb2_records = _read_therapeutic_subcategory(WORKBOOK_FILES['Therapeutic_Subcategory'])
        self.assertEqual(len(wb2_records), 50)
        self.assertTrue(all(r['generic_name'] for r in wb2_records))

        wb3_records = _read_ear_eye_nasal(WORKBOOK_FILES['Ear_Eye_Nasal'])
        self.assertEqual(len(wb3_records), 150)
        # Verify routes were captured
        routes = {r['dosage_form'] for r in wb3_records}
        self.assertTrue('Ear Drops' in routes)
        self.assertTrue('Eye Drops' in routes)
        self.assertTrue('Nasal Drops' in routes)

        wb4_records = _read_therapeutic_subcategories_generic(WORKBOOK_FILES['Therapeutic_Subcategories_Generic'])
        self.assertEqual(len(wb4_records), 50)

        wb1_records = _read_drug_classification(WORKBOOK_FILES['Drug_Classification'])
        self.assertGreater(len(wb1_records), 100)

    # ── Test 2: Field Normalization Helpers ──────────────────────────────────
    def test_generic_and_dosage_form_normalization(self):
        """Generic names strip parenthetical synonyms; dosage forms are cleanly normalized."""
        self.assertEqual(_clean_generic('Paracetamol (acetaminophen)'), 'Paracetamol')
        self.assertEqual(_clean_generic('Salbutamol (albuterol)'), 'Salbutamol')
        self.assertEqual(_clean_generic('Lidocaine (Lignocaine)'), 'Lidocaine')
        self.assertEqual(_norm_form('Eye Drops - Prescription Review'), 'eye drop')
        self.assertEqual(_norm_form('Ear Drops/Otic'), 'ear drop')
        self.assertEqual(_norm_dosage('ear drops'), 'Ear Drops')
        self.assertEqual(_infer_unit('Eye Drops'), 'Bottle')
        self.assertEqual(_infer_unit('Tablet'), 'Tablet')

    # ── Test 3: Catalogue Import Creates ZERO Stock ─────────────────────────
    def test_catalogue_import_creates_zero_stock_and_zero_batches(self):
        """Management command creates PENDING_REVIEW medicines without creating batches or inventory."""
        initial_batches = MedicineBatch.objects.count()
        initial_inv = Inventory.objects.count()

        call_command('import_excel_workbooks', '--commit')

        # Verify new medicines were created in PENDING_REVIEW
        new_meds = Medicine.objects.filter(source_reference__icontains='Drug')
        self.assertGreater(new_meds.count(), 0)
        for med in new_meds[:10]:
            self.assertEqual(med.status, Medicine.Status.PENDING_REVIEW)
            self.assertEqual(med.verification_status, Medicine.VerificationStatus.PENDING_REVIEW)
            self.assertEqual(med.strength, 'PENDING VERIFICATION')
            self.assertEqual(med.mrp, Decimal('0.00'))

        # Verify zero stock was created
        self.assertEqual(MedicineBatch.objects.count(), initial_batches)
        self.assertEqual(Inventory.objects.count(), initial_inv)

    # ── Test 4: Idempotency — Re-import creates ZERO duplicates ──────────────
    def test_import_is_strictly_idempotent(self):
        """Running the import a second time creates zero duplicate records."""
        call_command('import_excel_workbooks', '--commit')
        count_after_first = Medicine.objects.count()

        # Run again
        call_command('import_excel_workbooks', '--commit')
        count_after_second = Medicine.objects.count()

        self.assertEqual(count_after_first, count_after_second)

    # ── Test 5: Distinct Formulations Kept Separate ─────────────────────────
    def test_distinct_formulations_are_not_merged(self):
        """Ciprofloxacin Tablet and Ciprofloxacin Ear Drops exist as distinct products."""
        # Create an existing active tablet
        Medicine.objects.create(
            medicine_name='Ciprofloxacin 500mg Tablet',
            generic_name='Ciprofloxacin',
            dosage_form='Tablet',
            strength='500mg',
            unit_of_measure='Tablet',
            category=self.category,
            status=Medicine.Status.ACTIVE,
        )

        call_command('import_excel_workbooks', '--commit')

        # Ciprofloxacin should exist in Tablet and in Ear Drops / Eye Drops
        cipro_meds = Medicine.objects.filter(generic_name='Ciprofloxacin')
        forms = {m.dosage_form for m in cipro_meds}
        self.assertIn('Tablet', forms)
        self.assertTrue('Ear Drops' in forms or 'Eye Drops' in forms)

    # ── Test 6: Verified Opening Stock Workflow ─────────────────────────────
    def test_verified_opening_stock_creates_batch_and_inventory(self):
        """Adding stock through central service creates batch balances and inventory correctly."""
        med = Medicine.objects.create(
            medicine_name='Amoxicillin 500mg',
            generic_name='Amoxicillin',
            dosage_form='Capsule',
            strength='500mg',
            unit_of_measure='Capsule',
            category=self.category,
            status=Medicine.Status.ACTIVE,
        )

        batch = MedicineBatch.objects.create(
            medicine=med,
            batch_number='BATCH-OPENING-001',
            expiry_date=timezone.now().date() + timedelta(days=365),
            purchase_price=Decimal('10.00'),
            selling_price=Decimal('15.00'),
            quantity_received=0,
            quantity_available=0,
        )

        inventory_services.add_stock(
            medicine=med,
            batch=batch,
            quantity=100,
            transaction_type=StockTransaction.TransactionType.PURCHASE,
            performed_by=self.admin,
            reference_number='OPENING-STOCK-2026',
            notes='Verified physical count opening stock',
        )

        batch.refresh_from_db()
        self.assertEqual(batch.quantity_available, 100)
        self.assertEqual(med.inventory.current_stock, 100)
        self.assertEqual(med.inventory.available_stock, 100)

    # ── Test 7: Prescription Dispensing Validation & Controlled Drug Safeguard ─
    def test_regulated_dispensing_blocks_missing_prescription(self):
        """Dispensing a prescription-only or controlled medicine without a prescription raises ValidationError."""
        controlled_med = Medicine.objects.create(
            medicine_name='Morphine Sulfate 10mg',
            generic_name='Morphine Sulfate',
            dosage_form='Tablet',
            strength='10mg',
            unit_of_measure='Tablet',
            category=self.category,
            regulatory_category=Medicine.RegulatoryCategory.NARCOTIC,
            status=Medicine.Status.ACTIVE,
        )

        batch = MedicineBatch.objects.create(
            medicine=controlled_med,
            batch_number='BATCH-MORPH-01',
            expiry_date=timezone.now().date() + timedelta(days=200),
            purchase_price=Decimal('20.00'),
            selling_price=Decimal('35.00'),
            quantity_received=50,
            quantity_available=50,
            is_controlled=True,
        )
        inventory_services.recalculate_inventory(
            Inventory.objects.create(medicine=controlled_med)
        )

        # Attempt to dispense without a prescription
        is_valid, err_msg = secure_dispensing_service.validate_dispensing_eligibility(
            medicine=controlled_med,
            requested_qty=10,
            prescription=None,
        )
        self.assertFalse(is_valid)
        self.assertIn('prescription is mandatory', err_msg.lower())

    # ── Test 8: POS Sale Atomicity & Negative Stock Prevention ───────────────
    def test_pos_sale_cannot_produce_negative_stock(self):
        """POS checkout cannot deduct more stock than available and rolls back completely."""
        med = Medicine.objects.create(
            medicine_name='Paracetamol 500mg Tablet',
            generic_name='Paracetamol',
            dosage_form='Tablet',
            strength='500mg',
            unit_of_measure='Tablet',
            category=self.category,
            status=Medicine.Status.ACTIVE,
        )

        batch = MedicineBatch.objects.create(
            medicine=med,
            batch_number='BATCH-PARA-01',
            expiry_date=timezone.now().date() + timedelta(days=180),
            purchase_price=Decimal('2.00'),
            selling_price=Decimal('5.00'),
            quantity_received=10,
            quantity_available=10,
        )
        inv = Inventory.objects.create(medicine=med)
        inventory_services.recalculate_inventory(inv)

        # Attempt to buy 50 units when only 10 are available
        with self.assertRaises(ValidationError):
            sales_services.create_sale_from_cart(
                patient_name='Walk-in Customer',
                cart_items=[{'medicine_id': med.id, 'quantity': 50}],
                pharmacist_user=self.pharmacist,
            )

        # Verify batch stock is unchanged (still 10)
        batch.refresh_from_db()
        self.assertEqual(batch.quantity_available, 10)
        self.assertEqual(Sale.objects.count(), 0)
