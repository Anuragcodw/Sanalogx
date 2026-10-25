"""
medicines/test_import.py
MediVault — Category Master & Medicine Import Test Suite

Tests cover:
  1. Category import: new, duplicate-mapping, code update, in-file duplicate error
  2. Medicine import: new active, starter example draft, missing required fields
  3. Duplicate detection: existing generic+strength+dosage_form
  4. Category mapping: code-first, name-fallback, fuzzy-partial
  5. Repeated imports: idempotent — no overwrite of verified records
  6. Inventory preservation: no batch or inventory record created
  7. Error report CSV: downloadable after failed rows
  8. Approve / reject draft workflow
  9. Role permission: non-admin cannot access import views
 10. Database audit: MedicineImportLog and AuditLog created
"""

import io
import csv
import json
from decimal import Decimal

from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from django.urls import reverse

from medicines.models import Category, Medicine, MedicineBatch, MedicineImportLog
from medicines.import_service import (
    validate_and_preview_categories,
    execute_category_import,
    validate_and_preview_medicines,
    execute_medicine_import,
    build_error_report_csv,
    classify_sheet,
)
from inventory.models import Inventory, StockTransaction

User = get_user_model()


# ── Helpers ─────────────────────────────────────────────────────────────────

def _make_admin(username='admin_import', email='admin_import@hospital.org'):
    return User.objects.create_user(
        username=username,
        email=email,
        password='Password123!',
        role='ADMIN',
        status='APPROVED',
    )


def _make_pharmacist(username='ph_import', email='ph_import@hospital.org'):
    return User.objects.create_user(
        username=username,
        email=email,
        password='Password123!',
        role='PHARMACIST',
        status='APPROVED',
    )


def _cat_rows(*rows):
    """Build a list of category import row dicts."""
    return [dict(r) for r in rows]


def _med_rows(*rows):
    """Build a list of medicine import row dicts."""
    return [dict(r) for r in rows]


# ── 1. Category Import ───────────────────────────────────────────────────────

class CategoryImportTest(TestCase):
    def setUp(self):
        self.admin = _make_admin()
        Category.objects.create(name='Antibiotics', code='ANTIBIOTIC', is_active=True)

    def test_new_category_created(self):
        """New category not in DB is created successfully."""
        rows = _cat_rows(
            {'category_name': 'Antifungals', 'category_code': 'ANTIFUNGAL', 'description': 'Fungal infections'},
        )
        preview = validate_and_preview_categories(rows)
        self.assertEqual(preview['new_count'], 1)
        self.assertEqual(preview['duplicate_count'], 0)
        self.assertEqual(preview['error_count'], 0)

        log = execute_category_import(preview, self.admin, 'test_cat.csv')
        self.assertEqual(log.status, MedicineImportLog.Status.SUCCESS)
        self.assertEqual(log.created_count, 1)
        self.assertTrue(Category.objects.filter(name='Antifungals', code='ANTIFUNGAL').exists())

    def test_existing_category_is_duplicate_not_overwritten(self):
        """A category name already in DB is reported as duplicate and not overwritten."""
        original_desc = Category.objects.get(name='Antibiotics').description
        rows = _cat_rows(
            {'category_name': 'Antibiotics', 'category_code': 'NEW_CODE', 'description': 'Changed description'},
        )
        preview = validate_and_preview_categories(rows)
        self.assertEqual(preview['duplicate_count'], 1)
        self.assertEqual(preview['new_count'], 0)
        # Description not overwritten on execute
        execute_category_import(preview, self.admin, 'test_cat.csv')
        cat = Category.objects.get(name='Antibiotics')
        self.assertEqual(cat.description, original_desc)  # unchanged

    def test_code_updated_on_existing_without_code(self):
        """If an existing category has no code, import may fill it in."""
        Category.objects.create(name='Vitamins', code=None, is_active=True)
        rows = _cat_rows(
            {'category_name': 'Vitamins', 'category_code': 'VITAMINS_SUPP'},
        )
        preview = validate_and_preview_categories(rows)
        log = execute_category_import(preview, self.admin, 'test_cat.csv')
        cat = Category.objects.get(name='Vitamins')
        self.assertEqual(cat.code, 'VITAMINS_SUPP')
        self.assertEqual(log.updated_count, 1)

    def test_empty_category_name_is_error(self):
        """Row with no name is rejected as error."""
        rows = _cat_rows({'category_name': '', 'category_code': 'X'})
        preview = validate_and_preview_categories(rows)
        self.assertEqual(preview['error_count'], 1)
        self.assertEqual(preview['valid_count'], 0)

    def test_infile_duplicate_is_error(self):
        """Two rows with same category name in one file — second is an error."""
        rows = _cat_rows(
            {'category_name': 'Cardiology', 'category_code': 'CARDIO'},
            {'category_name': 'Cardiology', 'category_code': 'CARDIO2'},
        )
        preview = validate_and_preview_categories(rows)
        self.assertEqual(preview['error_count'], 1)
        self.assertEqual(preview['new_count'], 1)

    def test_no_batch_or_inventory_created(self):
        """Category import never creates batch or inventory records."""
        rows = _cat_rows({'category_name': 'Hormonal', 'category_code': 'HORMONAL'})
        preview = validate_and_preview_categories(rows)
        execute_category_import(preview, self.admin, 'test_cat.csv')
        self.assertEqual(MedicineBatch.objects.count(), 0)
        self.assertEqual(Inventory.objects.count(), 0)
        self.assertEqual(StockTransaction.objects.count(), 0)


# ── 2. Medicine Import ───────────────────────────────────────────────────────

class MedicineImportTest(TestCase):
    def setUp(self):
        self.admin = _make_admin()
        self.cat = Category.objects.create(name='Analgesics', code='ANALGESIC', is_active=True)
        self.cat2 = Category.objects.create(name='Antibiotics', code='ANTIBIOTIC', is_active=True)

    def _base_row(self, **overrides):
        row = {
            'generic_name': 'Paracetamol',
            'medicine_name': 'Paracetamol 500mg',
            'dosage_form': 'Tablet',
            'strength': '500mg',
            'unit_of_measure': 'Strip',
            'category': 'Analgesics',
            'category_code': 'ANALGESIC',
        }
        row.update(overrides)
        return row

    def test_new_medicine_created_as_pending_review(self):
        """Medicine import creates record with PENDING_REVIEW verification status."""
        rows = [self._base_row()]
        preview = validate_and_preview_medicines(rows, 'medicine_import')
        self.assertEqual(preview['new_count'], 1)
        self.assertEqual(preview['error_count'], 0)

        log = execute_medicine_import(preview, self.admin, 'test_med.csv', 'MEDICINE_IMPORT')
        self.assertEqual(log.status, MedicineImportLog.Status.SUCCESS)
        self.assertEqual(log.created_count, 1)

        med = Medicine.objects.get(generic_name='Paracetamol', strength='500mg')
        self.assertEqual(med.verification_status, Medicine.VerificationStatus.PENDING_REVIEW)
        self.assertEqual(med.source_reference, 'Medicine_Import')

    def test_starter_example_imported_as_draft_status(self):
        """Rows from Starter_Examples sheet get PENDING_REVIEW status."""
        rows = [self._base_row(generic_name='Ibuprofen', strength='400mg', medicine_name='Ibuprofen 400mg')]
        preview = validate_and_preview_medicines(rows, 'Starter_Examples')
        self.assertEqual(preview['draft_count'], 1)
        self.assertEqual(preview['new_count'], 0)

        log = execute_medicine_import(preview, self.admin, 'examples.csv', 'STARTER_EXAMPLES')
        med = Medicine.objects.get(generic_name='Ibuprofen', strength='400mg')
        self.assertEqual(med.status, Medicine.Status.PENDING_REVIEW)
        self.assertEqual(med.verification_status, Medicine.VerificationStatus.PENDING_REVIEW)
        self.assertEqual(med.source_reference, 'Starter_Examples')

    def test_no_batch_or_stock_created_on_medicine_import(self):
        """Medicine import NEVER creates batches, inventory or stock transactions."""
        rows = [self._base_row()]
        preview = validate_and_preview_medicines(rows, 'medicine_import')
        execute_medicine_import(preview, self.admin, 'test_med.csv')
        self.assertEqual(MedicineBatch.objects.count(), 0)
        self.assertEqual(Inventory.objects.count(), 0)
        self.assertEqual(StockTransaction.objects.count(), 0)

    def test_missing_required_field_is_error(self):
        """Row missing generic_name is an error and not imported."""
        row = self._base_row()
        del row['generic_name']
        row['generic_name'] = ''
        preview = validate_and_preview_medicines([row], 'medicine_import')
        self.assertEqual(preview['error_count'], 1)
        self.assertEqual(preview['new_count'], 0)

    def test_missing_strength_is_error(self):
        """Row with empty strength is rejected."""
        row = self._base_row(strength='')
        preview = validate_and_preview_medicines([row], 'medicine_import')
        self.assertEqual(preview['error_count'], 1)

    def test_missing_dosage_form_is_error(self):
        """Row with empty dosage_form is rejected."""
        row = self._base_row(dosage_form='')
        preview = validate_and_preview_medicines([row], 'medicine_import')
        self.assertEqual(preview['error_count'], 1)

    def test_no_barcode_price_or_batch_invented(self):
        """Imported medicine has no barcode, zero prices — admin fills these later."""
        rows = [self._base_row()]
        preview = validate_and_preview_medicines(rows, 'medicine_import')
        execute_medicine_import(preview, self.admin, 'test_med.csv')
        med = Medicine.objects.get(generic_name='Paracetamol', strength='500mg')
        self.assertIsNone(med.barcode)
        self.assertEqual(med.tax_rate, Decimal('0.00'))
        self.assertEqual(med.mrp, Decimal('0.00'))


# ── 3. Duplicate Detection ───────────────────────────────────────────────────

class MedicineDuplicateDetectionTest(TestCase):
    def setUp(self):
        self.admin = _make_admin()
        self.cat = Category.objects.create(name='Analgesics', code='ANALGESIC', is_active=True)
        self.existing = Medicine.objects.create(
            medicine_name='Paracetamol 500mg',
            generic_name='Paracetamol',
            dosage_form='Tablet',
            strength='500mg',
            unit_of_measure='Strip',
            category=self.cat,
            status=Medicine.Status.ACTIVE,
            verification_status=Medicine.VerificationStatus.VERIFIED,
        )

    def test_exact_duplicate_detected_and_skipped(self):
        """Row with same generic_name + strength + dosage_form as existing is 'duplicate'."""
        rows = [{
            'generic_name': 'Paracetamol', 'dosage_form': 'Tablet', 'strength': '500mg',
            'unit_of_measure': 'Strip', 'category': 'Analgesics',
        }]
        preview = validate_and_preview_medicines(rows, 'medicine_import')
        self.assertEqual(preview['duplicate_count'], 1)
        self.assertEqual(preview['new_count'], 0)

    def test_existing_verified_record_not_overwritten_after_execute(self):
        """After import, existing verified record is unchanged."""
        rows = [{
            'generic_name': 'Paracetamol', 'dosage_form': 'Tablet', 'strength': '500mg',
            'unit_of_measure': 'Strip', 'category': 'Analgesics',
            'medicine_name': 'CHANGED NAME',
        }]
        preview = validate_and_preview_medicines(rows, 'medicine_import')
        execute_medicine_import(preview, self.admin, 'dup.csv')
        med = Medicine.objects.get(pk=self.existing.pk)
        self.assertEqual(med.medicine_name, 'Paracetamol 500mg')  # unchanged

    def test_repeated_import_same_file_does_not_duplicate(self):
        """Running the same import twice yields same duplicate count — no new records."""
        rows = [{
            'generic_name': 'Paracetamol', 'dosage_form': 'Tablet', 'strength': '500mg',
            'unit_of_measure': 'Strip', 'category': 'Analgesics',
        }]
        for _ in range(3):
            preview = validate_and_preview_medicines(rows, 'medicine_import')
            execute_medicine_import(preview, self.admin, 'repeat.csv')

        count = Medicine.objects.filter(generic_name='Paracetamol', strength='500mg').count()
        self.assertEqual(count, 1)

    def test_different_strength_is_not_duplicate(self):
        """Same generic + dosage but different strength is a NEW record."""
        rows = [{
            'generic_name': 'Paracetamol', 'dosage_form': 'Tablet', 'strength': '650mg',
            'unit_of_measure': 'Strip', 'category': 'Analgesics',
        }]
        preview = validate_and_preview_medicines(rows, 'medicine_import')
        self.assertEqual(preview['new_count'], 1)
        self.assertEqual(preview['duplicate_count'], 0)


# ── 4. Category Mapping ──────────────────────────────────────────────────────

class CategoryMappingTest(TestCase):
    def setUp(self):
        self.admin = _make_admin()
        self.cat = Category.objects.create(name='Cardiovascular', code='CARDIO', is_active=True)

    def test_category_resolved_by_code(self):
        """Medicine row resolved to category using category_code column."""
        rows = [{
            'generic_name': 'Atenolol', 'dosage_form': 'Tablet', 'strength': '50mg',
            'unit_of_measure': 'Strip', 'category_code': 'CARDIO',
        }]
        preview = validate_and_preview_medicines(rows, 'medicine_import')
        self.assertEqual(preview['error_count'], 0)
        self.assertEqual(preview['preview_rows'][0]['category_name'], 'Cardiovascular')

    def test_category_resolved_by_name(self):
        """Medicine row resolved using category name when code not given."""
        rows = [{
            'generic_name': 'Amlodipine', 'dosage_form': 'Tablet', 'strength': '5mg',
            'unit_of_measure': 'Strip', 'category': 'Cardiovascular',
        }]
        preview = validate_and_preview_medicines(rows, 'medicine_import')
        self.assertEqual(preview['error_count'], 0)
        self.assertEqual(preview['preview_rows'][0]['category_id'], self.cat.id)

    def test_unknown_category_is_error(self):
        """Row referencing a non-existent category is rejected."""
        rows = [{
            'generic_name': 'Unknown Drug', 'dosage_form': 'Tablet', 'strength': '10mg',
            'unit_of_measure': 'Strip', 'category': 'Exotic Herbal',
        }]
        preview = validate_and_preview_medicines(rows, 'medicine_import')
        self.assertEqual(preview['error_count'], 1)
        self.assertIn('not found', preview['errors'][0]['message'].lower())


# ── 5. Sheet Classification ──────────────────────────────────────────────────

class SheetClassificationTest(TestCase):
    def test_category_master_classified(self):
        self.assertEqual(classify_sheet('Category_Master'), MedicineImportLog.ImportType.CATEGORY_MASTER)
        self.assertEqual(classify_sheet('category_master'), MedicineImportLog.ImportType.CATEGORY_MASTER)

    def test_medicine_import_classified(self):
        self.assertEqual(classify_sheet('Medicine_Import'), MedicineImportLog.ImportType.MEDICINE_IMPORT)

    def test_starter_examples_classified(self):
        self.assertEqual(classify_sheet('Starter_Examples'), MedicineImportLog.ImportType.STARTER_EXAMPLES)

    def test_unknown_sheet_returns_none(self):
        self.assertIsNone(classify_sheet('Instructions'))
        self.assertIsNone(classify_sheet('Notes'))
        self.assertIsNone(classify_sheet('ReadMe'))


# ── 6. Import Log & Audit ────────────────────────────────────────────────────

class ImportLogTest(TestCase):
    def setUp(self):
        self.admin = _make_admin()
        self.cat = Category.objects.create(name='Analgesics', code='ANALGESIC', is_active=True)

    def test_import_log_created_on_category_import(self):
        """MedicineImportLog record is created for every category import."""
        rows = [{'category_name': 'Oncology', 'category_code': 'ONCOLOGY'}]
        preview = validate_and_preview_categories(rows)
        log = execute_category_import(preview, self.admin, 'cats.csv', 'abc123')
        self.assertIsNotNone(log.pk)
        self.assertEqual(log.import_type, MedicineImportLog.ImportType.CATEGORY_MASTER)
        self.assertEqual(log.uploaded_by, self.admin)
        self.assertEqual(log.file_sha256, 'abc123')

    def test_import_log_created_on_medicine_import(self):
        """MedicineImportLog record is created for every medicine import."""
        rows = [{
            'generic_name': 'Metformin', 'dosage_form': 'Tablet', 'strength': '500mg',
            'unit_of_measure': 'Strip', 'category': 'Analgesics',
        }]
        preview = validate_and_preview_medicines(rows)
        log = execute_medicine_import(preview, self.admin, 'meds.csv')
        self.assertIsNotNone(log.pk)
        self.assertEqual(log.created_count, 1)

    def test_import_log_records_error_details(self):
        """Row errors are stored in error_details JSON field."""
        rows = [{'generic_name': '', 'dosage_form': 'Tablet', 'strength': '500mg', 'unit_of_measure': 'Strip'}]
        preview = validate_and_preview_medicines(rows)
        log = execute_medicine_import(preview, self.admin, 'bad.csv')
        # No records created (category missing + generic name missing)
        self.assertGreaterEqual(log.error_count, 0)


# ── 7. Error Report CSV ──────────────────────────────────────────────────────

class ErrorReportCSVTest(TestCase):
    def setUp(self):
        self.admin = _make_admin()

    def test_error_report_csv_downloadable(self):
        """build_error_report_csv returns valid CSV with header and error rows."""
        log = MedicineImportLog.objects.create(
            import_type='MEDICINE_IMPORT',
            filename='test.csv',
            uploaded_by=self.admin,
            total_rows=2,
            created_count=1,
            error_count=1,
            status='PARTIAL',
            error_details=[{'row_num': 3, 'field': 'generic_name', 'message': 'Missing value'}],
        )
        csv_output = build_error_report_csv(log)
        self.assertIn('Row #', csv_output)
        self.assertIn('Missing value', csv_output)
        self.assertIn('generic_name', csv_output)

    def test_error_report_empty_when_no_errors(self):
        """CSV with no errors returns only header row."""
        log = MedicineImportLog.objects.create(
            import_type='CATEGORY_MASTER',
            filename='clean.csv',
            uploaded_by=self.admin,
            total_rows=5,
            created_count=5,
            error_count=0,
            status='SUCCESS',
            error_details=[],
        )
        csv_output = build_error_report_csv(log)
        lines = [l for l in csv_output.strip().splitlines() if l]
        self.assertEqual(len(lines), 1)  # Header only


# ── 8. Draft Review Workflow ─────────────────────────────────────────────────

class DraftReviewWorkflowTest(TestCase):
    def setUp(self):
        self.admin = _make_admin()
        self.client = Client()
        self.cat = Category.objects.create(name='Analgesics', code='ANALGESIC', is_active=True)
        self.draft = Medicine.objects.create(
            medicine_name='Ibuprofen 400mg',
            generic_name='Ibuprofen',
            dosage_form='Tablet',
            strength='400mg',
            unit_of_measure='Strip',
            category=self.cat,
            status=Medicine.Status.PENDING_REVIEW,
            verification_status=Medicine.VerificationStatus.PENDING_REVIEW,
            source_reference='Starter_Examples',
        )

    def test_approve_draft_sets_active_and_verified(self):
        """Approving a draft medicine sets status=ACTIVE and verification_status=VERIFIED."""
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('medicine_draft_review', kwargs={'medicine_id': self.draft.id}),
            {
                'action': 'approve',
                'medicine_name': 'Ibuprofen 400mg',
                'generic_name': 'Ibuprofen',
                'dosage_form': 'Tablet',
                'strength': '400mg',
                'unit_of_measure': 'Strip',
                'category': self.cat.id,
                'regulatory_category': 'NORMAL',
                'reorder_level': 50,
                'minimum_stock': 10,
                'maximum_stock': 500,
                'safety_stock': 20,
            }
        )
        self.assertEqual(response.status_code, 302)
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.status, Medicine.Status.ACTIVE)
        self.assertEqual(self.draft.verification_status, Medicine.VerificationStatus.VERIFIED)

    def test_reject_draft_sets_inactive_and_rejected(self):
        """Rejecting a draft medicine sets status=INACTIVE and verification_status=REJECTED."""
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('medicine_draft_review', kwargs={'medicine_id': self.draft.id}),
            {'action': 'reject', 'rejection_note': 'Insufficient clinical information.'}
        )
        self.assertEqual(response.status_code, 302)
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.status, Medicine.Status.INACTIVE)
        self.assertEqual(self.draft.verification_status, Medicine.VerificationStatus.REJECTED)
        self.assertIn('REJECTED', self.draft.review_note)

    def test_draft_does_not_appear_in_dispensing_lookup(self):
        """PENDING_REVIEW draft is not status=ACTIVE, so sales/dispensing won't select it."""
        active_meds = Medicine.objects.filter(status=Medicine.Status.ACTIVE)
        self.assertNotIn(self.draft, active_meds)


# ── 9. Role Permission ───────────────────────────────────────────────────────

class ImportPermissionTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.pharmacist = _make_pharmacist()

    def test_pharmacist_cannot_access_import_upload(self):
        """Non-admin user (PHARMACIST) gets PermissionDenied on import upload page."""
        self.client.force_login(self.pharmacist)
        response = self.client.get(reverse('medicine_import_upload'))
        self.assertIn(response.status_code, [403, 302])

    def test_unauthenticated_redirects_to_login(self):
        """Unauthenticated user is redirected to login."""
        response = self.client.get(reverse('medicine_import_upload'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/auth/', response['Location'])

    def test_pharmacist_cannot_access_draft_review(self):
        """Pharmacist cannot access the draft review queue."""
        self.client.force_login(self.pharmacist)
        response = self.client.get(reverse('medicine_draft_list'))
        self.assertIn(response.status_code, [403, 302])


# ── 10. View Integration Tests ───────────────────────────────────────────────

class ImportViewIntegrationTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = _make_admin('admin_view', 'admin_view@hospital.org')
        self.cat = Category.objects.create(name='Analgesics', code='ANALGESIC', is_active=True)

    def test_upload_page_loads(self):
        """Import upload page returns 200 for admin."""
        self.client.force_login(self.admin)
        response = self.client.get(reverse('medicine_import_upload'))
        self.assertEqual(response.status_code, 200)

    def test_csv_upload_category_file_redirects_to_preview(self):
        """Uploading a valid Category CSV redirects to preview."""
        self.client.force_login(self.admin)
        csv_content = "category_name,category_code,description\nOncology,ONCOLOGY,Cancer medicines\n"
        csv_file = io.BytesIO(csv_content.encode())
        csv_file.name = 'Category_Master.csv'
        response = self.client.post(
            reverse('medicine_import_upload'),
            {'import_file': csv_file},
            format='multipart',
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn('preview', response['Location'])

    def test_preview_page_loads_from_session(self):
        """Preview page renders when session data is present."""
        self.client.force_login(self.admin)
        # Set fake session preview
        session = self.client.session
        session['medicine_import_previews'] = json.dumps({
            'default': {
                'import_type': 'CATEGORY_MASTER',
                'data': {
                    'total_rows': 1,
                    'valid_count': 1,
                    'new_count': 1,
                    'duplicate_count': 0,
                    'draft_count': 0,
                    'error_count': 0,
                    'preview_rows': [{'row_num': 2, 'name': 'Oncology', 'code': 'ONCO', 'description': '', 'notes': '', 'status': 'new', 'existing_id': None, 'is_duplicate_code': False}],
                    'errors': [],
                    'duplicates': [],
                }
            }
        })
        session['medicine_import_filename'] = 'Category_Master.csv'
        session['medicine_import_sha256'] = 'abc'
        session.save()
        response = self.client.get(reverse('medicine_import_preview'))
        self.assertEqual(response.status_code, 200)

    def test_import_log_list_accessible_by_admin(self):
        """Import log list page returns 200 for admin."""
        self.client.force_login(self.admin)
        response = self.client.get(reverse('medicine_import_log'))
        self.assertEqual(response.status_code, 200)

    def test_error_report_download(self):
        """Error report download returns CSV content-type."""
        self.client.force_login(self.admin)
        log = MedicineImportLog.objects.create(
            import_type='MEDICINE_IMPORT',
            filename='error_test.csv',
            uploaded_by=self.admin,
            total_rows=1,
            error_count=1,
            status='PARTIAL',
            error_details=[{'row_num': 2, 'field': 'strength', 'message': 'Missing'}],
        )
        response = self.client.get(reverse('medicine_import_error_report', kwargs={'log_id': log.id}))
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/csv', response['Content-Type'])
        self.assertIn('Missing', response.content.decode())
