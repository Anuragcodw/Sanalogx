"""
ai_engine/test_voice_assistant.py
Comprehensive automated test suite for Sanalogx AI Voice Assistant
Covers:
1. Stock query for known medicine ("How many Paracetamol tablets are available?")
2. Low stock query ("Show medicines with low stock")
3. Expiry next month query ("Show medicines expiring next month")
4. Ambiguous medicine name clarification (disambiguation choices)
5. Hindi / Hinglish queries ("Paracetamol ka stock kitna hai", "kam stock wali dawaiyan")
6. Unauthorized user attempting stock addition (Permission denied)
7. Stock addition confirmation generation (Two-step safety gating)
8. Stock addition cancellation (Leaves inventory untouched)
9. Confirmed stock addition updating inventory (StockTransaction + AuditLog recorded)
10. Duplicate command idempotency (Token cannot be consumed twice)
11. Purchase request creation via voice ("Create a purchase request for 100 units of Paracetamol")
12. Out of stock query ("Find medicines that are out of stock")
13. Stock movements query ("Show today's stock movements")
14. Pending purchase requests query ("Show pending purchase requests")
15. Medicines needing reordering query ("Show medicines that need reordering")
16. Navigation commands ("Navigate to expiry management")
17. Empty query handling & Unauthenticated access gating
"""

import json
from decimal import Decimal
from datetime import timedelta
from django.test import TestCase, Client
from django.utils import timezone
from django.urls import reverse
from django.core.cache import cache

from accounts.models import User
from medicines.models import Medicine, MedicineBatch, Category
from inventory.models import Inventory, StockTransaction
from purchases.models import PurchaseRequest, PurchaseRequestItem, PurchaseOrder, PurchaseOrderItem
from suppliers.models import Supplier, SupplierMedicine
from ai_engine.models import VoiceCommandLog, AuditLog, AIRecommendation
from ai_engine import voice_service


class SanalogxVoiceAssistantTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()
        self.today = timezone.localdate()

        # Users
        self.admin = User.objects.create_superuser(
            username='admin_voice',
            email='admin_voice@sanalogx.com',
            password='AdminPassword123!',
            role=User.Role.ADMIN,
            status='APPROVED',
        )
        self.pharmacist = User.objects.create_user(
            username='pharm_voice',
            email='pharm_voice@sanalogx.com',
            password='PharmPassword123!',
            role=User.Role.PHARMACIST,
            status='APPROVED',
        )
        self.doctor = User.objects.create_user(
            username='doctor_voice',
            email='doctor_voice@sanalogx.com',
            password='DocPassword123!',
            role=User.Role.DOCTOR,
            status='APPROVED',
        )

        # Categories
        self.cat_analgesic = Category.objects.create(name='Analgesics', code='ANALGESIC')
        self.cat_antibiotic = Category.objects.create(name='Antibiotics', code='ANTIBIOTIC')

        # Primary test medicine: Paracetamol
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
            status=Medicine.Status.ACTIVE,
        )
        self.inv_para = Inventory.objects.create(
            medicine=self.med_para,
            current_stock=100,
            available_stock=100,
            reorder_level=50,
            safety_stock=20,
            minimum_stock=10,
            maximum_stock=500,
        )
        self.batch_para = MedicineBatch.objects.create(
            medicine=self.med_para,
            batch_number='PARA-2026-01',
            expiry_date=self.today + timedelta(days=200),
            purchase_price=Decimal('10.00'),
            selling_price=Decimal('15.00'),
            quantity_received=100,
            quantity_available=100,
            status=MedicineBatch.BatchStatus.ACTIVE,
        )

        # Low stock medicine: Amoxicillin
        self.med_amox = Medicine.objects.create(
            medicine_name='Amoxicillin',
            generic_name='Amoxicillin',
            category=self.cat_antibiotic,
            dosage_form='Capsule',
            strength='250mg',
            unit_of_measure='Capsule',
            reorder_level=40,
            safety_stock=15,
            minimum_stock=10,
            maximum_stock=400,
            status=Medicine.Status.ACTIVE,
        )
        self.inv_amox = Inventory.objects.create(
            medicine=self.med_amox,
            current_stock=15,  # Below reorder level 40!
            available_stock=15,
            reorder_level=40,
            safety_stock=15,
            minimum_stock=10,
            maximum_stock=400,
        )
        self.batch_amox = MedicineBatch.objects.create(
            medicine=self.med_amox,
            batch_number='AMOX-2026-01',
            expiry_date=self.today + timedelta(days=20),  # Expiring in 20 days (next month)!
            purchase_price=Decimal('12.00'),
            selling_price=Decimal('18.00'),
            quantity_received=50,
            quantity_available=15,
            status=MedicineBatch.BatchStatus.NEAR_EXPIRY,
        )

        # Out of stock medicine: Cetirizine
        self.med_cetz = Medicine.objects.create(
            medicine_name='Cetirizine',
            generic_name='Cetirizine',
            category=self.cat_analgesic,
            dosage_form='Tablet',
            strength='10mg',
            unit_of_measure='Strip',
            reorder_level=30,
            status=Medicine.Status.ACTIVE,
        )
        self.inv_cetz = Inventory.objects.create(
            medicine=self.med_cetz,
            current_stock=0,
            available_stock=0,
            reorder_level=30,
        )

        # Active Supplier for purchase request testing
        self.supplier = Supplier.objects.create(
            company_name='MedLife Pharma Suppliers',
            contact_person='Rajesh Kumar',
            email='orders@medlife.com',
            phone='9876543210',
            status=Supplier.SupplierStatus.ACTIVE,
            rating=Decimal('4.8'),
        )
        SupplierMedicine.objects.create(
            supplier=self.supplier,
            medicine=self.med_para,
            unit_price=Decimal('9.50'),
            delivery_days=3,
            pack_size=10,
            minimum_order_quantity=20,
            is_available=True,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # 1. Stock Query for Known Medicine
    # ─────────────────────────────────────────────────────────────────────────
    def test_stock_query_for_known_medicine(self):
        """Spoken: 'How many Paracetamol tablets are available?'"""
        self.client.force_login(self.pharmacist)
        response = self.client.post(
            reverse('api_voice_assistant'),
            data=json.dumps({'query': 'How many Paracetamol tablets are available?'}),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['intent'], 'CHECK_MEDICINE_STOCK')
        self.assertIn('Paracetamol', data['speech'])
        self.assertIn('100', data['speech'])
        self.assertEqual(data['usable_stock'], 100)
        self.assertIn('/inventory/medicine/', data['action_url'])

        # Verify command was logged to VoiceCommandLog
        log_entry = VoiceCommandLog.objects.filter(intent='CHECK_MEDICINE_STOCK').first()
        self.assertIsNotNone(log_entry)
        self.assertEqual(log_entry.user, self.pharmacist)

    # ─────────────────────────────────────────────────────────────────────────
    # 2. Low Stock Query
    # ─────────────────────────────────────────────────────────────────────────
    def test_low_stock_query(self):
        """Spoken: 'Show medicines with low stock.'"""
        self.client.force_login(self.pharmacist)
        response = self.client.post(
            reverse('api_voice_assistant'),
            data=json.dumps({'query': 'Show medicines with low stock'}),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['intent'], 'CHECK_LOW_STOCK')
        self.assertGreaterEqual(data['count'], 1)
        self.assertIn('reorder level', data['speech'].lower())
        self.assertEqual(data['action_url'], '/inventory/?status=LOW_STOCK')

    # ─────────────────────────────────────────────────────────────────────────
    # 3. Expiry Next Month Query
    # ─────────────────────────────────────────────────────────────────────────
    def test_expiry_next_month_query(self):
        """Spoken: 'Show medicines expiring next month.'"""
        self.client.force_login(self.pharmacist)
        response = self.client.post(
            reverse('api_voice_assistant'),
            data=json.dumps({'query': 'Show medicines expiring next month'}),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['intent'], 'CHECK_EXPIRY')
        self.assertGreaterEqual(data['count'], 1)
        self.assertIn('Amoxicillin', data['speech'])
        self.assertEqual(data['action_url'], '/inventory/expiry/')

    # ─────────────────────────────────────────────────────────────────────────
    # 4. Ambiguous Medicine Name Clarification
    # ─────────────────────────────────────────────────────────────────────────
    def test_ambiguous_medicine_name_clarification(self):
        """When multiple variants exist without an exact match, returns choice options."""
        # Create two distinct formulations with no exact 'Ibuprofen' single record
        Medicine.objects.create(
            medicine_name='Ibuprofen 200mg',
            generic_name='Ibuprofen',
            category=self.cat_analgesic,
            dosage_form='Tablet',
            strength='200mg',
            unit_of_measure='Strip',
            status=Medicine.Status.ACTIVE,
        )
        Medicine.objects.create(
            medicine_name='Ibuprofen 400mg',
            generic_name='Ibuprofen',
            category=self.cat_analgesic,
            dosage_form='Tablet',
            strength='400mg',
            unit_of_measure='Strip',
            status=Medicine.Status.ACTIVE,
        )

        self.client.force_login(self.pharmacist)
        response = self.client.post(
            reverse('api_voice_assistant'),
            data=json.dumps({'query': 'Stock of Ibuprofen'}),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertTrue(data.get('is_ambiguous'))
        self.assertEqual(data.get('action'), 'CHOICE_REQUIRED')
        self.assertGreaterEqual(len(data.get('matching_medicines', [])), 2)
        self.assertIn('Multiple medicines match', data['speech'])

    # ─────────────────────────────────────────────────────────────────────────
    # 5. Hindi and Hinglish Queries
    # ─────────────────────────────────────────────────────────────────────────
    def test_hindi_hinglish_queries(self):
        """Spoken Hindi/Hinglish commands map accurately to workflows."""
        self.client.force_login(self.pharmacist)

        # 1. Stock check: "Paracetamol ka stock kitna hai"
        res1 = self.client.post(
            reverse('api_voice_assistant'),
            data=json.dumps({'query': 'Paracetamol ka stock kitna hai'}),
            content_type='application/json'
        )
        self.assertEqual(res1.status_code, 200)
        data1 = res1.json()
        self.assertEqual(data1['intent'], 'CHECK_MEDICINE_STOCK')
        self.assertIn('Paracetamol', data1['speech'])

        # 2. Low stock: "Kam stock wali dawaiyan dikhao"
        res2 = self.client.post(
            reverse('api_voice_assistant'),
            data=json.dumps({'query': 'kam stock wali dawaiyan dikhao'}),
            content_type='application/json'
        )
        self.assertEqual(res2.status_code, 200)
        data2 = res2.json()
        self.assertEqual(data2['intent'], 'CHECK_LOW_STOCK')

    # ─────────────────────────────────────────────────────────────────────────
    # 6. Unauthorized User Attempting Stock Addition
    # ─────────────────────────────────────────────────────────────────────────
    def test_unauthorized_user_attempting_stock_addition(self):
        """Doctor attempting 'Add 50 Paracetamol' is rejected with permission denied."""
        self.client.force_login(self.doctor)
        initial_stock = self.batch_para.quantity_available

        response = self.client.post(
            reverse('api_voice_assistant'),
            data=json.dumps({'query': 'Add 50 Paracetamol tablets'}),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data['success'])
        self.assertEqual(data.get('action'), 'ACCESS_DENIED')
        self.assertIn('Access restricted', data['speech'])

        # Verify database stock was NOT modified
        self.batch_para.refresh_from_db()
        self.assertEqual(self.batch_para.quantity_available, initial_stock)

    # ─────────────────────────────────────────────────────────────────────────
    # 7. Stock Addition Confirmation Generation (Safety Gating)
    # ─────────────────────────────────────────────────────────────────────────
    def test_stock_addition_confirmation_generation(self):
        """Pharmacist saying 'Add 50 Paracetamol tablets' returns confirmation token without mutating DB."""
        self.client.force_login(self.pharmacist)
        initial_stock = self.batch_para.quantity_available

        response = self.client.post(
            reverse('api_voice_assistant'),
            data=json.dumps({'query': 'Add 50 Paracetamol tablets'}),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['action'], 'CONFIRMATION_REQUIRED')
        self.assertIn('Please confirm', data['speech'])
        self.assertIsNotNone(data.get('confirmation_token'))
        self.assertEqual(data['details']['quantity'], 50)
        self.assertEqual(data['details']['projected_stock'], 150)

        # Verify DB stock was strictly untouched
        self.batch_para.refresh_from_db()
        self.assertEqual(self.batch_para.quantity_available, initial_stock)

    # ─────────────────────────────────────────────────────────────────────────
    # 8. Stock Addition Cancellation
    # ─────────────────────────────────────────────────────────────────────────
    def test_stock_addition_cancellation(self):
        """Cancelling a confirmation token invalidates token and leaves stock unchanged."""
        self.client.force_login(self.pharmacist)
        gen_res = self.client.post(
            reverse('api_voice_assistant'),
            data=json.dumps({'query': 'Add 50 Paracetamol tablets'}),
            content_type='application/json'
        )
        token = gen_res.json()['confirmation_token']

        # Cancel action
        cancel_res = self.client.post(
            reverse('api_voice_assistant'),
            data=json.dumps({
                'action': 'CANCEL_STOCK_ACTION',
                'confirmation_token': token
            }),
            content_type='application/json'
        )
        self.assertEqual(cancel_res.status_code, 200)
        data = cancel_res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['action'], 'CANCELLED')
        self.assertIn('cancelled', data['speech'].lower())

        # Verify token was purged from cache
        self.assertIsNone(cache.get(f"voice_confirm_{token}"))
        self.batch_para.refresh_from_db()
        self.assertEqual(self.batch_para.quantity_available, 100)

    # ─────────────────────────────────────────────────────────────────────────
    # 9. Confirmed Stock Addition Updating Inventory & Logging
    # ─────────────────────────────────────────────────────────────────────────
    def test_confirmed_stock_addition_updating_inventory(self):
        """Confirming valid token executes add_stock, updates batch & inv, logs AuditLog."""
        self.client.force_login(self.pharmacist)
        gen_res = self.client.post(
            reverse('api_voice_assistant'),
            data=json.dumps({'query': 'Add 50 Paracetamol tablets'}),
            content_type='application/json'
        )
        token = gen_res.json()['confirmation_token']

        # Confirm action
        confirm_res = self.client.post(
            reverse('api_voice_assistant'),
            data=json.dumps({
                'action': 'CONFIRM_STOCK_ACTION',
                'confirmation_token': token
            }),
            content_type='application/json'
        )
        self.assertEqual(confirm_res.status_code, 200)
        data = confirm_res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['updated_stock'], 150)
        self.assertIn('Stock successfully updated', data['speech'])

        # Verify DB updates
        self.batch_para.refresh_from_db()
        self.assertEqual(self.batch_para.quantity_available, 150)
        self.inv_para.refresh_from_db()
        self.assertEqual(self.inv_para.current_stock, 150)

        # Verify StockTransaction
        txn = StockTransaction.objects.filter(
            medicine=self.med_para,
            transaction_type=StockTransaction.TransactionType.ADJUSTMENT
        ).first()
        self.assertIsNotNone(txn)
        self.assertEqual(txn.quantity, 50)
        self.assertEqual(txn.performed_by, self.pharmacist)

        # Verify AuditLog
        audit = AuditLog.objects.filter(
            action_type=AuditLog.ActionType.INVENTORY_ADJUSTED,
            performed_by=self.pharmacist,
            object_id=self.med_para.id
        ).first()
        self.assertIsNotNone(audit)

    # ─────────────────────────────────────────────────────────────────────────
    # 10. Duplicate Command Idempotency
    # ─────────────────────────────────────────────────────────────────────────
    def test_duplicate_command_idempotency(self):
        """Re-submitting an already confirmed token fails cleanly without duplicate addition."""
        self.client.force_login(self.pharmacist)
        gen_res = self.client.post(
            reverse('api_voice_assistant'),
            data=json.dumps({'query': 'Add 50 Paracetamol tablets'}),
            content_type='application/json'
        )
        token = gen_res.json()['confirmation_token']

        # Confirm 1st time (succeeds)
        self.client.post(
            reverse('api_voice_assistant'),
            data=json.dumps({'action': 'CONFIRM_STOCK_ACTION', 'confirmation_token': token}),
            content_type='application/json'
        )
        self.batch_para.refresh_from_db()
        self.assertEqual(self.batch_para.quantity_available, 150)

        # Confirm 2nd time (must fail gracefully)
        dup_res = self.client.post(
            reverse('api_voice_assistant'),
            data=json.dumps({'action': 'CONFIRM_STOCK_ACTION', 'confirmation_token': token}),
            content_type='application/json'
        )
        self.assertEqual(dup_res.status_code, 200)
        dup_data = dup_res.json()
        self.assertFalse(dup_data['success'])
        self.assertEqual(dup_data['error'], 'EXPIRED_OR_INVALID_TOKEN')

        # Stock must remain 150 (not 200)
        self.batch_para.refresh_from_db()
        self.assertEqual(self.batch_para.quantity_available, 150)

    # ─────────────────────────────────────────────────────────────────────────
    # 11. Purchase Request Creation via Voice
    # ─────────────────────────────────────────────────────────────────────────
    def test_purchase_request_creation_via_voice(self):
        """Spoken: 'Create a purchase request for 100 units of Paracetamol'"""
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('api_voice_assistant'),
            data=json.dumps({'query': 'Create a purchase request for 100 units of Paracetamol'}),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['intent'], 'CREATE_PURCHASE_REQUEST')
        self.assertIn('Purchase request', data['speech'])

        # Verify PR exists in database in PENDING_APPROVAL status
        pr = PurchaseRequest.objects.filter(
            items__medicine=self.med_para,
            status=PurchaseRequest.PRStatus.PENDING_APPROVAL
        ).first()
        self.assertIsNotNone(pr)
        item = pr.items.filter(medicine=self.med_para).first()
        self.assertEqual(item.requested_qty, 100)

    # ─────────────────────────────────────────────────────────────────────────
    # 12. Out of Stock Query
    # ─────────────────────────────────────────────────────────────────────────
    def test_out_of_stock_query(self):
        """Spoken: 'Find medicines that are out of stock.'"""
        self.client.force_login(self.pharmacist)
        response = self.client.post(
            reverse('api_voice_assistant'),
            data=json.dumps({'query': 'Find medicines that are out of stock'}),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['intent'], 'CHECK_OUT_OF_STOCK')
        self.assertGreaterEqual(data['count'], 1)
        self.assertIn('Cetirizine', data['speech'])
        self.assertEqual(data['action_url'], '/inventory/?status=OUT_OF_STOCK')

    # ─────────────────────────────────────────────────────────────────────────
    # 13. Stock Movements Query
    # ─────────────────────────────────────────────────────────────────────────
    def test_stock_movements_query(self):
        """Spoken: 'Show today's stock movements.'"""
        self.client.force_login(self.pharmacist)
        # Create a sample transaction today
        StockTransaction.objects.create(
            transaction_number='TX-TEST-001',
            inventory=self.inv_para,
            medicine=self.med_para,
            transaction_type=StockTransaction.TransactionType.ADJUSTMENT,
            quantity=10,
            previous_stock=90,
            new_stock=100,
            performed_by=self.pharmacist
        )

        response = self.client.post(
            reverse('api_voice_assistant'),
            data=json.dumps({'query': "Show today's stock movements"}),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['intent'], 'STOCK_MOVEMENTS')
        self.assertGreaterEqual(data['total_transactions'], 1)
        self.assertEqual(data['action_url'], '/inventory/history/')

    # ─────────────────────────────────────────────────────────────────────────
    # 14. Pending Purchase Requests Query
    # ─────────────────────────────────────────────────────────────────────────
    def test_pending_purchase_requests_query(self):
        """Spoken: 'Show pending purchase requests.'"""
        self.client.force_login(self.pharmacist)
        # Create a pending PR
        pr = PurchaseRequest.objects.create(
            request_id='PR-TEST-001',
            status=PurchaseRequest.PRStatus.PENDING_APPROVAL,
            created_by=self.pharmacist,
            notes='Test pending PR'
        )
        PurchaseRequestItem.objects.create(
            purchase_request=pr,
            medicine=self.med_para,
            requested_qty=50
        )

        response = self.client.post(
            reverse('api_voice_assistant'),
            data=json.dumps({'query': 'Show pending purchase requests'}),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['intent'], 'PENDING_PURCHASE_REQUESTS')
        self.assertGreaterEqual(data['pending_count'], 1)
        self.assertEqual(data['action_url'], '/purchases/requests/')

    # ─────────────────────────────────────────────────────────────────────────
    # 15. Medicines Needing Reordering Query
    # ─────────────────────────────────────────────────────────────────────────
    def test_medicines_needing_reordering_query(self):
        """Spoken: 'Show medicines that need reordering.'"""
        self.client.force_login(self.pharmacist)
        AIRecommendation.objects.create(
            recommendation_type=AIRecommendation.RecommendationType.REORDER,
            status=AIRecommendation.Status.PENDING,
            medicine=self.med_amox,
            title='Low Stock Reorder',
            message='Amoxicillin is below reorder level.'
        )

        response = self.client.post(
            reverse('api_voice_assistant'),
            data=json.dumps({'query': 'Show medicines that need reordering'}),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['intent'], 'REORDER_RECOMMENDATIONS')
        self.assertGreaterEqual(data['count'], 1)
        self.assertIn('Amoxicillin', data['speech'])

    # ─────────────────────────────────────────────────────────────────────────
    # 16. Navigation Commands
    # ─────────────────────────────────────────────────────────────────────────
    def test_navigation_commands(self):
        """Spoken: 'Navigate to expiry management'"""
        self.client.force_login(self.pharmacist)
        response = self.client.post(
            reverse('api_voice_assistant'),
            data=json.dumps({'query': 'Navigate to expiry management'}),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['intent'], 'NAVIGATION')
        self.assertEqual(data['action_url'], '/inventory/expiry/')

    # ─────────────────────────────────────────────────────────────────────────
    # 17. Security & Unauthenticated Access Gating
    # ─────────────────────────────────────────────────────────────────────────
    def test_unauthenticated_access_rejected(self):
        """Unauthenticated POST to voice assistant API returns 401."""
        self.client.logout()
        response = self.client.post(
            reverse('api_voice_assistant'),
            data=json.dumps({'query': 'How many Paracetamol tablets are available?'}),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 401)
        data = response.json()
        self.assertFalse(data['success'])

    def test_empty_query_handling(self):
        """Empty query returns friendly prompt without crashing."""
        self.client.force_login(self.pharmacist)
        response = self.client.post(
            reverse('api_voice_assistant'),
            data=json.dumps({'query': '   '}),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn("didn't catch that", data['speech'].lower())
