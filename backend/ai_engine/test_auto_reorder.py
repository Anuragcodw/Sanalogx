"""
ai_engine/test_auto_reorder.py
Phase 6 + Procurement Automation — Comprehensive Auto-Reorder Unit & Integration Tests

Covers all 10 required verification scenarios:
  1. Normal stock: no reorder request created.
  2. Low stock: correct reorder quantity calculated and requested.
  3. Zero stock: correct shortage calculation with critical priority.
  4. Expired batches excluded from usable stock calculation.
  5. Incoming stock from open approved Purchase Orders deducted from reorder quantity.
  6. Existing pending Purchase Request prevents duplicate order creation.
  7. Missing or inactive supplier handled safely without crashing.
  8. Repeated scheduled runs do not create duplicate orders (idempotency).
  9. Required approval is preserved (status PENDING_APPROVAL, no auto-dispatch).
  10. Receiving an order (Goods Receipt verification) updates stock exactly once.
"""

from decimal import Decimal
from datetime import timedelta
from django.test import TestCase, Client
from django.utils import timezone
from django.urls import reverse
from django.core.exceptions import ValidationError

from accounts.models import User
from medicines.models import Medicine, MedicineBatch, Category
from inventory.models import Inventory, StockTransaction
from suppliers.models import Supplier, SupplierMedicine
from purchases.models import (
    PurchaseRequest, PurchaseRequestItem,
    PurchaseOrder, PurchaseOrderItem,
    GoodsReceipt, GoodsReceiptItem,
)
from purchases.services import convert_pr_to_po, process_grn_receipt
from ai_engine.auto_reorder_service import (
    get_usable_stock,
    get_confirmed_incoming_po_qty,
    get_existing_active_order_for_medicine,
    calculate_auto_reorder_details,
    create_auto_reorder_request,
    process_medicine_auto_reorder,
    run_auto_reorder,
)
from ai_engine.models import AIRecommendation


class AutoReorderComprehensiveTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.today = timezone.localdate()

        # Admin & Pharmacist Users
        self.admin = User.objects.create_superuser(
            username='admin_reorder',
            email='admin_reorder@sanalogx.org',
            password='Password123!',
            role='ADMIN',
            status='APPROVED',
        )
        self.pharmacist = User.objects.create_user(
            username='pharm_reorder',
            email='pharm_reorder@sanalogx.org',
            password='Password123!',
            role='PHARMACIST',
            status='APPROVED',
        )

        # Category
        self.category = Category.objects.create(name='Antibiotics & Anti-infectives', code='ANTIBIOTIC')

        # Active Supplier
        self.supplier = Supplier.objects.create(
            company_name='MedSource Global Ltd',
            status=Supplier.SupplierStatus.ACTIVE,
            rating=Decimal('4.8'),
            average_delivery_days=5,
        )

        # Standard Test Medicine
        self.medicine = Medicine.objects.create(
            medicine_name='Amoxicillin 500mg',
            generic_name='Amoxicillin',
            category=self.category,
            dosage_form='Capsule',
            strength='500mg',
            unit_of_measure='Strip',
            reorder_level=50,
            safety_stock=20,
            minimum_stock=10,
            maximum_stock=500,
            status=Medicine.Status.ACTIVE,
        )

        # Supplier Catalog Entry
        self.sm = SupplierMedicine.objects.create(
            supplier=self.supplier,
            medicine=self.medicine,
            unit_price=Decimal('25.50'),
            delivery_days=5,
            pack_size=10,
            minimum_order_quantity=10,
            is_available=True,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # 1. Normal stock: no reorder
    # ─────────────────────────────────────────────────────────────────────────
    def test_normal_stock_no_reorder(self):
        """When usable stock is well above reorder level, no reorder request is created."""
        # Create healthy active batch with 200 units (reorder level is 50)
        MedicineBatch.objects.create(
            medicine=self.medicine,
            batch_number='BATCH-NORM-01',
            expiry_date=self.today + timedelta(days=365),
            purchase_price=Decimal('20.00'),
            selling_price=Decimal('30.00'),
            quantity_received=200,
            quantity_available=200,
            status=MedicineBatch.BatchStatus.ACTIVE,
        )
        Inventory.objects.create(
            medicine=self.medicine,
            current_stock=200,
            available_stock=200,
            reorder_level=50,
            safety_stock=20,
            maximum_stock=500,
        )

        outcome = process_medicine_auto_reorder(self.medicine)
        self.assertEqual(outcome['status'], 'SKIPPED_NORMAL_STOCK')
        self.assertFalse(outcome['details']['needs_reorder'])
        self.assertEqual(PurchaseRequest.objects.filter(items__medicine=self.medicine).count(), 0)

    # ─────────────────────────────────────────────────────────────────────────
    # 2. Low stock: correct reorder quantity
    # ─────────────────────────────────────────────────────────────────────────
    def test_low_stock_correct_reorder_quantity(self):
        """When usable stock falls to or below reorder level, correct quantity is calculated and requested."""
        # Stock = 20 units (below reorder level of 50)
        MedicineBatch.objects.create(
            medicine=self.medicine,
            batch_number='BATCH-LOW-01',
            expiry_date=self.today + timedelta(days=200),
            purchase_price=Decimal('20.00'),
            selling_price=Decimal('30.00'),
            quantity_received=20,
            quantity_available=20,
            status=MedicineBatch.BatchStatus.ACTIVE,
        )
        Inventory.objects.create(
            medicine=self.medicine,
            current_stock=20,
            available_stock=20,
            reorder_level=50,
            safety_stock=20,
            maximum_stock=500,
        )

        outcome = process_medicine_auto_reorder(self.medicine)
        self.assertEqual(outcome['status'], 'CREATED')
        details = outcome['details']
        self.assertTrue(details['needs_reorder'])
        self.assertGreater(details['calculated_qty'], 0)

        # Verify PR was created
        pr = outcome['result']['purchase_request']
        self.assertEqual(pr.status, PurchaseRequest.PRStatus.PENDING_APPROVAL)
        self.assertEqual(pr.recommended_supplier, self.supplier)

        item = pr.items.get(medicine=self.medicine)
        self.assertEqual(item.current_stock, 20)
        self.assertEqual(item.requested_qty, details['calculated_qty'])
        self.assertEqual(item.suggested_supplier, self.supplier)
        self.assertEqual(item.suggested_unit_price, Decimal('25.50'))

        # Pack size constraint: pack size is 10, so requested_qty must be a multiple of 10
        self.assertEqual(item.requested_qty % 10, 0)

    # ─────────────────────────────────────────────────────────────────────────
    # 3. Zero stock: correct shortage calculation & critical priority
    # ─────────────────────────────────────────────────────────────────────────
    def test_zero_stock_shortage_calculation(self):
        """Zero usable stock triggers immediate critical shortage replenishment."""
        Inventory.objects.create(
            medicine=self.medicine,
            current_stock=0,
            available_stock=0,
            reorder_level=50,
            safety_stock=20,
            maximum_stock=500,
        )

        outcome = process_medicine_auto_reorder(self.medicine)
        self.assertEqual(outcome['status'], 'CREATED')
        details = outcome['details']
        self.assertEqual(details['usable_stock'], 0)
        self.assertEqual(details['priority'], PurchaseRequest.Priority.CRITICAL)

        pr = outcome['result']['purchase_request']
        self.assertEqual(pr.priority, PurchaseRequest.Priority.CRITICAL)
        self.assertIn("OUT OF STOCK", pr.reason)

        # Quantity covers reorder level + safety stock
        item = pr.items.get(medicine=self.medicine)
        self.assertGreaterEqual(item.requested_qty, self.medicine.reorder_level + self.medicine.safety_stock)

    # ─────────────────────────────────────────────────────────────────────────
    # 4. Expired batches excluded from usable stock
    # ─────────────────────────────────────────────────────────────────────────
    def test_expired_batches_excluded_from_usable_stock(self):
        """Expired or quarantined batches are strictly excluded from usable stock."""
        # 100 units physically on shelf, but expired 10 days ago
        MedicineBatch.objects.create(
            medicine=self.medicine,
            batch_number='BATCH-EXP-01',
            expiry_date=self.today - timedelta(days=10),
            purchase_price=Decimal('20.00'),
            selling_price=Decimal('30.00'),
            quantity_received=100,
            quantity_available=100,
            status=MedicineBatch.BatchStatus.EXPIRED,
        )
        # 50 units quarantined due to quality issue
        MedicineBatch.objects.create(
            medicine=self.medicine,
            batch_number='BATCH-QUAR-01',
            expiry_date=self.today + timedelta(days=180),
            purchase_price=Decimal('20.00'),
            selling_price=Decimal('30.00'),
            quantity_received=50,
            quantity_available=50,
            status=MedicineBatch.BatchStatus.QUARANTINED,
        )
        Inventory.objects.create(
            medicine=self.medicine,
            current_stock=150,  # physical sum on shelf
            available_stock=0,
            reorder_level=50,
            safety_stock=20,
            maximum_stock=500,
        )

        usable = get_usable_stock(self.medicine)
        self.assertEqual(usable, 0)

        outcome = process_medicine_auto_reorder(self.medicine)
        self.assertEqual(outcome['status'], 'CREATED')
        self.assertEqual(outcome['details']['usable_stock'], 0)
        self.assertEqual(outcome['details']['priority'], PurchaseRequest.Priority.CRITICAL)

    # ─────────────────────────────────────────────────────────────────────────
    # 5. Incoming stock deducted from required reorder quantity
    # ─────────────────────────────────────────────────────────────────────────
    def test_incoming_stock_deducted_from_reorder_quantity(self):
        """Confirmed open Purchase Orders are deducted from the reorder quantity so pharmacy does not over-order."""
        # Usable stock = 20 (reorder level = 50)
        MedicineBatch.objects.create(
            medicine=self.medicine,
            batch_number='BATCH-BASE-01',
            expiry_date=self.today + timedelta(days=200),
            purchase_price=Decimal('20.00'),
            selling_price=Decimal('30.00'),
            quantity_received=20,
            quantity_available=20,
            status=MedicineBatch.BatchStatus.ACTIVE,
        )
        Inventory.objects.create(
            medicine=self.medicine,
            current_stock=20,
            available_stock=20,
            reorder_level=50,
            safety_stock=20,
            maximum_stock=500,
        )

        # Baseline details without PO
        base_details = calculate_auto_reorder_details(self.medicine)
        base_qty = base_details['calculated_qty']

        # Create confirmed approved PO with 40 units pending delivery
        po = PurchaseOrder.objects.create(
            po_number='PO-2026-INCOMING',
            supplier=self.supplier,
            status=PurchaseOrder.POStatus.APPROVED,
            created_by=self.admin,
        )
        PurchaseOrderItem.objects.create(
            purchase_order=po,
            medicine=self.medicine,
            quantity_ordered=40,
            quantity_pending=40,
            unit_price=Decimal('25.50'),
        )

        incoming = get_confirmed_incoming_po_qty(self.medicine)
        self.assertEqual(incoming, 40)

        new_details = calculate_auto_reorder_details(self.medicine)
        new_qty = new_details['calculated_qty']

        # With 40 units incoming, the reorder quantity must be less than base_qty
        self.assertLess(new_qty, base_qty)

    # ─────────────────────────────────────────────────────────────────────────
    # 6. Existing pending order prevents duplication
    # ─────────────────────────────────────────────────────────────────────────
    def test_existing_pending_order_prevents_duplication(self):
        """If an active PurchaseRequest is already pending review, auto-reorder does not create a duplicate."""
        Inventory.objects.create(
            medicine=self.medicine,
            current_stock=10,
            available_stock=10,
            reorder_level=50,
            safety_stock=20,
            maximum_stock=500,
        )

        # First run creates the PR
        run1 = process_medicine_auto_reorder(self.medicine)
        self.assertEqual(run1['status'], 'CREATED')
        initial_pr_count = PurchaseRequest.objects.filter(items__medicine=self.medicine).count()
        self.assertEqual(initial_pr_count, 1)

        # Second run evaluates the same medicine while PR is still PENDING_APPROVAL
        run2 = process_medicine_auto_reorder(self.medicine)
        self.assertEqual(run2['status'], 'SKIPPED_EXISTING_ORDER')
        self.assertIn("already in progress", run2['message'])

        # Total PR count remains exactly 1
        final_pr_count = PurchaseRequest.objects.filter(items__medicine=self.medicine).count()
        self.assertEqual(final_pr_count, 1)

    # ─────────────────────────────────────────────────────────────────────────
    # 7. Missing or inactive supplier handled safely
    # ─────────────────────────────────────────────────────────────────────────
    def test_missing_or_inactive_supplier_handled_safely(self):
        """When medicine has no active supplier, auto-reorder completes safely without unhandled exceptions."""
        # Deactivate existing supplier
        self.supplier.status = Supplier.SupplierStatus.INACTIVE
        self.supplier.save()

        # Create orphan medicine with no suppliers at all
        orphan_med = Medicine.objects.create(
            medicine_name='Rare Orphan Compound 10mg',
            generic_name='Orphan Compound',
            category=self.category,
            dosage_form='Vial',
            strength='10mg',
            unit_of_measure='Vial',
            reorder_level=30,
            safety_stock=10,
            minimum_stock=5,
            maximum_stock=100,
            status=Medicine.Status.ACTIVE,
        )
        Inventory.objects.create(
            medicine=orphan_med,
            current_stock=5,
            available_stock=5,
            reorder_level=30,
            safety_stock=10,
            maximum_stock=100,
        )

        outcome = process_medicine_auto_reorder(orphan_med)
        self.assertEqual(outcome['status'], 'CREATED')

        pr = outcome['result']['purchase_request']
        self.assertIsNone(pr.recommended_supplier)
        self.assertIn("NO ACTIVE SUPPLIER CONFIGURED", pr.notes)

        item = pr.items.get(medicine=orphan_med)
        self.assertIsNone(item.suggested_supplier)
        self.assertEqual(item.suggested_unit_price, Decimal('0.00'))

    # ─────────────────────────────────────────────────────────────────────────
    # 8. Repeated scheduled runs do not create duplicate orders
    # ─────────────────────────────────────────────────────────────────────────
    def test_repeated_scheduled_runs_idempotent(self):
        """Running the full auto-reorder engine multiple times creates orders on cycle 1 and skips them on cycle 2."""
        Inventory.objects.create(
            medicine=self.medicine,
            current_stock=15,
            available_stock=15,
            reorder_level=50,
            safety_stock=20,
            maximum_stock=500,
        )

        # Cycle 1: Generates PR
        stats1 = run_auto_reorder(medicine_ids=[self.medicine.id])
        self.assertEqual(stats1['requests_created'], 1)
        self.assertEqual(stats1['skipped_existing_order'], 0)

        # Cycle 2: Skips duplicate creation
        stats2 = run_auto_reorder(medicine_ids=[self.medicine.id])
        self.assertEqual(stats2['requests_created'], 0)
        self.assertEqual(stats2['skipped_existing_order'], 1)

        # Exactly 1 PR exists in the database
        total_prs = PurchaseRequest.objects.filter(items__medicine=self.medicine).count()
        self.assertEqual(total_prs, 1)

    # ─────────────────────────────────────────────────────────────────────────
    # 9. Required approval is preserved
    # ─────────────────────────────────────────────────────────────────────────
    def test_required_approval_is_preserved(self):
        """Auto-reorder creates a Purchase Request in PENDING_APPROVAL status and does NOT convert to PO without Admin review."""
        Inventory.objects.create(
            medicine=self.medicine,
            current_stock=10,
            available_stock=10,
            reorder_level=50,
            safety_stock=20,
            maximum_stock=500,
        )

        outcome = process_medicine_auto_reorder(self.medicine)
        pr = outcome['result']['purchase_request']

        # Status must be PENDING_APPROVAL
        self.assertEqual(pr.status, PurchaseRequest.PRStatus.PENDING_APPROVAL)
        self.assertIsNone(pr.converted_to_po)

        # Converting an unapproved PR to PO must raise ValidationError
        with self.assertRaises(ValidationError):
            convert_pr_to_po(pr, admin_user=self.admin)

        # Explicit admin approval is required
        pr.status = PurchaseRequest.PRStatus.APPROVED
        pr.reviewed_by = self.admin
        pr.save()

        # Now conversion succeeds
        po = convert_pr_to_po(pr, admin_user=self.admin)
        self.assertIsNotNone(po)
        self.assertEqual(po.status, PurchaseOrder.POStatus.APPROVED)
        pr.refresh_from_db()
        self.assertEqual(pr.status, PurchaseRequest.PRStatus.CONVERTED_TO_PO)

    # ─────────────────────────────────────────────────────────────────────────
    # 10. Receiving an order updates stock exactly once
    # ─────────────────────────────────────────────────────────────────────────
    def test_receiving_order_updates_stock_exactly_once(self):
        """Goods Receipt (GRN) processing increases stock exactly once upon physical verification."""
        initial_batch = MedicineBatch.objects.create(
            medicine=self.medicine,
            batch_number='INIT-BATCH-01',
            expiry_date=self.today + timedelta(days=180),
            purchase_price=Decimal('20.00'),
            selling_price=Decimal('30.00'),
            quantity_received=10,
            quantity_available=10,
            status=MedicineBatch.BatchStatus.ACTIVE,
        )
        inv = Inventory.objects.create(
            medicine=self.medicine,
            current_stock=10,
            available_stock=10,
            reorder_level=50,
            safety_stock=20,
            maximum_stock=500,
        )

        # 1. Create and approve PR -> PO
        outcome = process_medicine_auto_reorder(self.medicine)
        pr = outcome['result']['purchase_request']
        pr.status = PurchaseRequest.PRStatus.APPROVED
        pr.reviewed_by = self.admin
        pr.save()

        po = convert_pr_to_po(pr, admin_user=self.admin)
        po_item = po.items.first()
        ordered_qty = po_item.quantity_ordered

        # Stock before GRN is unchanged (still 10 units)
        inv.refresh_from_db()
        self.assertEqual(inv.current_stock, 10)

        # 2. Goods arrive: create GRN and line item
        grn = GoodsReceipt.objects.create(
            grn_number=f"GRN-2026-TEST01",
            purchase_order=po,
            supplier=self.supplier,
            receipt_date=self.today,
            status=GoodsReceipt.GRNStatus.PENDING_VERIFICATION,
            received_by=self.pharmacist,
        )
        GoodsReceiptItem.objects.create(
            goods_receipt=grn,
            po_item=po_item,
            medicine=self.medicine,
            quantity_received=ordered_qty,
            quantity_accepted=ordered_qty,
            quantity_rejected=0,
            batch_number='DELIVERED-BATCH-01',
            manufactured_date=self.today - timedelta(days=30),
            expiry_date=self.today + timedelta(days=365),
            unit_price=po_item.unit_price,
        )

        # Stock is still unchanged before QC verification
        inv.refresh_from_db()
        self.assertEqual(inv.current_stock, 10)

        # 3. QC verification processes receipt and creates batch + inventory
        process_grn_receipt(grn.id, verified_by=self.admin)

        inv.refresh_from_db()
        # Stock must have increased by exactly ordered_qty
        expected_stock = 10 + ordered_qty
        self.assertEqual(inv.current_stock, expected_stock)

        # 4. Attempting to process the same GRN again must fail (preventing double counting)
        grn.refresh_from_db()
        self.assertEqual(grn.status, GoodsReceipt.GRNStatus.VERIFIED)
        with self.assertRaises(ValidationError):
            process_grn_receipt(grn.id, verified_by=self.admin)

        # Stock remains exactly the same
        inv.refresh_from_db()
        self.assertEqual(inv.current_stock, expected_stock)
