"""
inventory/test_stock_transaction_accuracy.py
Phase 1 — MediVault Stock Transaction Accuracy Test Suite

Covers:
  A1  - Verified goods receipt adds stock once with PURCHASE type
  A2  - Sale deducts stock via remove_stock(SALE)
  A3  - Dispensing deducts stock via remove_stock(DISPENSE)
  A4  - Customer return restores stock via record_return / add_stock(RETURN)
  A5  - Damage write-off deducts via record_damage(DAMAGE)
  A6  - Expiry write-off deducts via record_expiry(EXPIRY), marks batch EXPIRED
  A7  - Stock adjustment (Increase) goes through adjust_stock(ADJUSTMENT)
  A8  - Stock adjustment (Decrease) goes through adjust_stock(ADJUSTMENT)
  A9  - FEFO allocation: earliest expiry batch used first
  A10 - Multi-batch allocation: FEFO spans two batches correctly
  A11 - Negative stock prevention: remove_stock raises on over-deduction
  A12 - Negative stock prevention: record_damage raises on over-deduction
  A13 - Negative stock prevention: adjust_stock(Decrease) raises on over-deduction
  A14 - Expired batch blocked from SALE
  A15 - Quarantined batch blocked from DISPENSE
  A16 - Transaction number uniqueness: all tx numbers are distinct
  A17 - quantity_received only increments on PURCHASE (not RETURN)
  A18 - Duplicate GRN re-processing blocked
  A19 - Completed Sale delete raises ValidationError
  A20 - Completed DispensingRecord delete raises ValidationError
  A21 - Cancelled Sale excluded from net consumption
  A22 - Return reduces net consumption correctly
  A23 - batch_edit_view does NOT expose quantity_available for direct edit
  A24 - batch_edit_view does NOT expose quantity_received for direct edit
  A25 - record_return: no PURCHASE transaction created (RETURN type only)
  A26 - Transaction rollback: remove_stock is atomic (partial deduction impossible)
  A27 - Inventory recalculated correctly after multi-step workflow
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from django.core.exceptions import ValidationError

from accounts.models import User
from medicines.models import Category, Medicine, MedicineBatch
from inventory.models import Inventory, StockTransaction
from inventory import services as inventory_services
from inventory.consumption_service import calculate_net_consumption_in_range
from sales.models import Sale
from dispensing.models import DispensingRecord
from medicines.forms import BatchEditForm


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

def _make_user(username, role=None):
    if role is None:
        role = User.Role.PHARMACIST
    return User.objects.create_user(
        username=username,
        email=f'{username}@hospital.org',
        password='Password123!',
        role=role,
        status=User.Status.APPROVED,
        email_verified=True,
        is_active=True,
    )


def _make_medicine(name='TestMed', barcode=None):
    cat, _ = Category.objects.get_or_create(
        name='StockAccuracyTest', defaults={'is_active': True}
    )
    barcode = barcode or f'BAR-{name[:8].upper()}-{id(name)}'
    return Medicine.objects.create(
        medicine_name=name,
        generic_name=name,
        category=cat,
        dosage_form='Tablet',
        strength='500mg',
        unit_of_measure='Strip',
        barcode=barcode,
        reorder_level=20,
        safety_stock=10,
        maximum_stock=500,
        status='ACTIVE',
    )


def _make_batch(medicine, qty_received=100, qty_available=0,
                days_to_expiry=365, batch_suffix='A', status='ACTIVE'):
    expiry = timezone.now().date() + timedelta(days=days_to_expiry)
    return MedicineBatch.objects.create(
        medicine=medicine,
        batch_number=f'B-{medicine.pk}-{batch_suffix}',
        expiry_date=expiry,
        quantity_received=qty_received,
        quantity_available=qty_available,
        purchase_price=Decimal('10.00'),
        selling_price=Decimal('15.00'),
        status=status,
    )


def _seed_stock(medicine, batch, qty, user, ref='PO-SEED'):
    return inventory_services.add_stock(
        medicine=medicine,
        batch=batch,
        quantity=qty,
        transaction_type=StockTransaction.TransactionType.PURCHASE,
        performed_by=user,
        reference_number=ref,
    )


# ---------------------------------------------------------------------------
# A1: Verified goods receipt adds stock once with PURCHASE type
# ---------------------------------------------------------------------------

class A1_VerifiedReceivingTest(TestCase):
    def setUp(self):
        self.user = _make_user('a1_pharm')
        self.medicine = _make_medicine('Amoxicillin', 'BAR-AMOX-A1')
        self.batch = _make_batch(self.medicine, qty_received=100, qty_available=0)

    def test_receiving_adds_stock_and_creates_purchase_transaction(self):
        txn = _seed_stock(self.medicine, self.batch, 100, self.user, 'GRN-A1-001')

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_available, 100)
        self.assertEqual(self.batch.quantity_received, 200)  # original 100 + 100 added

        inv = Inventory.objects.get(medicine=self.medicine)
        self.assertEqual(inv.current_stock, 100)

        self.assertEqual(txn.transaction_type, StockTransaction.TransactionType.PURCHASE)
        self.assertEqual(txn.quantity, 100)
        self.assertEqual(txn.reference_number, 'GRN-A1-001')


# ---------------------------------------------------------------------------
# A2: Sale deducts stock via remove_stock(SALE)
# ---------------------------------------------------------------------------

class A2_SaleDeductsStockTest(TestCase):
    def setUp(self):
        self.user = _make_user('a2_pharm')
        self.medicine = _make_medicine('Paracetamol', 'BAR-PARA-A2')
        self.batch = _make_batch(self.medicine, batch_suffix='A2')
        _seed_stock(self.medicine, self.batch, 100, self.user)

    def test_sale_deducts_stock_with_sale_type(self):
        txn = inventory_services.remove_stock(
            medicine=self.medicine, batch=self.batch, quantity=30,
            transaction_type=StockTransaction.TransactionType.SALE,
            performed_by=self.user, reference_number='SALE-A2-001',
        )

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_available, 70)

        inv = Inventory.objects.get(medicine=self.medicine)
        self.assertEqual(inv.current_stock, 70)

        self.assertEqual(txn.transaction_type, StockTransaction.TransactionType.SALE)
        self.assertEqual(txn.quantity, 30)


# ---------------------------------------------------------------------------
# A3: Dispensing deducts stock via remove_stock(DISPENSE)
# ---------------------------------------------------------------------------

class A3_DispensingDeductsStockTest(TestCase):
    def setUp(self):
        self.user = _make_user('a3_pharm')
        self.medicine = _make_medicine('Ibuprofen', 'BAR-IBU-A3')
        self.batch = _make_batch(self.medicine, batch_suffix='A3')
        _seed_stock(self.medicine, self.batch, 80, self.user)

    def test_dispense_deducts_stock_with_dispense_type(self):
        txn = inventory_services.remove_stock(
            medicine=self.medicine, batch=self.batch, quantity=20,
            transaction_type=StockTransaction.TransactionType.DISPENSE,
            performed_by=self.user, reference_number='DISP-A3-001',
        )

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_available, 60)
        self.assertEqual(txn.transaction_type, StockTransaction.TransactionType.DISPENSE)
        self.assertEqual(txn.quantity, 20)


# ---------------------------------------------------------------------------
# A4: Customer return restores stock via record_return / add_stock(RETURN)
# ---------------------------------------------------------------------------

class A4_ReturnRestoresStockTest(TestCase):
    def setUp(self):
        self.user = _make_user('a4_pharm')
        self.medicine = _make_medicine('Cetirizine', 'BAR-CET-A4')
        self.batch = _make_batch(self.medicine, batch_suffix='A4')
        _seed_stock(self.medicine, self.batch, 100, self.user)
        inventory_services.remove_stock(
            medicine=self.medicine, batch=self.batch, quantity=40,
            transaction_type=StockTransaction.TransactionType.DISPENSE,
            performed_by=self.user, reference_number='DISP-A4-001',
        )

    def test_return_restores_stock_with_return_type(self):
        txn = inventory_services.record_return(
            medicine=self.medicine, batch=self.batch, quantity=15,
            performed_by=self.user, reason='Patient returned unused portion',
            reference_number='RET-A4-001',
        )

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_available, 75)  # 60 + 15
        self.assertEqual(txn.transaction_type, StockTransaction.TransactionType.RETURN)
        self.assertEqual(txn.quantity, 15)

    def test_return_does_not_increment_quantity_received(self):
        before_received = MedicineBatch.objects.get(pk=self.batch.pk).quantity_received
        inventory_services.record_return(
            medicine=self.medicine, batch=self.batch, quantity=10,
            performed_by=self.user, reason='Test return',
        )
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_received, before_received,
                         "quantity_received must not change on RETURN transactions")


# ---------------------------------------------------------------------------
# A5: Damage write-off deducts via record_damage(DAMAGE)
# ---------------------------------------------------------------------------

class A5_DamageWriteOffTest(TestCase):
    def setUp(self):
        self.user = _make_user('a5_pharm')
        self.medicine = _make_medicine('Azithromycin', 'BAR-AZI-A5')
        self.batch = _make_batch(self.medicine, batch_suffix='A5')
        _seed_stock(self.medicine, self.batch, 50, self.user)

    def test_damage_deducts_stock_with_damage_type(self):
        txn = inventory_services.record_damage(
            medicine=self.medicine, batch=self.batch, quantity=5,
            performed_by=self.user, reason='Broken packaging',
        )

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_available, 45)
        self.assertEqual(txn.transaction_type, StockTransaction.TransactionType.DAMAGE)
        self.assertEqual(txn.quantity, 5)


# ---------------------------------------------------------------------------
# A6: Expiry write-off deducts via record_expiry(EXPIRY), marks batch EXPIRED
# ---------------------------------------------------------------------------

class A6_ExpiryWriteOffTest(TestCase):
    def setUp(self):
        self.user = _make_user('a6_pharm')
        self.medicine = _make_medicine('Metformin', 'BAR-MET-A6')
        self.batch = _make_batch(self.medicine, batch_suffix='A6', days_to_expiry=5)
        _seed_stock(self.medicine, self.batch, 30, self.user)

    def test_expiry_deducts_stock_marks_batch_expired(self):
        txn = inventory_services.record_expiry(
            medicine=self.medicine, batch=self.batch, quantity=30,
            performed_by=self.user, notes='Writing off expired batch',
        )

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_available, 0)
        self.assertEqual(self.batch.status, MedicineBatch.BatchStatus.EXPIRED)
        self.assertEqual(txn.transaction_type, StockTransaction.TransactionType.EXPIRY)
        self.assertEqual(txn.quantity, 30)


# ---------------------------------------------------------------------------
# A7 & A8: Stock adjustments go through adjust_stock(ADJUSTMENT)
# ---------------------------------------------------------------------------

class A7_A8_StockAdjustmentTest(TestCase):
    def setUp(self):
        self.user = _make_user('a78_pharm')
        self.medicine = _make_medicine('Atorvastatin', 'BAR-ATO-A78')
        self.batch = _make_batch(self.medicine, batch_suffix='A78')
        _seed_stock(self.medicine, self.batch, 100, self.user)

    def test_increase_adjustment_creates_adjustment_transaction(self):
        txn = inventory_services.adjust_stock(
            medicine=self.medicine, batch=self.batch,
            adjustment_type='Increase', quantity=10,
            performed_by=self.user, reason='Physical count: found 10 extra',
        )

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_available, 110)
        self.assertEqual(txn.transaction_type, StockTransaction.TransactionType.ADJUSTMENT)
        self.assertEqual(txn.quantity, 10)

    def test_decrease_adjustment_creates_adjustment_transaction(self):
        txn = inventory_services.adjust_stock(
            medicine=self.medicine, batch=self.batch,
            adjustment_type='Decrease', quantity=15,
            performed_by=self.user, reason='Physical count: 15 units missing',
        )

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_available, 85)
        self.assertEqual(txn.transaction_type, StockTransaction.TransactionType.ADJUSTMENT)
        self.assertEqual(txn.quantity, 15)


# ---------------------------------------------------------------------------
# A9: FEFO allocation — earliest expiry batch used first
# ---------------------------------------------------------------------------

class A9_FEFOAllocationTest(TestCase):
    def setUp(self):
        self.user = _make_user('a9_pharm')
        self.medicine = _make_medicine('Cefixime', 'BAR-CEF-A9')

    def test_fefo_picks_earliest_expiry_first(self):
        near = MedicineBatch.objects.create(
            medicine=self.medicine, batch_number='B-NEAR-A9',
            expiry_date=timezone.now().date() + timedelta(days=30),
            quantity_received=50, quantity_available=50,
            purchase_price=Decimal('10.00'), selling_price=Decimal('15.00'),
            status='NEAR_EXPIRY',
        )
        MedicineBatch.objects.create(
            medicine=self.medicine, batch_number='B-FAR-A9',
            expiry_date=timezone.now().date() + timedelta(days=300),
            quantity_received=50, quantity_available=50,
            purchase_price=Decimal('10.00'), selling_price=Decimal('15.00'),
            status='ACTIVE',
        )

        allocations = inventory_services.allocate_stock(self.medicine, quantity_needed=20)

        self.assertEqual(len(allocations), 1)
        self.assertEqual(allocations[0]['batch'].pk, near.pk)
        self.assertEqual(allocations[0]['quantity'], 20)

    def test_fefo_does_not_mutate_stock(self):
        MedicineBatch.objects.create(
            medicine=self.medicine, batch_number='B-FEFO-RO-A9',
            expiry_date=timezone.now().date() + timedelta(days=180),
            quantity_received=100, quantity_available=100,
            purchase_price=Decimal('10.00'), selling_price=Decimal('15.00'),
        )
        initial_count = StockTransaction.objects.count()
        inventory_services.allocate_stock(self.medicine, quantity_needed=10)
        self.assertEqual(StockTransaction.objects.count(), initial_count,
                         "allocate_stock must NOT create any StockTransaction")


# ---------------------------------------------------------------------------
# A10: Multi-batch FEFO allocation spans two batches
# ---------------------------------------------------------------------------

class A10_MultiBatchFEFOTest(TestCase):
    def setUp(self):
        self.user = _make_user('a10_pharm')
        self.medicine = _make_medicine('Omeprazole', 'BAR-OME-A10')

    def test_allocation_spans_two_batches_in_fefo_order(self):
        b1 = MedicineBatch.objects.create(
            medicine=self.medicine, batch_number='B-MULTI-1-A10',
            expiry_date=timezone.now().date() + timedelta(days=60),
            quantity_received=30, quantity_available=30,
            purchase_price=Decimal('5.00'), selling_price=Decimal('8.00'),
            status='NEAR_EXPIRY',
        )
        b2 = MedicineBatch.objects.create(
            medicine=self.medicine, batch_number='B-MULTI-2-A10',
            expiry_date=timezone.now().date() + timedelta(days=200),
            quantity_received=50, quantity_available=50,
            purchase_price=Decimal('5.00'), selling_price=Decimal('8.00'),
            status='ACTIVE',
        )

        allocations = inventory_services.allocate_stock(self.medicine, quantity_needed=50)

        self.assertEqual(len(allocations), 2)
        self.assertEqual(allocations[0]['batch'].pk, b1.pk)
        self.assertEqual(allocations[0]['quantity'], 30)
        self.assertEqual(allocations[1]['batch'].pk, b2.pk)
        self.assertEqual(allocations[1]['quantity'], 20)


# ---------------------------------------------------------------------------
# A11-A13: Negative stock prevention
# ---------------------------------------------------------------------------

class A11_A13_NegativeStockPreventionTest(TestCase):
    def setUp(self):
        self.user = _make_user('a11_pharm')
        self.medicine = _make_medicine('Pantoprazole', 'BAR-PAN-A11')
        self.batch = _make_batch(self.medicine, batch_suffix='A11')
        _seed_stock(self.medicine, self.batch, 50, self.user)

    def test_A11_remove_stock_raises_on_over_deduction(self):
        with self.assertRaises(ValidationError):
            inventory_services.remove_stock(
                medicine=self.medicine, batch=self.batch, quantity=100,
                transaction_type=StockTransaction.TransactionType.SALE,
                performed_by=self.user,
            )

    def test_A12_record_damage_raises_on_over_deduction(self):
        with self.assertRaises(ValidationError):
            inventory_services.record_damage(
                medicine=self.medicine, batch=self.batch,
                quantity=999, performed_by=self.user, reason='Test',
            )

    def test_A13_adjust_stock_decrease_raises_on_over_deduction(self):
        with self.assertRaises(ValidationError):
            inventory_services.adjust_stock(
                medicine=self.medicine, batch=self.batch,
                adjustment_type='Decrease', quantity=999,
                performed_by=self.user, reason='Physical count',
            )

    def test_batch_quantity_unchanged_after_failed_remove(self):
        before = MedicineBatch.objects.get(pk=self.batch.pk).quantity_available
        try:
            inventory_services.remove_stock(
                medicine=self.medicine, batch=self.batch, quantity=9999,
                transaction_type=StockTransaction.TransactionType.SALE,
                performed_by=self.user,
            )
        except ValidationError:
            pass
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_available, before)


# ---------------------------------------------------------------------------
# A14: Expired batch blocked from SALE / DISPENSE
# ---------------------------------------------------------------------------

class A14_ExpiredBatchBlockedTest(TestCase):
    def setUp(self):
        self.user = _make_user('a14_pharm')
        self.medicine = _make_medicine('Penicillin', 'BAR-PEN-A14')
        expiry = timezone.now().date() - timedelta(days=10)
        self.batch = MedicineBatch.objects.create(
            medicine=self.medicine, batch_number='B-EXP-A14',
            expiry_date=expiry,
            quantity_received=100, quantity_available=100,
            purchase_price=Decimal('10.00'), selling_price=Decimal('15.00'),
            status='EXPIRED',
        )
        inv, _ = Inventory.objects.get_or_create(
            medicine=self.medicine,
            defaults={'current_stock': 100, 'reorder_level': 20,
                      'safety_stock': 10, 'maximum_stock': 500},
        )
        inv.current_stock = 100
        inv.save()

    def test_expired_batch_blocked_from_sale(self):
        with self.assertRaises(ValidationError):
            inventory_services.remove_stock(
                medicine=self.medicine, batch=self.batch, quantity=10,
                transaction_type=StockTransaction.TransactionType.SALE,
                performed_by=self.user,
            )

    def test_expired_batch_blocked_from_dispense(self):
        with self.assertRaises(ValidationError):
            inventory_services.remove_stock(
                medicine=self.medicine, batch=self.batch, quantity=10,
                transaction_type=StockTransaction.TransactionType.DISPENSE,
                performed_by=self.user,
            )


# ---------------------------------------------------------------------------
# A15: Quarantined batch blocked from DISPENSE
# ---------------------------------------------------------------------------

class A15_QuarantinedBatchBlockedTest(TestCase):
    def setUp(self):
        self.user = _make_user('a15_pharm')
        self.medicine = _make_medicine('Vancomycin', 'BAR-VAN-A15')
        self.batch = MedicineBatch.objects.create(
            medicine=self.medicine, batch_number='B-QUAR-A15',
            expiry_date=timezone.now().date() + timedelta(days=200),
            quantity_received=80, quantity_available=80,
            purchase_price=Decimal('50.00'), selling_price=Decimal('70.00'),
            status='QUARANTINED',
        )
        inv, _ = Inventory.objects.get_or_create(
            medicine=self.medicine,
            defaults={'current_stock': 80, 'reorder_level': 20,
                      'safety_stock': 10, 'maximum_stock': 200},
        )

    def test_quarantined_batch_blocked_from_dispense(self):
        with self.assertRaises(ValidationError):
            inventory_services.remove_stock(
                medicine=self.medicine, batch=self.batch, quantity=10,
                transaction_type=StockTransaction.TransactionType.DISPENSE,
                performed_by=self.user,
            )


# ---------------------------------------------------------------------------
# A16: Transaction number uniqueness
# ---------------------------------------------------------------------------

class A16_TransactionNumberUniquenessTest(TestCase):
    def setUp(self):
        self.user = _make_user('a16_pharm')
        self.medicine = _make_medicine('Doxycycline', 'BAR-DOX-A16')
        self.batch = _make_batch(self.medicine, batch_suffix='A16')
        _seed_stock(self.medicine, self.batch, 200, self.user)

    def test_all_transaction_numbers_are_unique(self):
        for i in range(10):
            inventory_services.remove_stock(
                medicine=self.medicine, batch=self.batch, quantity=1,
                transaction_type=StockTransaction.TransactionType.SALE,
                performed_by=self.user,
                reference_number=f'SALE-A16-{i:03d}',
            )

        txns = StockTransaction.objects.filter(medicine=self.medicine)
        tx_numbers = [t.transaction_number for t in txns if t.transaction_number]
        self.assertEqual(len(tx_numbers), len(set(tx_numbers)),
                         "Duplicate transaction_numbers found")


# ---------------------------------------------------------------------------
# A17: quantity_received only increments on PURCHASE (not RETURN)
# ---------------------------------------------------------------------------

class A17_QuantityReceivedOnPurchaseOnlyTest(TestCase):
    def setUp(self):
        self.user = _make_user('a17_pharm')
        self.medicine = _make_medicine('Ciprofloxacin', 'BAR-CIP-A17')
        self.batch = _make_batch(self.medicine, qty_received=100, batch_suffix='A17')

    def test_purchase_increments_quantity_received(self):
        before = MedicineBatch.objects.get(pk=self.batch.pk).quantity_received
        inventory_services.add_stock(
            medicine=self.medicine, batch=self.batch, quantity=50,
            transaction_type=StockTransaction.TransactionType.PURCHASE,
            performed_by=self.user,
        )
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_received, before + 50)

    def test_return_does_not_increment_quantity_received(self):
        inventory_services.add_stock(
            medicine=self.medicine, batch=self.batch, quantity=100,
            transaction_type=StockTransaction.TransactionType.PURCHASE,
            performed_by=self.user,
        )
        self.batch.refresh_from_db()
        before_received = self.batch.quantity_received

        inventory_services.add_stock(
            medicine=self.medicine, batch=self.batch, quantity=20,
            transaction_type=StockTransaction.TransactionType.RETURN,
            performed_by=self.user,
        )
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_received, before_received,
                         "RETURN must NOT increment quantity_received")


# ---------------------------------------------------------------------------
# A18: Duplicate GRN re-processing blocked at service level
# ---------------------------------------------------------------------------

class A18_DuplicateGRNBlockedTest(TestCase):
    def setUp(self):
        self.admin = _make_user('a18_admin', User.Role.ADMIN)
        self.pharmacist = _make_user('a18_pharm', User.Role.PHARMACIST)
        cat, _ = Category.objects.get_or_create(name='A18Cat', defaults={'is_active': True})
        self.medicine = Medicine.objects.create(
            medicine_name='GRN-DupMed-A18', generic_name='GRN-DupMed-A18',
            category=cat, dosage_form='Tablet', strength='100mg',
            unit_of_measure='Strip', barcode='BAR-GRNDUP-A18',
            reorder_level=10, safety_stock=5, maximum_stock=200,
        )
        Inventory.objects.get_or_create(
            medicine=self.medicine,
            defaults={'current_stock': 0, 'reorder_level': 10,
                      'safety_stock': 5, 'maximum_stock': 200},
        )

    def test_duplicate_grn_raises_validation_error(self):
        from suppliers.models import Supplier
        from purchases.models import (
            PurchaseOrder, PurchaseOrderItem,
            GoodsReceipt, GoodsReceiptItem
        )
        from purchases import services as purchase_services

        supplier = Supplier.objects.create(
            company_name='DupTest Pharma A18', contact_person='Dup Vendor',
            email='dup18@testpharma.com', phone='9999999918',
            status=Supplier.SupplierStatus.ACTIVE,
        )
        po = PurchaseOrder.objects.create(
            po_number=PurchaseOrder.generate_po_number(), supplier=supplier,
            status=PurchaseOrder.POStatus.APPROVED,
            order_date=timezone.now().date(), created_by=self.admin,
        )
        poi = PurchaseOrderItem.objects.create(
            purchase_order=po, medicine=self.medicine,
            quantity_ordered=20, quantity_pending=20, unit_price=Decimal('10.00'),
        )
        grn = GoodsReceipt.objects.create(
            grn_number=GoodsReceipt.generate_grn_number(),
            purchase_order=po, supplier=supplier,
            receipt_date=timezone.now().date(),
            invoice_number='INV-DUP-A18', invoice_amount=Decimal('200.00'),
            status=GoodsReceipt.GRNStatus.PENDING_VERIFICATION,
            received_by=self.pharmacist,
        )
        GoodsReceiptItem.objects.create(
            goods_receipt=grn, po_item=poi, medicine=self.medicine,
            quantity_received=20, quantity_accepted=20, quantity_rejected=0,
            batch_number='BATCH-DUP-A18',
            expiry_date=timezone.now().date() + timedelta(days=365),
            unit_price=Decimal('10.00'),
        )

        purchase_services.process_grn_receipt(grn.pk, verified_by=self.pharmacist)

        with self.assertRaises(ValidationError):
            purchase_services.process_grn_receipt(grn.pk, verified_by=self.pharmacist)

        inv = Inventory.objects.get(medicine=self.medicine)
        self.assertEqual(inv.current_stock, 20)


# ---------------------------------------------------------------------------
# A19: Completed Sale delete raises ValidationError
# ---------------------------------------------------------------------------

class A19_CompletedSaleDeleteProtectionTest(TestCase):
    def setUp(self):
        self.user = _make_user('a19_pharm')

    def test_completed_sale_cannot_be_deleted(self):
        sale = Sale.objects.create(
            sale_number='SALE-A19-001',
            status=Sale.SaleStatus.COMPLETED,
            sold_by=self.user,
        )
        with self.assertRaises((ValidationError, Exception)):
            sale.delete()
        self.assertTrue(Sale.objects.filter(pk=sale.pk).exists())

    def test_partial_return_sale_cannot_be_deleted(self):
        sale = Sale.objects.create(
            sale_number='SALE-A19-002',
            status=Sale.SaleStatus.PARTIAL_RETURN,
            sold_by=self.user,
        )
        with self.assertRaises((ValidationError, Exception)):
            sale.delete()
        self.assertTrue(Sale.objects.filter(pk=sale.pk).exists())

    def test_cancelled_sale_can_be_deleted(self):
        sale = Sale.objects.create(
            sale_number='SALE-A19-003',
            status=Sale.SaleStatus.CANCELLED,
            sold_by=self.user,
        )
        sale.delete()
        self.assertFalse(Sale.objects.filter(pk=sale.pk).exists())


# ---------------------------------------------------------------------------
# A20: Completed DispensingRecord delete raises ValidationError
# ---------------------------------------------------------------------------

class A20_CompletedDispensingDeleteProtectionTest(TestCase):
    def setUp(self):
        self.user = _make_user('a20_pharm')
        self.medicine = _make_medicine('Losartan', 'BAR-LOS-A20')

    def test_confirmed_dispensing_record_cannot_be_deleted(self):
        rec = DispensingRecord.objects.create(
            dispensing_id='DISP-A20-001',
            medicine=self.medicine,
            patient_name='John Doe',
            patient_id='PAT-101',
            pharmacist=self.user,
            total_quantity=10,
            regulatory_category='NORMAL',
            status=DispensingRecord.Status.CONFIRMED,
        )
        with self.assertRaises((ValidationError, Exception)):
            rec.delete()
        self.assertTrue(DispensingRecord.objects.filter(pk=rec.pk).exists())

    def test_completed_dispensing_record_cannot_be_deleted(self):
        rec = DispensingRecord.objects.create(
            dispensing_id='DISP-A20-002',
            medicine=self.medicine,
            patient_name='Jane Doe',
            patient_id='PAT-102',
            pharmacist=self.user,
            total_quantity=10,
            regulatory_category='NORMAL',
            status=DispensingRecord.Status.COMPLETED,
        )
        with self.assertRaises((ValidationError, Exception)):
            rec.delete()
        self.assertTrue(DispensingRecord.objects.filter(pk=rec.pk).exists())



# ---------------------------------------------------------------------------
# A21: Cancelled Sale excluded from net consumption
# ---------------------------------------------------------------------------

class A21_CancelledSaleExcludedFromConsumptionTest(TestCase):
    def setUp(self):
        self.user = _make_user('a21_pharm')
        self.medicine = _make_medicine('Enalapril', 'BAR-ENA-A21')
        self.batch = _make_batch(self.medicine, batch_suffix='A21')
        _seed_stock(self.medicine, self.batch, 200, self.user)

    def test_cancelled_sale_not_counted_in_net_consumption(self):
        start = timezone.now() - timedelta(hours=1)

        inventory_services.remove_stock(
            medicine=self.medicine, batch=self.batch, quantity=30,
            transaction_type=StockTransaction.TransactionType.SALE,
            performed_by=self.user, reference_number='SALE-A21-NORMAL',
        )

        cancelled_sale = Sale.objects.create(
            sale_number='SALE-A21-CANCEL',
            status=Sale.SaleStatus.CANCELLED,
            sold_by=self.user,
        )
        inv = Inventory.objects.get(medicine=self.medicine)
        StockTransaction.objects.create(
            inventory=inv, medicine=self.medicine, batch=self.batch,
            transaction_type=StockTransaction.TransactionType.SALE,
            quantity=50, previous_stock=170, new_stock=120,
            reference_number=cancelled_sale.sale_number,
            performed_by=self.user,
        )

        end = timezone.now() + timedelta(hours=1)
        result = calculate_net_consumption_in_range(self.medicine, start, end)

        self.assertEqual(result['net_consumption'], 30,
                         "Cancelled sale (50 units) must NOT appear in net consumption")
        self.assertEqual(result['gross_consumption'], 30)


# ---------------------------------------------------------------------------
# A22: Return reduces net consumption
# ---------------------------------------------------------------------------

class A22_ReturnReducesNetConsumptionTest(TestCase):
    def setUp(self):
        self.user = _make_user('a22_pharm')
        self.medicine = _make_medicine('Amlodipine', 'BAR-AML-A22')
        self.batch = _make_batch(self.medicine, batch_suffix='A22')
        _seed_stock(self.medicine, self.batch, 100, self.user)

    def test_return_reduces_net_consumption(self):
        start = timezone.now() - timedelta(hours=1)

        inventory_services.remove_stock(
            medicine=self.medicine, batch=self.batch, quantity=40,
            transaction_type=StockTransaction.TransactionType.DISPENSE,
            performed_by=self.user, reference_number='DISP-A22-001',
        )

        inventory_services.record_return(
            medicine=self.medicine, batch=self.batch, quantity=10,
            performed_by=self.user, reason='Unused strips returned',
            reference_number='DISP-A22-001',
        )

        end = timezone.now() + timedelta(hours=1)
        result = calculate_net_consumption_in_range(self.medicine, start, end)

        self.assertEqual(result['gross_consumption'], 40)
        self.assertEqual(result['return_quantity'], 10)
        self.assertEqual(result['net_consumption'], 30)


# ---------------------------------------------------------------------------
# A23 & A24: BatchEditForm does NOT expose quantity_available or quantity_received
# ---------------------------------------------------------------------------

class A23_A24_BatchEditFormFieldsTest(TestCase):
    def test_batch_edit_form_excludes_quantity_available(self):
        form = BatchEditForm()
        self.assertNotIn('quantity_available', form.fields,
                         "quantity_available must NOT be in BatchEditForm.fields")

    def test_batch_edit_form_excludes_quantity_received(self):
        form = BatchEditForm()
        self.assertNotIn('quantity_received', form.fields,
                         "quantity_received must NOT be in BatchEditForm.fields")

    def test_batch_edit_form_allows_metadata_edit(self):
        form = BatchEditForm()
        self.assertIn('purchase_price', form.fields)
        self.assertIn('selling_price', form.fields)
        self.assertIn('status', form.fields)
        self.assertIn('storage_condition', form.fields)
        self.assertIn('expiry_date', form.fields)


# ---------------------------------------------------------------------------
# A25: record_return creates RETURN type (not PURCHASE)
# ---------------------------------------------------------------------------

class A25_RecordReturnCreatesReturnTypeTest(TestCase):
    def setUp(self):
        self.user = _make_user('a25_pharm')
        self.medicine = _make_medicine('Furosemide', 'BAR-FUR-A25')
        self.batch = _make_batch(self.medicine, batch_suffix='A25')
        _seed_stock(self.medicine, self.batch, 60, self.user)
        inventory_services.remove_stock(
            medicine=self.medicine, batch=self.batch, quantity=20,
            transaction_type=StockTransaction.TransactionType.SALE,
            performed_by=self.user,
        )

    def test_record_return_creates_return_transaction_not_purchase(self):
        before_purchase_count = StockTransaction.objects.filter(
            medicine=self.medicine,
            transaction_type=StockTransaction.TransactionType.PURCHASE,
        ).count()

        txn = inventory_services.record_return(
            medicine=self.medicine, batch=self.batch, quantity=10,
            performed_by=self.user, reason='Customer return',
        )

        self.assertEqual(txn.transaction_type, StockTransaction.TransactionType.RETURN)

        after_purchase_count = StockTransaction.objects.filter(
            medicine=self.medicine,
            transaction_type=StockTransaction.TransactionType.PURCHASE,
        ).count()
        self.assertEqual(before_purchase_count, after_purchase_count,
                         "record_return must NOT create any PURCHASE transaction")


# ---------------------------------------------------------------------------
# A26: Transaction rollback — remove_stock is atomic
# ---------------------------------------------------------------------------

class A26_AtomicTransactionRollbackTest(TestCase):
    def setUp(self):
        self.user = _make_user('a26_pharm')
        self.medicine = _make_medicine('Spironolactone', 'BAR-SPI-A26')
        self.batch = _make_batch(self.medicine, batch_suffix='A26')
        _seed_stock(self.medicine, self.batch, 50, self.user)

    def test_failed_remove_leaves_no_orphan_transaction(self):
        before_count = StockTransaction.objects.count()
        try:
            inventory_services.remove_stock(
                medicine=self.medicine, batch=self.batch, quantity=9999,
                transaction_type=StockTransaction.TransactionType.SALE,
                performed_by=self.user,
            )
        except ValidationError:
            pass

        after_count = StockTransaction.objects.count()
        self.assertEqual(before_count, after_count,
                         "Failed remove_stock must not leave any orphan StockTransaction")


# ---------------------------------------------------------------------------
# A27: Inventory recalculated correctly after multi-step workflow
# ---------------------------------------------------------------------------

class A27_InventoryRecalculationAccuracyTest(TestCase):
    def setUp(self):
        self.user = _make_user('a27_pharm')
        self.medicine = _make_medicine('Hydralazine', 'BAR-HYD-A27')
        self.batch = _make_batch(self.medicine, batch_suffix='A27')

    def test_inventory_accurate_after_complex_workflow(self):
        """
        1. Receive 100 (PURCHASE)
        2. Sell 30 (SALE)      -> 70 remaining
        3. Return 10 (RETURN)  -> 80 remaining
        4. Damage 5 (DAMAGE)   -> 75 remaining
        5. Adjust +5 (ADJ)     -> 80 remaining
        """
        _seed_stock(self.medicine, self.batch, 100, self.user)

        inventory_services.remove_stock(
            medicine=self.medicine, batch=self.batch, quantity=30,
            transaction_type=StockTransaction.TransactionType.SALE,
            performed_by=self.user,
        )
        inventory_services.record_return(
            medicine=self.medicine, batch=self.batch, quantity=10,
            performed_by=self.user, reason='Return step',
        )
        inventory_services.record_damage(
            medicine=self.medicine, batch=self.batch, quantity=5,
            performed_by=self.user, reason='Damaged step',
        )
        inventory_services.adjust_stock(
            medicine=self.medicine, batch=self.batch,
            adjustment_type='Increase', quantity=5,
            performed_by=self.user, reason='Count correction',
        )

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_available, 80)

        inv = Inventory.objects.get(medicine=self.medicine)
        self.assertEqual(inv.current_stock, 80)

        # Exactly 5 transactions: PURCHASE + SALE + RETURN + DAMAGE + ADJUSTMENT
        txn_count = StockTransaction.objects.filter(medicine=self.medicine).count()
        self.assertEqual(txn_count, 5)

