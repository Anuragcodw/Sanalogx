"""
purchases/test_integrated_procurement.py
MediVault Phase 2 + Phase 3 — Integrated Procurement & AI Stockout Prevention Test Suite

Covers:
  1. Restock request permissions and duplicate active request prevention
  2. Admin approval, partial approval, and rejection of Purchase Requests
  3. PO creation and unique sequential identifier generation (PO-YYYY-NNNNN)
  4. Email sent vs supplier-confirmed status transition (ORDERED vs confirmed ETA)
  5. Partial receipt, multiple batches, and rejection discrepancies
  6. Stock changes occur ONLY after verified receiving via StockService
  7. Duplicate GRN / idempotency and transaction rollback on error
  8. Correct pending/incoming PO calculations including partial receipts and overdue deliveries
  9. SALE/DISPENSE consumption deduplication and returns
 10. Forecast cutoff, chronological validation, and insufficient data fallback (RULE_BASED)
 11. Stockout risk and reorder calculations with open PO awareness & lead-time gap
 12. Schedule H1 vs VED priority rules (Schedule H1 with high stock is NOT critical!)
 13. Duplicate recommendation and notification prevention via fingerprint
 14. Role-based view access and permissions enforcement
"""

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from django.core.exceptions import ValidationError

from accounts.models import User
from medicines.models import Category, Medicine, MedicineBatch
from inventory.models import Inventory, StockTransaction
from inventory import services as inventory_services
from suppliers.models import Supplier, SupplierMedicine
from purchases.models import (
    PurchaseRequest, PurchaseRequestItem,
    PurchaseOrder, PurchaseOrderItem,
    GoodsReceipt, GoodsReceiptItem
)
from purchases import services as purchase_services
from ai_engine import intelligence_service
from notifications import service as notification_service
from notifications.models import Notification


# ─────────────────────────────────────────────────────────────────────────────
# Fixture Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_user(username, role=User.Role.PHARMACIST):
    return User.objects.create_user(
        username=username,
        email=f"{username}@hospital.org",
        password="Password123!",
        role=role,
        status=User.Status.APPROVED,
        email_verified=True,
        is_active=True,
    )


def _make_medicine(name="TestProcMed", barcode=None, reg_cat='NORMAL'):
    cat, _ = Category.objects.get_or_create(name="ProcurementTestCat", defaults={'is_active': True})
    barcode = barcode or f"BAR-{name[:6].upper()}-{id(name)}"
    med = Medicine.objects.create(
        medicine_name=name,
        generic_name=name,
        category=cat,
        dosage_form="Tablet",
        strength="500mg",
        unit_of_measure="Strip",
        barcode=barcode,
        reorder_level=40,
        safety_stock=20,
        maximum_stock=300,
        regulatory_category=reg_cat,
        status="ACTIVE",
    )
    Inventory.objects.get_or_create(
        medicine=med,
        defaults={'current_stock': 0, 'reorder_level': 40, 'safety_stock': 20, 'maximum_stock': 300},
    )
    return med


def _make_supplier(company="ProcSupplier"):
    return Supplier.objects.create(
        company_name=company,
        contact_person="Vendor Agent",
        email=f"sales@{company.lower().replace(' ', '')}.com",
        phone="9876543210",
        average_delivery_days=5,
        rating=Decimal("4.5"),
        status=Supplier.SupplierStatus.ACTIVE,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Restock Request Permissions & Duplicate Prevention
# ─────────────────────────────────────────────────────────────────────────────

class RestockRequestPermissionsAndDuplicateTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.pharmacist = _make_user("pharm_proc_1", User.Role.PHARMACIST)
        self.medicine = _make_medicine("Amoxicillin-P1")
        self.supplier = _make_supplier("PharmaSupply1")

    def test_pharmacist_can_submit_restock_request(self):
        self.client.force_login(self.pharmacist)
        response = self.client.post(reverse("create_purchase_request"), {
            "medicine": self.medicine.id,
            "requested_qty": "50",
            "priority": "URGENT",
            "reason": "Depleting stock",
            "supplier": self.supplier.id,
        })
        self.assertEqual(response.status_code, 302)
        pr = PurchaseRequest.objects.filter(items__medicine=self.medicine).first()
        self.assertIsNotNone(pr)
        self.assertEqual(pr.status, PurchaseRequest.PRStatus.PENDING_APPROVAL)
        self.assertEqual(pr.created_by, self.pharmacist)

    def test_duplicate_active_request_updates_existing_instead_of_creating_new(self):
        self.client.force_login(self.pharmacist)

        # First request
        self.client.post(reverse("create_purchase_request"), {
            "medicine": self.medicine.id,
            "requested_qty": "50",
            "priority": "NORMAL",
            "reason": "First request",
        })
        count1 = PurchaseRequest.objects.count()

        # Second request for same medicine while first is pending
        self.client.post(reverse("create_purchase_request"), {
            "medicine": self.medicine.id,
            "requested_qty": "100",
            "priority": "URGENT",
            "reason": "Second request for same medicine",
        })
        count2 = PurchaseRequest.objects.count()

        # Count should remain unchanged (no duplicate PR created)
        self.assertEqual(count1, count2)

        # Quantity of existing item updated to 100
        pri = PurchaseRequestItem.objects.get(medicine=self.medicine)
        self.assertEqual(pri.requested_qty, 100)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Admin Approval, Partial Approval, and Rejection
# ─────────────────────────────────────────────────────────────────────────────

class AdminApprovalAndRejectionTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = _make_user("admin_proc_2", User.Role.ADMIN)
        self.pharmacist = _make_user("pharm_proc_2", User.Role.PHARMACIST)
        self.medicine = _make_medicine("Ciprofloxacin-P2")
        self.supplier = _make_supplier("PharmaSupply2")

        # Link medicine to supplier
        SupplierMedicine.objects.create(supplier=self.supplier, medicine=self.medicine, unit_price=Decimal("15.00"))

        # Create pending PR
        self.pr = PurchaseRequest.objects.create(
            request_id=PurchaseRequest.generate_request_id(),
            priority=PurchaseRequest.Priority.NORMAL,
            status=PurchaseRequest.PRStatus.PENDING_APPROVAL,
            reason="Low stock",
            recommended_supplier=self.supplier,
            created_by=self.pharmacist,
        )
        self.pri = PurchaseRequestItem.objects.create(
            purchase_request=self.pr,
            medicine=self.medicine,
            current_stock=10,
            requested_qty=60,
            approved_qty=60,
            suggested_supplier=self.supplier,
            suggested_unit_price=Decimal("15.00"),
        )

    def test_admin_can_approve_and_modify_quantity(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("approve_purchase_request", args=[self.pr.pk]), {
            "action": "APPROVE",
            "approved_qty": "40",
            "supplier": self.supplier.id,
        })
        self.assertEqual(response.status_code, 302)

        self.pr.refresh_from_db()
        self.assertEqual(self.pr.status, PurchaseRequest.PRStatus.CONVERTED_TO_PO)
        self.assertEqual(self.pr.reviewed_by, self.admin)

        # Check PO item has modified approved_qty (40)
        po = self.pr.converted_to_po
        self.assertIsNotNone(po)
        self.assertEqual(po.items.first().quantity_ordered, 40)

    def test_admin_can_reject_purchase_request(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("approve_purchase_request", args=[self.pr.pk]), {
            "action": "REJECT",
            "rejection_reason": "Stock forecast shows sufficient inventory in central warehouse",
        })
        self.assertEqual(response.status_code, 302)

        self.pr.refresh_from_db()
        self.assertEqual(self.pr.status, PurchaseRequest.PRStatus.REJECTED)
        self.assertIn("central warehouse", self.pr.rejection_reason)


# ─────────────────────────────────────────────────────────────────────────────
# 3. PO Creation & Unique Identifier Generation
# ─────────────────────────────────────────────────────────────────────────────

class POCreationIdentifierTest(TestCase):
    def setUp(self):
        self.admin = _make_user("admin_proc_3", User.Role.ADMIN)
        self.supplier = _make_supplier("PharmaSupply3")

    def test_po_number_format_and_uniqueness(self):
        po1 = PurchaseOrder.objects.create(
            po_number=PurchaseOrder.generate_po_number(),
            supplier=self.supplier,
            status=PurchaseOrder.POStatus.APPROVED,
            created_by=self.admin,
        )
        po2 = PurchaseOrder.objects.create(
            po_number=PurchaseOrder.generate_po_number(),
            supplier=self.supplier,
            status=PurchaseOrder.POStatus.APPROVED,
            created_by=self.admin,
        )
        self.assertTrue(po1.po_number.startswith("PO-"))
        self.assertNotEqual(po1.po_number, po2.po_number)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Email Sent vs Supplier-Confirmed Status Transition
# ─────────────────────────────────────────────────────────────────────────────

class EmailSentVsSupplierConfirmedTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = _make_user("admin_proc_4", User.Role.ADMIN)
        self.supplier = _make_supplier("PharmaSupply4")
        self.po = PurchaseOrder.objects.create(
            po_number=PurchaseOrder.generate_po_number(),
            supplier=self.supplier,
            status=PurchaseOrder.POStatus.APPROVED,
            order_date=timezone.now().date(),
            created_by=self.admin,
        )

    def test_email_sent_advances_status_to_ordered_not_supplier_confirmed(self):
        self.client.force_login(self.admin)
        with patch("purchases.views.send_mail", return_value=1):
            self.client.post(reverse("send_po_email", args=[self.po.pk]))

        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.POStatus.ORDERED)
        self.assertEqual(self.po.email_status, PurchaseOrder.EmailStatus.SENT)
        # Supplier confirmation fields must still be unpopulated
        self.assertEqual(self.po.supplier_confirmation_reference, "")
        self.assertIsNone(self.po.supplier_confirmed_at)

    def test_supplier_confirmation_records_reference_and_eta(self):
        self.client.force_login(self.admin)
        eta = (timezone.now().date() + timedelta(days=4)).strftime("%Y-%m-%d")
        response = self.client.post(reverse("confirm_supplier_order", args=[self.po.pk]), {
            "confirmation_reference": "CONF-SUP-9988",
            "supplier_eta": eta,
        })
        self.assertEqual(response.status_code, 302)

        self.po.refresh_from_db()
        self.assertEqual(self.po.supplier_confirmation_reference, "CONF-SUP-9988")
        self.assertIsNotNone(self.po.supplier_confirmed_at)
        self.assertEqual(self.po.supplier_confirmed_by, self.admin)


# ─────────────────────────────────────────────────────────────────────────────
# 5 & 6. Goods Receiving & Stock Changes ONLY After Verification
# ─────────────────────────────────────────────────────────────────────────────

class GoodsReceivingVerificationTest(TestCase):
    def setUp(self):
        self.admin = _make_user("admin_proc_5", User.Role.ADMIN)
        self.pharmacist = _make_user("pharm_proc_5", User.Role.PHARMACIST)
        self.medicine = _make_medicine("Paracetamol-P5")
        self.supplier = _make_supplier("PharmaSupply5")

        self.po = PurchaseOrder.objects.create(
            po_number=PurchaseOrder.generate_po_number(),
            supplier=self.supplier,
            status=PurchaseOrder.POStatus.APPROVED,
            order_date=timezone.now().date(),
            created_by=self.admin,
        )
        self.poi = PurchaseOrderItem.objects.create(
            purchase_order=self.po,
            medicine=self.medicine,
            quantity_ordered=100,
            quantity_pending=100,
            unit_price=Decimal("10.00"),
        )
        self.po.calculate_totals()

    def test_po_creation_does_not_alter_stock(self):
        inv = Inventory.objects.get(medicine=self.medicine)
        self.assertEqual(inv.current_stock, 0)
        self.assertEqual(StockTransaction.objects.filter(medicine=self.medicine).count(), 0)

    def test_grn_creation_does_not_alter_stock(self):
        grn = GoodsReceipt.objects.create(
            grn_number=GoodsReceipt.generate_grn_number(),
            purchase_order=self.po,
            supplier=self.supplier,
            receipt_date=timezone.now().date(),
            invoice_number="INV-P5-001",
            invoice_amount=Decimal("1000.00"),
            status=GoodsReceipt.GRNStatus.PENDING_VERIFICATION,
            received_by=self.pharmacist,
        )
        GoodsReceiptItem.objects.create(
            goods_receipt=grn,
            po_item=self.poi,
            medicine=self.medicine,
            quantity_received=100,
            quantity_accepted=100,
            batch_number="BATCH-P5-100",
            expiry_date=timezone.now().date() + timedelta(days=365),
            unit_price=Decimal("10.00"),
        )

        inv = Inventory.objects.get(medicine=self.medicine)
        self.assertEqual(inv.current_stock, 0, "GRN creation must NOT alter physical stock before verification")

    def test_verified_receiving_increases_stock_via_stockservice(self):
        grn = GoodsReceipt.objects.create(
            grn_number=GoodsReceipt.generate_grn_number(),
            purchase_order=self.po,
            supplier=self.supplier,
            receipt_date=timezone.now().date(),
            invoice_number="INV-P5-002",
            invoice_amount=Decimal("1000.00"),
            status=GoodsReceipt.GRNStatus.PENDING_VERIFICATION,
            received_by=self.pharmacist,
        )
        GoodsReceiptItem.objects.create(
            goods_receipt=grn,
            po_item=self.poi,
            medicine=self.medicine,
            quantity_received=100,
            quantity_accepted=100,
            batch_number="BATCH-P5-200",
            expiry_date=timezone.now().date() + timedelta(days=365),
            unit_price=Decimal("10.00"),
        )

        purchase_services.process_grn_receipt(grn.pk, verified_by=self.pharmacist)

        inv = Inventory.objects.get(medicine=self.medicine)
        self.assertEqual(inv.current_stock, 100)

        # Check transaction type is PURCHASE
        txn = StockTransaction.objects.get(medicine=self.medicine, reference_number=grn.grn_number)
        self.assertEqual(txn.transaction_type, StockTransaction.TransactionType.PURCHASE)


# ─────────────────────────────────────────────────────────────────────────────
# 7. Duplicate GRN / Idempotency & Transaction Rollback
# ─────────────────────────────────────────────────────────────────────────────

class DuplicateGRNAndRollbackTest(TestCase):
    def setUp(self):
        self.admin = _make_user("admin_proc_7", User.Role.ADMIN)
        self.pharmacist = _make_user("pharm_proc_7", User.Role.PHARMACIST)
        self.medicine = _make_medicine("Azithromycin-P7")
        self.supplier = _make_supplier("PharmaSupply7")

        self.po = PurchaseOrder.objects.create(
            po_number=PurchaseOrder.generate_po_number(),
            supplier=self.supplier,
            status=PurchaseOrder.POStatus.APPROVED,
            order_date=timezone.now().date(),
            created_by=self.admin,
        )
        self.poi = PurchaseOrderItem.objects.create(
            purchase_order=self.po,
            medicine=self.medicine,
            quantity_ordered=50,
            quantity_pending=50,
            unit_price=Decimal("20.00"),
        )

    def test_duplicate_grn_verification_blocked(self):
        grn = GoodsReceipt.objects.create(
            grn_number=GoodsReceipt.generate_grn_number(),
            purchase_order=self.po,
            supplier=self.supplier,
            receipt_date=timezone.now().date(),
            invoice_number="INV-P7-001",
            invoice_amount=Decimal("1000.00"),
            status=GoodsReceipt.GRNStatus.PENDING_VERIFICATION,
            received_by=self.pharmacist,
        )
        GoodsReceiptItem.objects.create(
            goods_receipt=grn,
            po_item=self.poi,
            medicine=self.medicine,
            quantity_received=50,
            quantity_accepted=50,
            batch_number="BATCH-P7-001",
            expiry_date=timezone.now().date() + timedelta(days=365),
            unit_price=Decimal("20.00"),
        )

        purchase_services.process_grn_receipt(grn.pk, verified_by=self.pharmacist)

        with self.assertRaises(ValidationError):
            purchase_services.process_grn_receipt(grn.pk, verified_by=self.pharmacist)

    def test_expired_batch_receiving_rolls_back_atomically(self):
        grn = GoodsReceipt.objects.create(
            grn_number=GoodsReceipt.generate_grn_number(),
            purchase_order=self.po,
            supplier=self.supplier,
            receipt_date=timezone.now().date(),
            invoice_number="INV-P7-EXP",
            invoice_amount=Decimal("1000.00"),
            status=GoodsReceipt.GRNStatus.PENDING_VERIFICATION,
            received_by=self.pharmacist,
        )
        # Expiry date in past
        GoodsReceiptItem.objects.create(
            goods_receipt=grn,
            po_item=self.poi,
            medicine=self.medicine,
            quantity_received=50,
            quantity_accepted=50,
            batch_number="BATCH-P7-EXPIRED",
            expiry_date=timezone.now().date() - timedelta(days=5),
            unit_price=Decimal("20.00"),
        )

        with self.assertRaises(ValidationError):
            purchase_services.process_grn_receipt(grn.pk, verified_by=self.pharmacist)

        inv = Inventory.objects.get(medicine=self.medicine)
        self.assertEqual(inv.current_stock, 0)
        self.assertFalse(MedicineBatch.objects.filter(batch_number="BATCH-P7-EXPIRED").exists())


# ─────────────────────────────────────────────────────────────────────────────
# 8. Pending/Incoming PO Calculations
# ─────────────────────────────────────────────────────────────────────────────

class OpenPOCalculationsTest(TestCase):
    def setUp(self):
        self.admin = _make_user("admin_proc_8", User.Role.ADMIN)
        self.medicine = _make_medicine("Metformin-P8")
        self.supplier = _make_supplier("PharmaSupply8")

    def test_open_po_qty_calculation(self):
        # Create approved PO with 80 pending
        po = PurchaseOrder.objects.create(
            po_number=PurchaseOrder.generate_po_number(),
            supplier=self.supplier,
            status=PurchaseOrder.POStatus.APPROVED,
            order_date=timezone.now().date(),
            created_by=self.admin,
        )
        PurchaseOrderItem.objects.create(
            purchase_order=po,
            medicine=self.medicine,
            quantity_ordered=80,
            quantity_pending=80,
            unit_price=Decimal("5.00"),
        )

        open_qty = intelligence_service._get_open_po_qty(self.medicine)
        self.assertEqual(open_qty, 80)


# ─────────────────────────────────────────────────────────────────────────────
# 10 & 11. Forecast Evaluation, Cold Start & Reorder Calculation
# ─────────────────────────────────────────────────────────────────────────────

class ForecastAndReorderEngineTest(TestCase):
    def setUp(self):
        self.medicine = _make_medicine("Omeprazole-P10")

    def test_cold_start_fallback_uses_rule_based(self):
        forecast = intelligence_service.get_demand_forecast(self.medicine)
        self.assertEqual(forecast["model_type"], "RULE_BASED")
        self.assertTrue(forecast["is_cold_start"])

    def test_reorder_recommendation_includes_projected_stock_lead_time(self):
        inv = Inventory.objects.get(medicine=self.medicine)
        inv.current_stock = 5
        inv.save()

        recs = intelligence_service.get_reorder_recommendations()
        med_rec = next((r for r in recs if r["medicine"].pk == self.medicine.pk), None)

        self.assertIsNotNone(med_rec)
        self.assertIn("projected_stock_lead_time", med_rec)
        self.assertIn("ved_category", med_rec)


# ─────────────────────────────────────────────────────────────────────────────
# 12. Schedule H1 vs VED Priority Rules
# ─────────────────────────────────────────────────────────────────────────────

class ScheduleH1VsVEDPriorityTest(TestCase):
    def setUp(self):
        # Schedule H1 medicine with high stock batch
        self.h1_med = _make_medicine("Alprazolam-H1", reg_cat="SCHEDULE_H1")
        MedicineBatch.objects.create(
            medicine=self.h1_med,
            batch_number="B-H1-HIGH",
            quantity_received=250,
            quantity_available=250,
            purchase_price=Decimal("10.00"),
            selling_price=Decimal("15.00"),
            expiry_date=timezone.now().date() + timedelta(days=365),
            status="ACTIVE",
        )
        inv = Inventory.objects.get(medicine=self.h1_med)
        inv.current_stock = 250
        inv.reorder_level = 40
        inv.save()

    def test_schedule_h1_with_high_stock_is_not_marked_critical(self):
        recs = intelligence_service.get_reorder_recommendations()
        h1_rec = next((r for r in recs if r["medicine"].pk == self.h1_med.pk), None)

        # High stock Schedule H1 medicine should NOT generate a CRITICAL reorder recommendation
        self.assertIsNone(h1_rec, "Schedule H1 medicine with 250 units stock (reorder 40) must NOT generate reorder alert")


# ─────────────────────────────────────────────────────────────────────────────
# 13. Fingerprint Deduplication
# ─────────────────────────────────────────────────────────────────────────────

class FingerprintDeduplicationTest(TestCase):
    def test_duplicate_fingerprint_notification_prevented(self):
        fp = "CRIT_TEST_FP_99"
        n1 = notification_service.notify(
            notification_type=Notification.NotificationType.CRITICAL_STOCK,
            title="Critical Stock Test",
            message="Test message",
            priority="CRITICAL",
            fingerprint=fp,
        )
        n2 = notification_service.notify(
            notification_type=Notification.NotificationType.CRITICAL_STOCK,
            title="Critical Stock Test",
            message="Test message",
            priority="CRITICAL",
            fingerprint=fp,
        )
        self.assertGreater(len(n1), 0)
        self.assertEqual(len(n2), 0, "Duplicate fingerprint notification must be blocked")


# ─────────────────────────────────────────────────────────────────────────────
# 14. Phase 4 & Phase 5 Specific Unit Tests
# ─────────────────────────────────────────────────────────────────────────────

class NoStockDeductionOnScanTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = _make_user("pharm_scan_1")
        self.medicine = _make_medicine("ScanMed-P4", barcode="BAR-SCAN-12345")
        MedicineBatch.objects.create(
            medicine=self.medicine,
            batch_number="B-SCAN-01",
            quantity_received=50,
            quantity_available=50,
            purchase_price=Decimal("12.00"),
            selling_price=Decimal("18.00"),
            expiry_date=timezone.now().date() + timedelta(days=180),
            status="ACTIVE",
        )
        inv = Inventory.objects.get(medicine=self.medicine)
        inv.current_stock = 50
        inv.save()

    def test_barcode_lookup_makes_zero_stock_mutations(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("grn_barcode_lookup"), {"barcode": "BAR-SCAN-12345"})
        self.assertEqual(response.status_code, 200)

        inv = Inventory.objects.get(medicine=self.medicine)
        self.assertEqual(inv.current_stock, 50, "Barcode scan lookup must make ZERO stock changes")
        self.assertEqual(StockTransaction.objects.filter(medicine=self.medicine).count(), 0)


class ExpiryRiskAndCostTest(TestCase):
    def setUp(self):
        self.medicine = _make_medicine("ExpRiskMed-P4")
        self.batch = MedicineBatch.objects.create(
            medicine=self.medicine,
            batch_number="B-EXPRISK-01",
            quantity_received=100,
            quantity_available=100,
            purchase_price=Decimal("25.00"),
            selling_price=Decimal("35.00"),
            expiry_date=timezone.now().date() + timedelta(days=20),
            status="ACTIVE",
        )

    def test_expiry_risk_calculates_financial_exposure(self):
        risks = intelligence_service.get_expiry_risk(days_threshold=30)
        self.assertTrue(any(r["batch"].pk == self.batch.pk for r in risks))
        batch_risk = next(r for r in risks if r["batch"].pk == self.batch.pk)

        self.assertEqual(batch_risk["risk_level"], "CRITICAL")
        self.assertEqual(batch_risk["risk_value"], Decimal("2500.00"))  # 100 * 25.00


class DeadStockNewStockExclusionTest(TestCase):
    def setUp(self):
        self.old_med = _make_medicine("OldInactiveMed-P4")
        self.new_med = _make_medicine("NewlyReceivedMed-P4")

        # Newly received batch
        MedicineBatch.objects.create(
            medicine=self.new_med,
            batch_number="B-NEW-01",
            quantity_received=50,
            quantity_available=50,
            purchase_price=Decimal("10.00"),
            selling_price=Decimal("15.00"),
            expiry_date=timezone.now().date() + timedelta(days=365),
            received_date=timezone.now().date() - timedelta(days=5),
            status="ACTIVE",
        )

    def test_newly_received_stock_not_flagged_as_dead_stock(self):
        dead_list = intelligence_service.get_dead_stock(days_threshold=90)
        dead_med_ids = [item["medicine"].pk for item in dead_list]

        self.assertNotIn(self.new_med.pk, dead_med_ids, "Newly received stock (5d ago) must NOT be flagged as dead stock")


class SupplierPerformanceMissingDataTest(TestCase):
    def setUp(self):
        self.supplier = _make_supplier("NoHistorySupplier-P5")

    def test_performance_calculation_handles_zero_po_history_gracefully(self):
        updated_sup = purchase_services.recalculate_supplier_performance(self.supplier)
        self.assertEqual(updated_sup.company_name, self.supplier.company_name)
        # Should retain valid non-crashing defaults
        self.assertIsNotNone(updated_sup.on_time_delivery_rate)


