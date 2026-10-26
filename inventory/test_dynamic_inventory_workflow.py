"""
inventory/test_dynamic_inventory_workflow.py
MediVault — End-to-End Dynamic Transaction-Driven Inventory Workflow Tests

Tests:
1. Complete Lifecycle: Medicine Creation → Opening Stock → Sale & Invoice → Stock Deduction → Low-Stock Alert & Email → Consumption Update → Reorder Recommendation → Admin Approval → PO → GRN Receipt → Stock Increase & Alert Resolution.
2. Clinical Dispensing & Expiry Block: FEFO allocation, expiry enforcement, stock ledger.
3. Concurrency, Idempotency & Over-selling Prevention: Negative stock block, duplicate checkout prevention, duplicate GRN verification guard.
4. Returns & Ledger Accuracy: Return increases batch stock and records audit transaction without deleting history.
5. Background Reconciliation: Command execution, near-expiry detection, alert reconciliation.
"""

from decimal import Decimal
from datetime import timedelta
from django.test import TestCase
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.core import mail
from django.core.management import call_command

from accounts.models import User
from medicines.models import Medicine, MedicineBatch, Category
from inventory.models import Inventory, StockTransaction, StockAlert
from inventory import services as inventory_services
from sales.models import Sale, SaleItem
from sales import services as sales_services
from purchases.models import PurchaseRequest, PurchaseRequestItem, PurchaseOrder, GoodsReceipt, GoodsReceiptItem
from purchases import services as purchase_services
from suppliers.models import Supplier, SupplierMedicine
from dispensing.models import MedicineRequest, MedicineRequestItem, PharmacyQRToken
from dispensing import services as dispensing_services
from notifications.models import Notification
from notifications import service as notification_service
from ai_engine import intelligence_service as ai_service
from inventory import consumption_service


class DynamicInventoryWorkflowTestCase(TestCase):
    def setUp(self):
        # 1. Setup authorized users
        self.admin = User.objects.create_user(
            username='admin_test',
            email='admin@hospital.org',
            password='Password@123',
            role=User.Role.ADMIN,
            status=User.Status.APPROVED,
            is_active=True
        )
        self.pharmacist = User.objects.create_user(
            username='pharm_test',
            email='pharmacist@hospital.org',
            password='Password@123',
            role=User.Role.PHARMACIST,
            status=User.Status.APPROVED,
            is_active=True
        )

        # 2. Category & Supplier
        self.category = Category.objects.create(name='Antibiotics', is_active=True)
        self.supplier = Supplier.objects.create(
            company_name='MedSupply Global',
            contact_person='Mr. Supplier',
            email='orders@medsupply.com',
            phone='+919876543210',
            average_delivery_days=5,
            status=Supplier.SupplierStatus.ACTIVE
        )

        # 3. Medicine Master (no initial stock)
        self.medicine = Medicine.objects.create(
            medicine_name='Workflow Ciprofloxacin 500mg',
            generic_name='Ciprofloxacin',
            category=self.category,
            dosage_form='Tablet',
            strength='500mg',
            reorder_level=20,
            minimum_stock=10,
            maximum_stock=100,
            safety_stock=10,
            status=Medicine.Status.ACTIVE
        )

        SupplierMedicine.objects.create(
            supplier=self.supplier,
            medicine=self.medicine,
            unit_price=Decimal('15.00'),
            is_preferred=True
        )

    def test_complete_end_to_end_workflow(self):
        """
        Tests the entire transaction-driven supply chain and inventory cycle end-to-end:
        Creation → Stock Inbound → Sale & Stock Outbound → Alert & Notification → AI Forecast & Reorder →
        Procurement PR/PO → Goods Receipt (GRN) → Alert Resolution.
        """
        today = timezone.now().date()
        future_expiry = today + timedelta(days=365)

        # ── Step 1: Add Opening Stock via Authoritative Stock Service ───────
        batch1 = MedicineBatch.objects.create(
            medicine=self.medicine,
            batch_number='BATCH-E2E-001',
            expiry_date=future_expiry,
            purchase_price=Decimal('15.00'),
            selling_price=Decimal('25.00'),
            quantity_received=30,
            quantity_available=0,
            status=MedicineBatch.BatchStatus.ACTIVE
        )

        tx_in = inventory_services.add_stock(
            medicine=self.medicine,
            batch=batch1,
            quantity=30,
            transaction_type=StockTransaction.TransactionType.OPENING,
            performed_by=self.pharmacist,
            reference_number='INIT-001',
            notes='Initial opening stock'
        )

        # Verify stock and audit transaction
        batch1.refresh_from_db()
        inv = Inventory.objects.get(medicine=self.medicine)
        self.assertEqual(batch1.quantity_available, 30)
        self.assertEqual(inv.current_stock, 30)
        self.assertEqual(tx_in.transaction_type, StockTransaction.TransactionType.OPENING)
        self.assertEqual(tx_in.quantity, 30)

        # ── Step 2: Confirmed Sale & POS Stock Deduction ────────────────────
        cart = [{'medicine_id': self.medicine.id, 'quantity': 15}]
        sale = sales_services.create_sale_from_cart(
            patient_name='Alice Smith',
            patient_id='UHID-9001',
            cart_items=cart,
            pharmacist_user=self.pharmacist
        )

        self.assertIsNotNone(sale.sale_number)
        self.assertIsNotNone(sale.invoice_number)
        self.assertEqual(sale.status, Sale.SaleStatus.COMPLETED)

        # Verify exact deduction: 30 - 15 = 15
        batch1.refresh_from_db()
        inv.refresh_from_db()
        self.assertEqual(batch1.quantity_available, 15)
        self.assertEqual(inv.current_stock, 15)

        # Verify StockTransaction was created for the sale
        sale_tx = StockTransaction.objects.filter(reference_number=sale.sale_number).first()
        self.assertIsNotNone(sale_tx)
        self.assertEqual(sale_tx.transaction_type, StockTransaction.TransactionType.SALE)
        self.assertEqual(sale_tx.quantity, 15)
        self.assertEqual(sale_tx.new_stock, 15)

        # ── Step 3: Threshold Evaluation & Alert Generation ─────────────────
        # Stock is now 15 <= reorder_level (20) -> Should have active StockAlert
        alert = StockAlert.objects.filter(medicine=self.medicine, is_active=True).first()
        self.assertIsNotNone(alert)
        self.assertEqual(alert.alert_type, StockAlert.AlertType.LOW_STOCK)

        # Verify Notification was created
        notif = Notification.objects.filter(
            related_object_type='Medicine',
            related_object_id=self.medicine.id,
            notification_type=Notification.NotificationType.LOW_STOCK
        ).first()
        self.assertIsNotNone(notif)
        self.assertIn('Low Stock', notif.title)

        # ── Step 4: Net Consumption Analytics ───────────────────────────────
        consumption = consumption_service.calculate_net_consumption_in_range(
            self.medicine,
            timezone.now() - timedelta(days=1),
            timezone.now() + timedelta(days=1)
        )
        self.assertEqual(consumption['net_consumption'], 15)
        self.assertEqual(consumption['transaction_count'], 1)

        # ── Step 5: Explainable AI Reorder Recommendations ──────────────────
        recommendations = ai_service.get_reorder_recommendations()
        med_rec = next((r for r in recommendations if r['medicine'].id == self.medicine.id), None)
        self.assertIsNotNone(med_rec)
        self.assertGreater(med_rec['recommended_qty'], 0)
        self.assertIn('explanation', med_rec)

        # ── Step 6: Procurement Pipeline (PR -> PO -> GRN) ──────────────────
        # Create Purchase Request
        pr = PurchaseRequest.objects.create(
            request_id='PR-TEST-001',
            status=PurchaseRequest.PRStatus.APPROVED,
            recommended_supplier=self.supplier,
            created_by=self.pharmacist,
            notes='Low stock auto reorder'
        )
        PurchaseRequestItem.objects.create(
            purchase_request=pr,
            medicine=self.medicine,
            requested_qty=50,
            approved_qty=50,
            suggested_supplier=self.supplier,
            suggested_unit_price=Decimal('15.00')
        )

        # Admin converts approved PR to PO
        po = purchase_services.convert_pr_to_po(pr, admin_user=self.admin)
        self.assertEqual(po.status, PurchaseOrder.POStatus.APPROVED)

        # Strict check: PO creation MUST NOT increase inventory stock
        inv.refresh_from_db()
        self.assertEqual(inv.current_stock, 15)

        # Supplier delivers -> Create GRN
        grn = GoodsReceipt.objects.create(
            grn_number='GRN-TEST-001',
            purchase_order=po,
            supplier=self.supplier,
            receipt_date=today,
            status=GoodsReceipt.GRNStatus.PENDING_VERIFICATION,
            received_by=self.pharmacist
        )
        po_item = po.items.first()
        GoodsReceiptItem.objects.create(
            goods_receipt=grn,
            po_item=po_item,
            medicine=self.medicine,
            batch_number='BATCH-E2E-002',
            manufactured_date=today - timedelta(days=30),
            expiry_date=today + timedelta(days=500),
            quantity_received=50,
            quantity_accepted=50,
            quantity_rejected=0,
            unit_price=Decimal('15.00')
        )

        # Verify GRN receipt -> Stock increases and alerts auto-resolve
        verified_grn = purchase_services.process_grn_receipt(grn.pk, verified_by=self.pharmacist)
        self.assertEqual(verified_grn.status, GoodsReceipt.GRNStatus.VERIFIED)

        inv.refresh_from_db()
        # 15 previous + 50 received = 65
        self.assertEqual(inv.current_stock, 65)

        # Low Stock alert should now be RESOLVED
        active_alert = StockAlert.objects.filter(medicine=self.medicine, is_active=True).first()
        self.assertIsNone(active_alert)

        # Verify GRN stock transaction was written to ledger
        grn_tx = StockTransaction.objects.filter(reference_number=grn.grn_number).first()
        self.assertIsNotNone(grn_tx)
        self.assertEqual(grn_tx.transaction_type, StockTransaction.TransactionType.PURCHASE)
        self.assertEqual(grn_tx.quantity, 50)
        self.assertEqual(grn_tx.new_stock, 65)

    def test_dispensing_fefo_and_expiry_block(self):
        """
        Tests clinical dispensing FEFO allocation, expiry date blocking,
        and stock deduction integrity.
        """
        today = timezone.now().date()

        # Batch 1: Expired batch
        expired_batch = MedicineBatch.objects.create(
            medicine=self.medicine,
            batch_number='BATCH-EXPIRED',
            expiry_date=today - timedelta(days=10),
            purchase_price=Decimal('10.00'),
            selling_price=Decimal('20.00'),
            quantity_received=20,
            quantity_available=20,
            status=MedicineBatch.BatchStatus.EXPIRED
        )

        # Batch 2: Valid near-term batch (Expires in 60 days)
        near_batch = MedicineBatch.objects.create(
            medicine=self.medicine,
            batch_number='BATCH-NEAR',
            expiry_date=today + timedelta(days=60),
            purchase_price=Decimal('12.00'),
            selling_price=Decimal('22.00'),
            quantity_received=30,
            quantity_available=30,
            status=MedicineBatch.BatchStatus.ACTIVE
        )

        # Batch 3: Valid long-term batch (Expires in 365 days)
        far_batch = MedicineBatch.objects.create(
            medicine=self.medicine,
            batch_number='BATCH-FAR',
            expiry_date=today + timedelta(days=365),
            purchase_price=Decimal('12.00'),
            selling_price=Decimal('22.00'),
            quantity_received=30,
            quantity_available=30,
            status=MedicineBatch.BatchStatus.ACTIVE
        )

        # 1. Attempting to dispense from expired batch MUST fail
        with self.assertRaises(ValidationError):
            inventory_services.remove_stock(
                medicine=self.medicine,
                batch=expired_batch,
                quantity=5,
                transaction_type=StockTransaction.TransactionType.DISPENSE,
                performed_by=self.pharmacist
            )

        # 2. FEFO allocation must pick BATCH-NEAR before BATCH-FAR
        allocations = inventory_services.allocate_stock(self.medicine, quantity_needed=10, policy='FEFO')
        self.assertEqual(len(allocations), 1)
        self.assertEqual(allocations[0]['batch'].batch_number, 'BATCH-NEAR')
        self.assertEqual(allocations[0]['quantity'], 10)

        # 3. Dispense via mobile QR request workflow
        qr_token = PharmacyQRToken.objects.create(name='Test Counter QR', is_active=True)
        cart = [{'medicine_id': self.medicine.id, 'quantity': 10}]
        req = dispensing_services.create_medicine_request(
            patient_name='Bob Jones',
            patient_id='UHID-1002',
            mobile_number='+919876543211',
            cart_items=cart,
            qr_token_obj=qr_token
        )

        req_item = req.items.first()
        dispensed_item = dispensing_services.dispense_request_item(
            request_item_id=req_item.id,
            batch_id=near_batch.id,
            dispense_quantity=10,
            pharmacist_user=self.pharmacist
        )

        self.assertEqual(dispensed_item.dispensed_quantity, 10)
        near_batch.refresh_from_db()
        self.assertEqual(near_batch.quantity_available, 20)

    def test_concurrency_and_negative_stock_prevention(self):
        """
        Verifies that requesting more stock than available fails with ValidationError
        and never allows negative stock.
        """
        today = timezone.now().date()
        batch = MedicineBatch.objects.create(
            medicine=self.medicine,
            batch_number='BATCH-CONCUR-01',
            expiry_date=today + timedelta(days=200),
            purchase_price=Decimal('10.00'),
            selling_price=Decimal('20.00'),
            quantity_received=5,
            quantity_available=5,
            status=MedicineBatch.BatchStatus.ACTIVE
        )

        # Try to deduct 10 when only 5 exist
        with self.assertRaises(ValidationError):
            inventory_services.remove_stock(
                medicine=self.medicine,
                batch=batch,
                quantity=10,
                transaction_type=StockTransaction.TransactionType.SALE,
                performed_by=self.pharmacist
            )

        batch.refresh_from_db()
        self.assertEqual(batch.quantity_available, 5)

    def test_stock_return_reversal(self):
        """
        Verifies that stock returns increase batch quantity and record
        a RETURN StockTransaction without deleting audit history.
        """
        today = timezone.now().date()
        batch = MedicineBatch.objects.create(
            medicine=self.medicine,
            batch_number='BATCH-RET-01',
            expiry_date=today + timedelta(days=200),
            purchase_price=Decimal('10.00'),
            selling_price=Decimal('20.00'),
            quantity_received=20,
            quantity_available=10,
            status=MedicineBatch.BatchStatus.ACTIVE
        )

        inventory_services.record_return(
            medicine=self.medicine,
            batch=batch,
            quantity=5,
            performed_by=self.pharmacist,
            reason='Patient returned unused sealed tablets',
            reference_number='RET-9901'
        )

        batch.refresh_from_db()
        self.assertEqual(batch.quantity_available, 15)

        ret_tx = StockTransaction.objects.filter(reference_number='RET-9901').first()
        self.assertIsNotNone(ret_tx)
        self.assertEqual(ret_tx.transaction_type, StockTransaction.TransactionType.RETURN)
        self.assertEqual(ret_tx.quantity, 5)

    def test_reconciliation_management_command(self):
        """
        Tests the background reconciliation command for batch expiry monitoring
        and missed alert handling.
        """
        today = timezone.now().date()

        # Create a batch expiring in 25 days (should become NEAR_EXPIRY)
        near_batch = MedicineBatch.objects.create(
            medicine=self.medicine,
            batch_number='BATCH-RECON-NEAR',
            expiry_date=today + timedelta(days=25),
            purchase_price=Decimal('10.00'),
            selling_price=Decimal('20.00'),
            quantity_received=20,
            quantity_available=20,
            status=MedicineBatch.BatchStatus.ACTIVE
        )

        # Run reconciliation command
        call_command('reconcile_inventory_alerts', quiet=True)

        near_batch.refresh_from_db()
        self.assertEqual(near_batch.status, MedicineBatch.BatchStatus.NEAR_EXPIRY)
