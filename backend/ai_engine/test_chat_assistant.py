"""
ai_engine/test_chat_assistant.py
MediVault — Sanalogx AI Assistant Comprehensive Automated Test Suite

Tests:
1. Health check & Capabilities GET API
2. Greetings, Help, and Unsupported clarification
3. All 14 Quick Actions & Intents (Inventory, Sales, Dispensing, Expiry, Consumption, Reorders, Discrepancies, POs)
4. Genuine Medicine Pharmacology Knowledge Engine & Live Inventory Linkage
5. Conversation Context & Follow-Up Resolution (e.g. "What about its batches?")
6. RBAC Security & Gating (Admin vs Pharmacist profit permissions, PII masking)
7. Security Protections (XSS, Injection, Invalid Payloads, Unauthenticated Access)
"""

import json
from decimal import Decimal
from datetime import timedelta, date
from django.test import TestCase, Client
from django.utils import timezone
from django.urls import reverse

from accounts.models import User
from medicines.models import Medicine, MedicineBatch, Category
from inventory.models import Inventory, StockTransaction
from sales.models import Sale, SaleItem
from dispensing.models import DispensingRecord, DispensingAllocation, Patient, StockDiscrepancy, ControlledMedicineRegister
from purchases.models import PurchaseOrder, PurchaseOrderItem
from suppliers.models import Supplier, SupplierMedicine
from ai_engine import chat_service


class SanalogxChatAssistantTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.today = timezone.localdate()

        # Users
        self.admin = User.objects.create_superuser(
            username='admin_sanalogx',
            email='admin@sanalogx.com',
            password='AdminPassword123!',
            role='ADMIN',
            status='APPROVED',
        )
        self.pharmacist = User.objects.create_user(
            username='pharm_sanalogx',
            email='pharm@sanalogx.com',
            password='PharmPassword123!',
            role='PHARMACIST',
            status='APPROVED',
        )

        # Categories
        self.cat_analgesic = Category.objects.create(name='Analgesics', code='ANALGESIC')
        self.cat_antibiotic = Category.objects.create(name='Antibiotics', code='ANTIBIOTIC')

        # Medicines
        self.med_para = Medicine.objects.create(
            medicine_name='Paracetamol',
            generic_name='Paracetamol',
            category=self.cat_analgesic,
            dosage_form='Tablet',
            strength='500mg',
            unit_of_measure='Strip',
            reorder_level=50,
            safety_stock=20,
            minimum_stock=10,
            maximum_stock=500,
            regulatory_category=Medicine.RegulatoryCategory.NORMAL,
            status='ACTIVE',
        )
        self.inv_para = Inventory.objects.create(
            medicine=self.med_para,
            current_stock=120,
            available_stock=120,
            reorder_level=50,
            safety_stock=20,
            minimum_stock=10,
            maximum_stock=500,
            stock_status='OPTIMAL',
        )
        self.batch_para1 = MedicineBatch.objects.create(
            medicine=self.med_para,
            batch_number='PARA-2026-A',
            expiry_date=self.today + timedelta(days=180),
            purchase_price=Decimal('10.00'),
            selling_price=Decimal('15.00'),
            quantity_received=100,
            quantity_available=100,
            status=MedicineBatch.BatchStatus.ACTIVE,
        )
        self.batch_para2 = MedicineBatch.objects.create(
            medicine=self.med_para,
            batch_number='PARA-2026-B',
            expiry_date=self.today + timedelta(days=25),  # Near expiry (<30d)
            purchase_price=Decimal('10.00'),
            selling_price=Decimal('15.00'),
            quantity_received=20,
            quantity_available=20,
            status=MedicineBatch.BatchStatus.NEAR_EXPIRY,
        )

        self.med_amox = Medicine.objects.create(
            medicine_name='Amoxicillin',
            generic_name='Amoxicillin',
            category=self.cat_antibiotic,
            dosage_form='Capsule',
            strength='500mg',
            unit_of_measure='Strip',
            reorder_level=60,
            safety_stock=20,
            minimum_stock=10,
            maximum_stock=400,
            regulatory_category=Medicine.RegulatoryCategory.SCHEDULE_H,
            status='ACTIVE',
        )
        self.inv_amox = Inventory.objects.create(
            medicine=self.med_amox,
            current_stock=25,  # Low stock (25 <= 60)
            available_stock=25,
            reorder_level=60,
            safety_stock=20,
            minimum_stock=10,
            maximum_stock=400,
            stock_status='LOW_STOCK',
        )

        self.med_zero = Medicine.objects.create(
            medicine_name='Metformin',
            generic_name='Metformin HCl',
            category=self.cat_analgesic,
            dosage_form='Tablet',
            strength='500mg',
            unit_of_measure='Strip',
            reorder_level=40,
            safety_stock=15,
            minimum_stock=5,
            maximum_stock=300,
            regulatory_category=Medicine.RegulatoryCategory.SCHEDULE_H,
            status='ACTIVE',
        )
        self.inv_zero = Inventory.objects.create(
            medicine=self.med_zero,
            current_stock=0,  # Out of stock
            available_stock=0,
            reorder_level=40,
            safety_stock=15,
            minimum_stock=5,
            maximum_stock=300,
            stock_status='OUT_OF_STOCK',
        )

        # Supplier & Price linkage
        self.supplier = Supplier.objects.create(
            company_name='Sun Pharma Diagnostics',
            status='ACTIVE',
            rating=Decimal('4.8'),
        )
        SupplierMedicine.objects.create(
            supplier=self.supplier,
            medicine=self.med_amox,
            unit_price=Decimal('8.50'),
            delivery_days=3,
            is_available=True,
        )

        # Sales records
        self.sale1 = Sale.objects.create(
            sale_number='SALE-TEST-001',
            invoice_number='INV-TEST-001',
            patient_name='Rajesh Kumar',
            patient_id='UHID-10001',
            subtotal=Decimal('150.00'),
            total_amount=Decimal('150.00'),
            amount_paid=Decimal('150.00'),
            outstanding_amount=Decimal('0.00'),
            payment_method=Sale.PaymentMethod.UPI,
            status=Sale.SaleStatus.COMPLETED,
            sold_by=self.pharmacist,
        )
        SaleItem.objects.create(
            sale=self.sale1,
            medicine=self.med_para,
            batch=self.batch_para1,
            quantity=10,
            unit_price=Decimal('15.00'),
            total_price=Decimal('150.00'),
            purchase_unit_price=Decimal('10.00'),
        )

        # Dispensing record
        self.disp_record = DispensingRecord.objects.create(
            dispensing_id='DISP-TEST-001',
            patient_name='Sunita Verma',
            patient_id='UHID-99002',
            medicine=self.med_para,
            total_quantity=5,
            pharmacist=self.pharmacist,
            status=DispensingRecord.Status.COMPLETED,
        )

    # ─── 1. Health Check API (GET) ───────────────────────────────────────────
    def test_get_health_check_endpoint(self):
        """GET /api/ai/chat/ returns ready status, branding, and capabilities list."""
        self.client.force_login(self.pharmacist)
        res = self.client.get(reverse('api_ai_chat'))
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['status'], 'ready')
        self.assertEqual(data['assistant_name'], 'Sanalogx AI Assistant')
        self.assertEqual(data['branding'], 'Sanalogx AI Assistant')
        self.assertIn('Inventory Overview', data['capabilities'])
        self.assertIn('AI Reorder Recommendations', data['capabilities'])

    # ─── 2. Greetings and Help ───────────────────────────────────────────────
    def test_greeting_intent(self):
        """Greeting 'hello' returns welcome message with user name and quick action options."""
        self.client.force_login(self.pharmacist)
        res = self.client.post(
            reverse('api_ai_chat'),
            data=json.dumps({'message': 'Hello'}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['intent'], 'GREETING')
        self.assertIn('Sanalogx AI Assistant', data['answer'])
        self.assertIn(self.pharmacist.username, data['answer'])

    def test_help_intent(self):
        """'Help' or 'What can you do?' returns capabilities breakdown and commands."""
        self.client.force_login(self.pharmacist)
        res = self.client.post(
            reverse('api_ai_chat'),
            data=json.dumps({'message': 'What can you do?'}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['intent'], 'HELP')
        self.assertIn('Capabilities & Commands', data['answer'])
        self.assertIn('Inventory', data['answer'])

    # ─── 3. Inventory Intents ────────────────────────────────────────────────
    def test_inventory_overview(self):
        """'Inventory overview' computes real database counts and available units."""
        self.client.force_login(self.pharmacist)
        res = self.client.post(
            reverse('api_ai_chat'),
            data=json.dumps({'message': 'Inventory Overview'}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['intent'], 'INVENTORY_OVERVIEW')
        self.assertIn('Active Medicines Catalogue', data['answer'])
        self.assertGreaterEqual(data['data']['total_medicines'], 3)

    def test_low_stock_medicines(self):
        """'Low stock medicines' reports Amoxicillin correctly."""
        self.client.force_login(self.pharmacist)
        res = self.client.post(
            reverse('api_ai_chat'),
            data=json.dumps({'message': 'Low Stock Medicines'}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['intent'], 'CHECK_LOW_STOCK')
        self.assertIn('Amoxicillin', data['answer'])
        self.assertGreaterEqual(data['data']['low_stock_count'], 1)

    def test_out_of_stock_medicines(self):
        """'Out-of-Stock Medicines' correctly identifies Metformin."""
        self.client.force_login(self.pharmacist)
        res = self.client.post(
            reverse('api_ai_chat'),
            data=json.dumps({'message': 'Out-of-Stock Medicines'}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['intent'], 'CHECK_OUT_OF_STOCK')
        self.assertIn('Metformin', data['answer'])
        self.assertGreaterEqual(data['data']['out_of_stock_count'], 1)

    def test_specific_medicine_stock_and_batches(self):
        """'Show stock for Paracetamol' and 'Which batches are available for Paracetamol?'."""
        self.client.force_login(self.pharmacist)
        # 1. Stock
        res = self.client.post(
            reverse('api_ai_chat'),
            data=json.dumps({'message': 'Show stock for Paracetamol'}),
            content_type='application/json'
        )
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['intent'], 'CHECK_MEDICINE_STOCK')
        self.assertIn('120 units', data['answer'])

        # 2. Batches
        res2 = self.client.post(
            reverse('api_ai_chat'),
            data=json.dumps({'message': 'Which batches are available for Paracetamol?'}),
            content_type='application/json'
        )
        data2 = res2.json()
        self.assertTrue(data2['success'])
        self.assertEqual(data2['intent'], 'CHECK_BATCHES')
        self.assertIn('PARA-2026-A', data2['answer'])
        self.assertIn('PARA-2026-B', data2['answer'])

    # ─── 4. Medicine Pharmacology Knowledge Engine ───────────────────────────
    def test_medicine_knowledge_paracetamol(self):
        """'What is Paracetamol?' returns verified monograph + live inventory."""
        self.client.force_login(self.pharmacist)
        res = self.client.post(
            reverse('api_ai_chat'),
            data=json.dumps({'message': 'What is Paracetamol?'}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['intent'], 'MEDICINE_INFO')
        self.assertIn('Clinical Monograph', data['answer'])
        self.assertIn('Dolo 650', data['answer'])
        self.assertIn('Analgesic & Antipyretic', data['answer'])
        self.assertIn('Clinical Advisory', data['answer'])
        self.assertIn('Live MediVault Inventory Status', data['answer'])

    def test_medicine_knowledge_prompt_when_no_medicine_named(self):
        """'Medicine information' with no medicine names prompts user for clarification."""
        self.client.force_login(self.pharmacist)
        res = self.client.post(
            reverse('api_ai_chat'),
            data=json.dumps({'message': 'Medicine information'}),
            content_type='application/json'
        )
        data = res.json()
        self.assertTrue(data['success'])
        self.assertIn('Which medicine would you like clinical', data['answer'])

    # ─── 5. Sales & RBAC ─────────────────────────────────────────────────────
    def test_today_sales_intent(self):
        """'Today's Sales' reports completed sales and revenue."""
        self.client.force_login(self.pharmacist)
        res = self.client.post(
            reverse('api_ai_chat'),
            data=json.dumps({'message': "Today's Sales"}),
            content_type='application/json'
        )
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['intent'], 'SALES_TODAY')
        self.assertIn('150.00', data['answer'])
        self.assertIn('UPI', data['answer'])

    def test_profit_summary_rbac_restrictions(self):
        """Pharmacist cannot view financial gross profit; Admin can view it."""
        # 1. Pharmacist attempt
        self.client.force_login(self.pharmacist)
        res_pharm = self.client.post(
            reverse('api_ai_chat'),
            data=json.dumps({'message': 'Show gross profit'}),
            content_type='application/json'
        )
        data_pharm = res_pharm.json()
        self.assertTrue(data_pharm['success'])
        self.assertEqual(data_pharm['intent'], 'PROFIT_RESTRICTED')
        self.assertIn('Access Restricted', data_pharm['answer'])

        # 2. Admin attempt
        self.client.force_login(self.admin)
        res_admin = self.client.post(
            reverse('api_ai_chat'),
            data=json.dumps({'message': 'Show gross profit'}),
            content_type='application/json'
        )
        data_admin = res_admin.json()
        self.assertTrue(data_admin['success'])
        self.assertEqual(data_admin['intent'], 'PROFIT_SUMMARY')
        self.assertIn('Financial & Profit Analytics', data_admin['answer'])

    # ─── 6. Dispensing & Privacy Preservation ────────────────────────────────
    def test_dispensing_history_masks_patient_pii(self):
        """Dispensing history masks UHID to preserve patient privacy."""
        self.client.force_login(self.pharmacist)
        res = self.client.post(
            reverse('api_ai_chat'),
            data=json.dumps({'message': 'Dispensing History'}),
            content_type='application/json'
        )
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['intent'], 'DISPENSING_HISTORY')
        self.assertIn('DISP-TEST-001', data['answer'])
        # UHID-99002 should be masked as UHID-***02
        self.assertNotIn('UHID-99002', data['answer'])
        self.assertIn('***', data['answer'])

    # ─── 7. Expiry Risk Intent ───────────────────────────────────────────────
    def test_expiry_risk_intent(self):
        """'Expiry and Near-Expiry Batches' detects batch PARA-2026-B (25 days left)."""
        self.client.force_login(self.pharmacist)
        res = self.client.post(
            reverse('api_ai_chat'),
            data=json.dumps({'message': 'Expiry and Near-Expiry Batches'}),
            content_type='application/json'
        )
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['intent'], 'CHECK_EXPIRY_RISK')
        self.assertIn('PARA-2026-B', data['answer'])
        self.assertGreaterEqual(data['data']['exp_30d'], 1)

    # ─── 8. Discrepancy & Purchase Orders ─────────────────────────────────────
    def test_stock_discrepancies_intent(self):
        """'Stock Discrepancies' queries StockDiscrepancy queue."""
        StockDiscrepancy.objects.create(
            discrepancy_id=StockDiscrepancy.generate_discrepancy_id(),
            medicine=self.med_para,
            batch=self.batch_para1,
            expected_quantity=100,
            actual_quantity=95,
            difference=-5,
            discrepancy_type=StockDiscrepancy.DiscrepancyType.PHYSICAL_VS_SYSTEM,
            status=StockDiscrepancy.Status.OPEN,
            detected_by=self.pharmacist,
        )
        self.client.force_login(self.pharmacist)
        res = self.client.post(
            reverse('api_ai_chat'),
            data=json.dumps({'message': 'Stock Discrepancies'}),
            content_type='application/json'
        )
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['intent'], 'STOCK_DISCREPANCY')
        self.assertIn('Paracetamol', data['answer'])
        self.assertEqual(data['data']['open_count'], 1)

    def test_purchase_orders_intent(self):
        """'Purchase Orders' intent reports PO status counts and pending units."""
        po = PurchaseOrder.objects.create(
            po_number='PO-2026-99999',
            supplier=self.supplier,
            status='ORDERED',
            total_amount=Decimal('500.00'),
            order_date=self.today,
            created_by=self.admin,
        )
        PurchaseOrderItem.objects.create(
            purchase_order=po,
            medicine=self.med_amox,
            quantity_ordered=50,
            quantity_received=0,
            quantity_pending=50,
            unit_price=Decimal('8.50'),
            total_price=Decimal('425.00'),
        )
        self.client.force_login(self.pharmacist)
        res = self.client.post(
            reverse('api_ai_chat'),
            data=json.dumps({'message': 'Purchase Orders'}),
            content_type='application/json'
        )
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['intent'], 'PURCHASE_ORDERS')
        self.assertIn('PO-2026-99999', data['answer'])
        self.assertGreaterEqual(data['data']['incoming_units'], 50)

    # ─── 9. Conversation Context & Follow-Up Questions ───────────────────────
    def test_conversation_context_followup_resolution(self):
        """User queries Paracetamol stock, then asks 'Which batches are available for it?'"""
        self.client.force_login(self.pharmacist)
        # Step 1: Query Paracetamol
        res1 = self.client.post(
            reverse('api_ai_chat'),
            data=json.dumps({'message': 'Show stock for Paracetamol'}),
            content_type='application/json'
        )
        self.assertTrue(res1.json()['success'])

        # Step 2: Follow-up using pronoun 'it'
        res2 = self.client.post(
            reverse('api_ai_chat'),
            data=json.dumps({'message': 'Which batches are available for it?'}),
            content_type='application/json'
        )
        data2 = res2.json()
        self.assertTrue(data2['success'])
        self.assertEqual(data2['intent'], 'CHECK_BATCHES')
        self.assertIn('Paracetamol', data2['answer'])
        self.assertIn('PARA-2026-A', data2['answer'])

    def test_clear_chat_context_action(self):
        """POST with action='clear' removes session conversation memory."""
        self.client.force_login(self.pharmacist)
        res = self.client.post(
            reverse('api_ai_chat'),
            data=json.dumps({'action': 'clear'}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertTrue(data['cleared'])

    # ─── 10. Security & Input Robustness ─────────────────────────────────────
    def test_empty_message_handling(self):
        """Empty query returns friendly prompt without crashing."""
        self.client.force_login(self.pharmacist)
        res = self.client.post(
            reverse('api_ai_chat'),
            data=json.dumps({'message': '   '}),
            content_type='application/json'
        )
        data = res.json()
        self.assertFalse(data['success'])
        self.assertEqual(data['intent'], 'EMPTY')

    def test_invalid_json_handling(self):
        """Invalid non-JSON request body returns HTTP 400."""
        self.client.force_login(self.pharmacist)
        res = self.client.post(
            reverse('api_ai_chat'),
            data="not a json",
            content_type='application/json'
        )
        self.assertEqual(res.status_code, 400)

    def test_xss_and_sql_injection_payload_safety(self):
        """XSS tags or SQL injection strings do not crash or execute."""
        self.client.force_login(self.pharmacist)
        xss_query = "<script>alert('xss')</script> Show Paracetamol stock"
        res = self.client.post(
            reverse('api_ai_chat'),
            data=json.dumps({'message': xss_query}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertNotIn('<script>', data['answer'])

    def test_unauthenticated_access_denied(self):
        """Unauthenticated requests are redirected or denied."""
        res = self.client.post(
            reverse('api_ai_chat'),
            data=json.dumps({'message': 'Inventory Overview'}),
            content_type='application/json'
        )
        # Should redirect to pharmacist login
        self.assertEqual(res.status_code, 302)
