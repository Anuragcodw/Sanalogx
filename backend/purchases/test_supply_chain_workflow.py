"""
purchases/test_supply_chain_workflow.py
End-to-End Hospital Pharmacy Supply Chain & Stock Optimization Test Suite.
Verifies all 22 acceptance criteria and workflows across procurement, receiving,
inventory, AI reordering, delivery tracking, and role-based permissions.
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
from inventory.models import Inventory, StockTransaction, StockAlert
from inventory import services as inventory_services
from suppliers.models import Supplier, SupplierMedicine
from purchases.models import (
    PurchaseRequest, PurchaseRequestItem,
    PurchaseOrder, PurchaseOrderItem,
    GoodsReceipt, GoodsReceiptItem
)
from purchases import services as purchase_services
from ai_engine.intelligence_service import get_reorder_recommendations


class SupplyChainWorkflowTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Users
        self.admin = User.objects.create_user(
            username='admin_sc',
            email='admin_sc@hospital.org',
            password='Password123!',
            role=User.Role.ADMIN,
            status=User.Status.APPROVED,
            email_verified=True,
            is_active=True
        )
        self.pharmacist = User.objects.create_user(
            username='pharm_sc',
            email='pharm_sc@hospital.org',
            password='Password123!',
            role=User.Role.PHARMACIST,
            status=User.Status.APPROVED,
            email_verified=True,
            is_active=True
        )

        # Category and Supplier
        self.category = Category.objects.create(name='Antibiotics', is_active=True)
        self.supplier = Supplier.objects.create(
            company_name='MedSupply Corp',
            contact_person='John Vendor',
            email='orders@medsupplycorp.com',
            phone='9876543210',
            average_delivery_days=5,
            rating=Decimal('4.5'),
            status=Supplier.SupplierStatus.ACTIVE
        )

        # Medicine 1
        self.medicine = Medicine.objects.create(
            medicine_name='Ceftriaxone 1g',
            generic_name='Ceftriaxone',
            category=self.category,
            dosage_form='Injection',
            strength='1g',
            unit_of_measure='Vial',
            barcode='BAR-CEFTRIAXONE-1G',
            reorder_level=40,
            safety_stock=20,
            maximum_stock=300,
            status='ACTIVE'
        )
        # Ensure inventory record exists
        self.inv, _ = Inventory.objects.get_or_create(
            medicine=self.medicine,
            defaults={
                'current_stock': 0,
                'reorder_level': 40,
                'safety_stock': 20,
                'maximum_stock': 300,
            }
        )

        # Supplier catalogue link
        SupplierMedicine.objects.create(
            supplier=self.supplier,
            medicine=self.medicine,
            unit_price=Decimal('120.00'),
            is_preferred=True
        )

    # ─────────────────────────────────────────────────────────────────────────
    # 1. Verified Receiving adds stock once with PURCHASE type
    # ─────────────────────────────────────────────────────────────────────────
    def test_verified_receiving_adds_stock_once(self):
        po = PurchaseOrder.objects.create(
            po_number=PurchaseOrder.generate_po_number(),
            supplier=self.supplier,
            status=PurchaseOrder.POStatus.APPROVED,
            order_date=timezone.now().date(),
            expected_delivery_date=timezone.now().date() + timedelta(days=5),
            created_by=self.admin
        )
        poi = PurchaseOrderItem.objects.create(
            purchase_order=po,
            medicine=self.medicine,
            quantity_ordered=50,
            quantity_pending=50,
            unit_price=Decimal('120.00')
        )
        po.calculate_totals()

        # Create GRN shell
        grn = GoodsReceipt.objects.create(
            grn_number=GoodsReceipt.generate_grn_number(),
            purchase_order=po,
            supplier=self.supplier,
            receipt_date=timezone.now().date(),
            invoice_number='INV-TEST-001',
            invoice_amount=Decimal('6000.00'),
            status=GoodsReceipt.GRNStatus.PENDING_VERIFICATION,
            received_by=self.pharmacist
        )
        expiry = timezone.now().date() + timedelta(days=365)
        GoodsReceiptItem.objects.create(
            goods_receipt=grn,
            po_item=poi,
            medicine=self.medicine,
            quantity_received=50,
            quantity_accepted=50,
            quantity_rejected=0,
            batch_number='BATCH-CEF-001',
            expiry_date=expiry,
            unit_price=Decimal('120.00')
        )

        initial_stock = Inventory.objects.get(medicine=self.medicine).current_stock

        # Verify GRN
        purchase_services.process_grn_receipt(grn.pk, verified_by=self.pharmacist)

        self.inv.refresh_from_db()
        self.assertEqual(self.inv.current_stock, initial_stock + 50)

        # Verify transaction created with PURCHASE type
        txn = StockTransaction.objects.filter(
            medicine=self.medicine,
            reference_number=grn.grn_number,
            transaction_type=StockTransaction.TransactionType.PURCHASE
        ).first()
        self.assertIsNotNone(txn)
        self.assertEqual(txn.quantity, 50)

        # Batch quantity should be 50
        batch = MedicineBatch.objects.get(batch_number='BATCH-CEF-001', medicine=self.medicine)
        self.assertEqual(batch.quantity_available, 50)
        self.assertEqual(batch.quantity_received, 50)
        self.assertEqual(batch.status, MedicineBatch.BatchStatus.ACTIVE)

        # PO item received and pending updated
        poi.refresh_from_db()
        self.assertEqual(poi.quantity_received, 50)
        self.assertEqual(poi.quantity_pending, 0)

        # PO marked COMPLETED
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.POStatus.COMPLETED)

    # ─────────────────────────────────────────────────────────────────────────
    # 2. PO Creation & Email dispatch do NOT mutate stock
    # ─────────────────────────────────────────────────────────────────────────
    def test_po_creation_and_email_does_not_mutate_stock(self):
        initial_stock = Inventory.objects.get(medicine=self.medicine).current_stock
        initial_txns = StockTransaction.objects.filter(medicine=self.medicine).count()

        # Create PO
        po = PurchaseOrder.objects.create(
            po_number=PurchaseOrder.generate_po_number(),
            supplier=self.supplier,
            status=PurchaseOrder.POStatus.APPROVED,
            order_date=timezone.now().date(),
            created_by=self.admin
        )
        PurchaseOrderItem.objects.create(
            purchase_order=po,
            medicine=self.medicine,
            quantity_ordered=100,
            quantity_pending=100,
            unit_price=Decimal('120.00')
        )
        po.calculate_totals()

        # Send PO email
        self.client.force_login(self.admin)
        with patch('purchases.views.send_mail') as mock_send_mail:
            mock_send_mail.return_value = 1
            resp = self.client.post(reverse('send_po_email', args=[po.pk]))
            self.assertEqual(resp.status_code, 302)

        # Verify stock and transactions are completely untouched
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.current_stock, initial_stock)
        self.assertEqual(StockTransaction.objects.filter(medicine=self.medicine).count(), initial_txns)

        # Email transmission is tracked independently from supplier confirmation.
        po.refresh_from_db()
        self.assertIn(po.status, (PurchaseOrder.POStatus.APPROVED, PurchaseOrder.POStatus.ORDERED))
        self.assertEqual(po.email_status, PurchaseOrder.EmailStatus.SENT)
        self.assertIsNotNone(po.email_sent_at)
        self.assertIsNone(po.supplier_confirmed_at)
        self.assertIn('[EMAIL SENT', po.notes)

    # ─────────────────────────────────────────────────────────────────────────
    # 3. Partial Receiving workflow
    # ─────────────────────────────────────────────────────────────────────────
    def test_partial_receiving_workflow(self):
        po = PurchaseOrder.objects.create(
            po_number=PurchaseOrder.generate_po_number(),
            supplier=self.supplier,
            status=PurchaseOrder.POStatus.APPROVED,
            order_date=timezone.now().date(),
            created_by=self.admin
        )
        poi = PurchaseOrderItem.objects.create(
            purchase_order=po,
            medicine=self.medicine,
            quantity_ordered=100,
            quantity_pending=100,
            unit_price=Decimal('120.00')
        )

        # First GRN: receive 40 of 100
        grn1 = GoodsReceipt.objects.create(
            grn_number=GoodsReceipt.generate_grn_number(),
            purchase_order=po,
            supplier=self.supplier,
            receipt_date=timezone.now().date(),
            invoice_number='INV-PART-1',
            invoice_amount=Decimal('4800.00'),
            status=GoodsReceipt.GRNStatus.PENDING_VERIFICATION,
            received_by=self.pharmacist
        )
        GoodsReceiptItem.objects.create(
            goods_receipt=grn1,
            po_item=poi,
            medicine=self.medicine,
            quantity_received=40,
            quantity_accepted=40,
            batch_number='BATCH-PART-1',
            expiry_date=timezone.now().date() + timedelta(days=365),
            unit_price=Decimal('120.00')
        )
        purchase_services.process_grn_receipt(grn1.pk, verified_by=self.pharmacist)

        poi.refresh_from_db()
        po.refresh_from_db()
        self.assertEqual(poi.quantity_received, 40)
        self.assertEqual(poi.quantity_pending, 60)
        self.assertEqual(po.status, PurchaseOrder.POStatus.PARTIAL_RECEIPT)
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.current_stock, 40)

        # Second GRN: receive remaining 60
        grn2 = GoodsReceipt.objects.create(
            grn_number=GoodsReceipt.generate_grn_number(),
            purchase_order=po,
            supplier=self.supplier,
            receipt_date=timezone.now().date(),
            invoice_number='INV-PART-2',
            invoice_amount=Decimal('7200.00'),
            status=GoodsReceipt.GRNStatus.PENDING_VERIFICATION,
            received_by=self.pharmacist
        )
        GoodsReceiptItem.objects.create(
            goods_receipt=grn2,
            po_item=poi,
            medicine=self.medicine,
            quantity_received=60,
            quantity_accepted=60,
            batch_number='BATCH-PART-2',
            expiry_date=timezone.now().date() + timedelta(days=365),
            unit_price=Decimal('120.00')
        )
        purchase_services.process_grn_receipt(grn2.pk, verified_by=self.pharmacist)

        poi.refresh_from_db()
        po.refresh_from_db()
        self.assertEqual(poi.quantity_received, 100)
        self.assertEqual(poi.quantity_pending, 0)
        self.assertEqual(po.status, PurchaseOrder.POStatus.COMPLETED)
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.current_stock, 100)

    # ─────────────────────────────────────────────────────────────────────────
    # 4. Rejected quantities strictly excluded from stock
    # ─────────────────────────────────────────────────────────────────────────
    def test_rejected_quantities_never_added_to_stock(self):
        po = PurchaseOrder.objects.create(
            po_number=PurchaseOrder.generate_po_number(),
            supplier=self.supplier,
            status=PurchaseOrder.POStatus.APPROVED,
            order_date=timezone.now().date(),
            created_by=self.admin
        )
        poi = PurchaseOrderItem.objects.create(
            purchase_order=po,
            medicine=self.medicine,
            quantity_ordered=50,
            quantity_pending=50,
            unit_price=Decimal('120.00')
        )

        # Physical receiving: 50 received, but 15 rejected for DAMAGED_PACKAGING
        grn = GoodsReceipt.objects.create(
            grn_number=GoodsReceipt.generate_grn_number(),
            purchase_order=po,
            supplier=self.supplier,
            receipt_date=timezone.now().date(),
            invoice_number='INV-DMG-01',
            invoice_amount=Decimal('6000.00'),
            status=GoodsReceipt.GRNStatus.PENDING_VERIFICATION,
            received_by=self.pharmacist
        )
        GoodsReceiptItem.objects.create(
            goods_receipt=grn,
            po_item=poi,
            medicine=self.medicine,
            quantity_received=50,
            quantity_accepted=35,
            quantity_rejected=15,
            rejection_reason='DAMAGED_PACKAGING',
            batch_number='BATCH-DMG-01',
            expiry_date=timezone.now().date() + timedelta(days=365),
            unit_price=Decimal('120.00')
        )

        purchase_services.process_grn_receipt(grn.pk, verified_by=self.pharmacist)

        self.inv.refresh_from_db()
        # ONLY 35 must enter stock, NOT 50
        self.assertEqual(self.inv.current_stock, 35)

        batch = MedicineBatch.objects.get(batch_number='BATCH-DMG-01', medicine=self.medicine)
        self.assertEqual(batch.quantity_available, 35)

        # Check StockTransaction is only for 35
        txn = StockTransaction.objects.get(
            medicine=self.medicine,
            reference_number=grn.grn_number
        )
        self.assertEqual(txn.quantity, 35)

    # ─────────────────────────────────────────────────────────────────────────
    # 5. Duplicate GRN verification rejected
    # ─────────────────────────────────────────────────────────────────────────
    def test_duplicate_grn_verification_rejected(self):
        po = PurchaseOrder.objects.create(
            po_number=PurchaseOrder.generate_po_number(),
            supplier=self.supplier,
            status=PurchaseOrder.POStatus.APPROVED,
            order_date=timezone.now().date(),
            created_by=self.admin
        )
        poi = PurchaseOrderItem.objects.create(
            purchase_order=po,
            medicine=self.medicine,
            quantity_ordered=20,
            quantity_pending=20,
            unit_price=Decimal('120.00')
        )
        grn = GoodsReceipt.objects.create(
            grn_number=GoodsReceipt.generate_grn_number(),
            purchase_order=po,
            supplier=self.supplier,
            receipt_date=timezone.now().date(),
            invoice_number='INV-DUP-01',
            invoice_amount=Decimal('2400.00'),
            status=GoodsReceipt.GRNStatus.PENDING_VERIFICATION,
            received_by=self.pharmacist
        )
        GoodsReceiptItem.objects.create(
            goods_receipt=grn,
            po_item=poi,
            medicine=self.medicine,
            quantity_received=20,
            quantity_accepted=20,
            batch_number='BATCH-DUP-01',
            expiry_date=timezone.now().date() + timedelta(days=365),
            unit_price=Decimal('120.00')
        )

        # First verification succeeds
        purchase_services.process_grn_receipt(grn.pk, verified_by=self.pharmacist)

        # Second verification must raise ValidationError
        with self.assertRaises(ValidationError):
            purchase_services.process_grn_receipt(grn.pk, verified_by=self.pharmacist)

        self.inv.refresh_from_db()
        self.assertEqual(self.inv.current_stock, 20)

    # ─────────────────────────────────────────────────────────────────────────
    # 6. Expired batch rejected atomically during receiving
    # ─────────────────────────────────────────────────────────────────────────
    def test_expired_batch_receiving_rejected_atomically(self):
        po = PurchaseOrder.objects.create(
            po_number=PurchaseOrder.generate_po_number(),
            supplier=self.supplier,
            status=PurchaseOrder.POStatus.APPROVED,
            order_date=timezone.now().date(),
            created_by=self.admin
        )
        poi = PurchaseOrderItem.objects.create(
            purchase_order=po,
            medicine=self.medicine,
            quantity_ordered=20,
            quantity_pending=20,
            unit_price=Decimal('120.00')
        )
        grn = GoodsReceipt.objects.create(
            grn_number=GoodsReceipt.generate_grn_number(),
            purchase_order=po,
            supplier=self.supplier,
            receipt_date=timezone.now().date(),
            invoice_number='INV-EXP-01',
            invoice_amount=Decimal('2400.00'),
            status=GoodsReceipt.GRNStatus.PENDING_VERIFICATION,
            received_by=self.pharmacist
        )
        # Expiry date in the past
        past_expiry = timezone.now().date() - timedelta(days=10)
        GoodsReceiptItem.objects.create(
            goods_receipt=grn,
            po_item=poi,
            medicine=self.medicine,
            quantity_received=20,
            quantity_accepted=20,
            batch_number='BATCH-EXPIRED-99',
            expiry_date=past_expiry,
            unit_price=Decimal('120.00')
        )

        with self.assertRaises(ValidationError):
            purchase_services.process_grn_receipt(grn.pk, verified_by=self.pharmacist)

        # Ensure no stock added and no batch created
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.current_stock, 0)
        self.assertFalse(MedicineBatch.objects.filter(batch_number='BATCH-EXPIRED-99').exists())

    def test_receipt_cannot_exceed_outstanding_po_quantity(self):
        po = PurchaseOrder.objects.create(
            po_number=PurchaseOrder.generate_po_number(),
            supplier=self.supplier,
            status=PurchaseOrder.POStatus.APPROVED,
            created_by=self.admin,
        )
        poi = PurchaseOrderItem.objects.create(
            purchase_order=po,
            medicine=self.medicine,
            quantity_ordered=20,
            quantity_pending=20,
            unit_price=Decimal('120.00'),
        )
        grn = GoodsReceipt.objects.create(
            grn_number=GoodsReceipt.generate_grn_number(),
            purchase_order=po,
            supplier=self.supplier,
            receipt_date=timezone.now().date(),
            received_by=self.pharmacist,
        )
        GoodsReceiptItem.objects.create(
            goods_receipt=grn,
            po_item=poi,
            medicine=self.medicine,
            quantity_received=25,
            quantity_accepted=25,
            quantity_rejected=0,
            batch_number='BATCH-OVER-PO',
            expiry_date=timezone.now().date() + timedelta(days=365),
            unit_price=Decimal('120.00'),
        )

        with self.assertRaises(ValidationError):
            purchase_services.process_grn_receipt(grn.pk, verified_by=self.pharmacist)

        self.inv.refresh_from_db()
        poi.refresh_from_db()
        grn.refresh_from_db()
        self.assertEqual(self.inv.current_stock, 0)
        self.assertEqual(poi.quantity_received, 0)
        self.assertEqual(poi.quantity_pending, 20)
        self.assertEqual(grn.status, GoodsReceipt.GRNStatus.PENDING_VERIFICATION)
        self.assertFalse(MedicineBatch.objects.filter(batch_number='BATCH-OVER-PO').exists())

    # ─────────────────────────────────────────────────────────────────────────
    # 7. Barcode lookup is strictly non-mutating
    # ─────────────────────────────────────────────────────────────────────────
    def test_barcode_lookup_non_mutating(self):
        initial_stock = self.inv.current_stock
        initial_txns = StockTransaction.objects.count()

        self.client.force_login(self.pharmacist)
        url = reverse('grn_barcode_lookup') + f'?barcode={self.medicine.barcode}'
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['medicine_name'], self.medicine.medicine_name)
        self.assertEqual(data['strength'], self.medicine.strength)

        # Stock and transactions must remain unchanged
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.current_stock, initial_stock)
        self.assertEqual(StockTransaction.objects.count(), initial_txns)

    # ─────────────────────────────────────────────────────────────────────────
    # 8. Barcode lookup not found handling
    # ─────────────────────────────────────────────────────────────────────────
    def test_barcode_lookup_not_found(self):
        self.client.force_login(self.pharmacist)
        url = reverse('grn_barcode_lookup') + '?barcode=NONEXISTENT_BARCODE_123'
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)
        self.assertIn('error', response.json())

    # ─────────────────────────────────────────────────────────────────────────
    # 9. Delivery Tracking view metrics and overdue calculations
    # ─────────────────────────────────────────────────────────────────────────
    def test_delivery_tracking_view_metrics(self):
        # Create an overdue PO
        overdue_date = timezone.now().date() - timedelta(days=3)
        po = PurchaseOrder.objects.create(
            po_number=PurchaseOrder.generate_po_number(),
            supplier=self.supplier,
            status=PurchaseOrder.POStatus.ORDERED,
            order_date=timezone.now().date() - timedelta(days=10),
            expected_delivery_date=overdue_date,
            created_by=self.admin
        )
        PurchaseOrderItem.objects.create(
            purchase_order=po,
            medicine=self.medicine,
            quantity_ordered=80,
            quantity_pending=80,
            unit_price=Decimal('120.00')
        )
        po.calculate_totals()

        self.client.force_login(self.pharmacist)
        response = self.client.get(reverse('delivery_tracking'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, po.po_number)
        self.assertContains(response, '3d LATE')
        self.assertEqual(response.context['overdue_count'], 1)

    # ─────────────────────────────────────────────────────────────────────────
    # 10. AI Reorder Urgency factors in lead time (Days of Stock Remaining)
    # ─────────────────────────────────────────────────────────────────────────
    def test_ai_reorder_urgency_lead_time_aware(self):
        # Current stock = 10, safety stock = 20, reorder level = 40
        # If daily forecast is 5 units/day, stock lasts 2 days (10/5).
        # Supplier delivery lead time is 5 days.
        # Since stock runs out in 2 days but delivery takes 5 days (gap of 3 days),
        # priority must escalate to CRITICAL with reason tag mentioning delivery gap.
        self.inv.current_stock = 10
        self.inv.save()

        # Mock demand forecast to return 150 units for 30d (= 5 units/day)
        with patch('ai_engine.intelligence_service.get_demand_forecast') as mock_fc:
            mock_fc.return_value = {
                'forecast_30d': 150,
                'model_type': 'test_model',
                'confidence_score': 0.95
            }
            recs = get_reorder_recommendations()

        med_rec = next((r for r in recs if r['medicine'].pk == self.medicine.pk), None)
        self.assertIsNotNone(med_rec)
        self.assertEqual(med_rec['priority'], 'CRITICAL')
        self.assertIn('STOCKOUT BEFORE DELIVERY', med_rec['reason_tag'])
        self.assertEqual(med_rec['days_of_stock_remaining'], 2)
        self.assertEqual(med_rec['supplier_lead_days'], 5)
        self.assertLess(med_rec['stockout_risk_days'], 0)

    # ─────────────────────────────────────────────────────────────────────────
    # 11. AI Reorder respects Open PO awareness
    # ─────────────────────────────────────────────────────────────────────────
    def test_ai_reorder_open_po_awareness(self):
        self.inv.current_stock = 15
        self.inv.save()

        # Open PO exists with 100 units pending
        po = PurchaseOrder.objects.create(
            po_number=PurchaseOrder.generate_po_number(),
            supplier=self.supplier,
            status=PurchaseOrder.POStatus.ORDERED,
            order_date=timezone.now().date(),
            created_by=self.admin
        )
        PurchaseOrderItem.objects.create(
            purchase_order=po,
            medicine=self.medicine,
            quantity_ordered=100,
            quantity_pending=100,
            unit_price=Decimal('120.00')
        )

        with patch('ai_engine.intelligence_service.get_demand_forecast') as mock_fc:
            mock_fc.return_value = {
                'forecast_30d': 60,
                'model_type': 'test_model',
                'confidence_score': 0.90
            }
            recs = get_reorder_recommendations()

        # Effective available is current_stock (15) + open_po (100) = 115
        # Since 115 > reorder_level (40) and 115 - 60 (55) >= safety_stock (20),
        # no recommendation should be created or it should reflect open PO
        med_rec = next((r for r in recs if r['medicine'].pk == self.medicine.pk), None)
        self.assertIsNone(med_rec)

    # ─────────────────────────────────────────────────────────────────────────
    # 12. Create Purchase Request view pre-fills medicine_id
    # ─────────────────────────────────────────────────────────────────────────
    def test_create_pr_view_prefills_medicine_id(self):
        self.client.force_login(self.pharmacist)
        url = reverse('create_purchase_request') + f'?medicine_id={self.medicine.pk}'
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['initial_med'], self.medicine)
        self.assertIsNotNone(response.context['initial_ai_rec'])
        self.assertEqual(response.context['initial_best_supplier'], self.supplier)

    # ─────────────────────────────────────────────────────────────────────────
    # 13. Send PO Email view is restricted to Admin
    # ─────────────────────────────────────────────────────────────────────────
    def test_send_po_email_admin_only(self):
        po = PurchaseOrder.objects.create(
            po_number=PurchaseOrder.generate_po_number(),
            supplier=self.supplier,
            status=PurchaseOrder.POStatus.APPROVED,
            order_date=timezone.now().date(),
            created_by=self.admin
        )

        # Pharmacist attempting to send email should be denied / redirected
        self.client.force_login(self.pharmacist)
        response = self.client.post(reverse('send_po_email', args=[po.pk]))
        # admin_required redirects non-admins
        self.assertNotEqual(po.status, PurchaseOrder.POStatus.ORDERED)

        # Admin can successfully send
        self.client.force_login(self.admin)
        with patch('purchases.views.send_mail') as mock_send:
            mock_send.return_value = 1
            response = self.client.post(reverse('send_po_email', args=[po.pk]))
            self.assertEqual(response.status_code, 302)

        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.POStatus.ORDERED)
        self.assertIn('[EMAIL SENT', po.notes)

    # ─────────────────────────────────────────────────────────────────────────
    # 14. Send PO Email handles transmission failure gracefully
    # ─────────────────────────────────────────────────────────────────────────
    def test_send_po_email_handles_failure_gracefully(self):
        po = PurchaseOrder.objects.create(
            po_number=PurchaseOrder.generate_po_number(),
            supplier=self.supplier,
            status=PurchaseOrder.POStatus.APPROVED,
            order_date=timezone.now().date(),
            created_by=self.admin
        )

        self.client.force_login(self.admin)
        with patch('purchases.views.send_mail', side_effect=ConnectionError('SMTP connection refused')):
            response = self.client.post(reverse('send_po_email', args=[po.pk]))
            self.assertEqual(response.status_code, 302)

        po.refresh_from_db()
        self.assertIn('[EMAIL FAILED', po.notes)
        self.assertNotIn('SMTP connection refused', po.notes)
        self.assertEqual(po.email_status, PurchaseOrder.EmailStatus.FAILED)
        self.assertEqual(po.status, PurchaseOrder.POStatus.APPROVED)

    def test_supplier_confirmation_is_a_separate_admin_transition(self):
        po = PurchaseOrder.objects.create(
            po_number=PurchaseOrder.generate_po_number(),
            supplier=self.supplier,
            status=PurchaseOrder.POStatus.APPROVED,
            created_by=self.admin,
        )
        eta = timezone.localdate() + timedelta(days=4)
        self.client.force_login(self.admin)

        response = self.client.post(reverse('confirm_supplier_order', args=[po.pk]), {
            'confirmation_reference': 'Supplier email ref 123',
            'supplier_eta': eta.isoformat(),
        })

        self.assertEqual(response.status_code, 302)
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.POStatus.ORDERED)
        self.assertEqual(po.supplier_confirmation_reference, 'Supplier email ref 123')
        self.assertEqual(po.supplier_confirmed_eta, eta)
        self.assertEqual(po.expected_delivery_date, eta)
        self.assertEqual(po.supplier_confirmed_by, self.admin)

    # ─────────────────────────────────────────────────────────────────────────
    # 15. FEFO Batch Allocation after goods receipt
    # ─────────────────────────────────────────────────────────────────────────
    def test_fefo_allocation_after_receiving(self):
        # Create earlier batch
        batch_near = MedicineBatch.objects.create(
            medicine=self.medicine,
            batch_number='BATCH-FEFO-NEAR',
            expiry_date=timezone.now().date() + timedelta(days=60),
            quantity_available=20,
            quantity_received=20,
            purchase_price=Decimal('100.00'),
            selling_price=Decimal('120.00'),
            status=MedicineBatch.BatchStatus.NEAR_EXPIRY
        )
        # Create later batch
        batch_far = MedicineBatch.objects.create(
            medicine=self.medicine,
            batch_number='BATCH-FEFO-FAR',
            expiry_date=timezone.now().date() + timedelta(days=360),
            quantity_available=50,
            quantity_received=50,
            purchase_price=Decimal('100.00'),
            selling_price=Decimal('120.00'),
            status=MedicineBatch.BatchStatus.ACTIVE
        )
        inventory_services.recalculate_inventory(self.inv)

        # Allocate 15 units via FEFO
        allocated = inventory_services.allocate_stock(
            medicine=self.medicine,
            quantity_needed=15,
            policy='FEFO'
        )

        # Must allocate from batch_near first
        self.assertEqual(len(allocated), 1)
        self.assertEqual(allocated[0]['batch'], batch_near)
        self.assertEqual(allocated[0]['quantity'], 15)

    # ─────────────────────────────────────────────────────────────────────────
    # 16. PR to PO Conversion workflow
    # ─────────────────────────────────────────────────────────────────────────
    def test_pr_to_po_conversion_workflow(self):
        pr = PurchaseRequest.objects.create(
            request_id=PurchaseRequest.generate_request_id(),
            priority=PurchaseRequest.Priority.URGENT,
            status=PurchaseRequest.PRStatus.APPROVED,
            reason='Stock depleting fast',
            recommended_supplier=self.supplier,
            created_by=self.pharmacist
        )
        PurchaseRequestItem.objects.create(
            purchase_request=pr,
            medicine=self.medicine,
            current_stock=10,
            reorder_level=40,
            requested_qty=60,
            approved_qty=50,
            suggested_supplier=self.supplier,
            suggested_unit_price=Decimal('120.00')
        )

        po = purchase_services.convert_pr_to_po(
            purchase_request=pr,
            admin_user=self.admin
        )

        self.assertIsNotNone(po)
        self.assertEqual(po.status, PurchaseOrder.POStatus.APPROVED)
        self.assertEqual(po.priority, PurchaseRequest.Priority.URGENT)
        # Verify approved_qty was used, not requested_qty
        poi = po.items.first()
        self.assertEqual(poi.quantity_ordered, 50)
        self.assertEqual(poi.quantity_pending, 50)

        pr.refresh_from_db()
        self.assertEqual(pr.status, PurchaseRequest.PRStatus.CONVERTED_TO_PO)
        self.assertEqual(pr.converted_to_po, po)

    # ─────────────────────────────────────────────────────────────────────────
    # 17. Cancel PO does not modify stock
    # ─────────────────────────────────────────────────────────────────────────
    def test_cancel_po_does_not_modify_stock(self):
        initial_stock = self.inv.current_stock
        po = PurchaseOrder.objects.create(
            po_number=PurchaseOrder.generate_po_number(),
            supplier=self.supplier,
            status=PurchaseOrder.POStatus.APPROVED,
            order_date=timezone.now().date(),
            created_by=self.admin
        )
        PurchaseOrderItem.objects.create(
            purchase_order=po,
            medicine=self.medicine,
            quantity_ordered=50,
            quantity_pending=50,
            unit_price=Decimal('120.00')
        )

        self.client.force_login(self.admin)
        response = self.client.post(reverse('cancel_po', args=[po.pk]), {
            'cancellation_reason': 'Supplier out of stock'
        })
        self.assertEqual(response.status_code, 302)

        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.POStatus.CANCELLED)
        self.assertEqual(po.cancellation_reason, 'Supplier out of stock')
        self.assertEqual(po.cancelled_by, self.admin)

        self.inv.refresh_from_db()
        self.assertEqual(self.inv.current_stock, initial_stock)
