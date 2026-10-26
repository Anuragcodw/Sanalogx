"""
ai_engine/tests.py
MediVault — AI Engine & Recommendation Center Unit and Integration Tests
"""

import json
from decimal import Decimal
from datetime import timedelta
from django.test import TestCase, Client
from django.utils import timezone
from django.urls import reverse

from accounts.models import User
from medicines.models import Medicine, MedicineBatch, Category
from inventory.models import Inventory, StockTransaction
from purchases.models import PurchaseOrder, PurchaseOrderItem
from suppliers.models import Supplier, SupplierMedicine
from ai_engine import intelligence_service
from ai_engine.models import AIRecommendation


class AIEngineTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_superuser(
            username='admin_ai',
            email='admin@medivault.com',
            password='Password123!',
            role='ADMIN',
            status='APPROVED',
        )
        self.pharmacist = User.objects.create_user(
            username='pharm_ai',
            email='pharm@medivault.com',
            password='Password123!',
            role='PHARMACIST',
            status='APPROVED',
        )

        self.category = Category.objects.create(name='Analgesics')
        self.med = Medicine.objects.create(
            medicine_name='Paracetamol 500mg',
            generic_name='Paracetamol',
            category=self.category,
            dosage_form='Tablet',
            strength='500mg',
            unit_of_measure='Strip',
            reorder_level=50,
            safety_stock=20,
            minimum_stock=10,
            maximum_stock=500,
            status='ACTIVE',
        )
        self.inv = Inventory.objects.create(
            medicine=self.med,
            current_stock=15,  # below reorder level (50)
            available_stock=15,
            reorder_level=50,
            safety_stock=20,
            minimum_stock=10,
            maximum_stock=500,
            stock_status='LOW_STOCK',
        )
        self.supplier = Supplier.objects.create(
            company_name='Apex Pharma',
            status='ACTIVE',
            rating=Decimal('4.5'),
        )
        self.sm = SupplierMedicine.objects.create(
            supplier=self.supplier,
            medicine=self.med,
            unit_price=Decimal('10.00'),
            delivery_days=3,
            is_available=True,
        )

    def test_multi_horizon_demand_forecast(self):
        """Test multi-horizon demand forecasting calculation."""
        # Create some sales transactions
        for _ in range(5):
            StockTransaction.objects.create(
                inventory=self.inv,
                medicine=self.med,
                transaction_type='SALE',
                quantity=10,
                previous_stock=50,
                new_stock=40,
                performed_by=self.pharmacist,
            )

        forecast = intelligence_service.get_demand_forecast_multi_horizon(self.med)
        self.assertIn('forecast_7d', forecast)
        self.assertIn('forecast_14d', forecast)
        self.assertIn('forecast_30d', forecast)
        self.assertIn('forecast_60d', forecast)
        self.assertIn('forecast_90d', forecast)
        self.assertGreater(forecast['forecast_30d'], 0)

    def test_open_po_aware_reorder_recommendations(self):
        """Test that pending open POs reduce/account for reorder quantity."""
        # Baseline reorders without open PO
        reorders = intelligence_service.get_reorder_recommendations()
        self.assertTrue(any(r['medicine'].id == self.med.id for r in reorders))

        # Create an open Purchase Order for 500 units (covers entire shortfall)
        po = PurchaseOrder.objects.create(
            po_number='PO-2026-TEST',
            supplier=self.supplier,
            status='APPROVED',
            created_by=self.admin,
        )
        PurchaseOrderItem.objects.create(
            purchase_order=po,
            medicine=self.med,
            quantity_ordered=500,
            quantity_pending=500,
            unit_price=Decimal('10.00'),
        )

        reorders_after_po = intelligence_service.get_reorder_recommendations()
        # Should now be skipped because effective_available (15 + 500) > reorder_level
        self.assertFalse(any(r['medicine'].id == self.med.id for r in reorders_after_po))

    def test_unapproved_pos_do_not_count_as_incoming_stock(self):
        for status in (PurchaseOrder.POStatus.DRAFT, PurchaseOrder.POStatus.SUBMITTED):
            po = PurchaseOrder.objects.create(
                po_number=f'PO-UNAPPROVED-{status}',
                supplier=self.supplier,
                status=status,
                created_by=self.admin,
            )
            PurchaseOrderItem.objects.create(
                purchase_order=po,
                medicine=self.med,
                quantity_ordered=500,
                quantity_pending=500,
                unit_price=Decimal('10.00'),
            )

        recommendations = intelligence_service.get_reorder_recommendations()
        recommendation = next(r for r in recommendations if r['medicine'].id == self.med.id)
        self.assertEqual(recommendation['open_po_qty'], 0)

    def test_reorder_quantity_respects_maximum_stock(self):
        self.inv.maximum_stock = 50
        self.inv.save()

        recommendations = intelligence_service.get_reorder_recommendations()
        recommendation = next(r for r in recommendations if r['medicine'].id == self.med.id)
        self.assertLessEqual(
            recommendation['current_stock'] + recommendation['recommended_qty'],
            self.inv.maximum_stock,
        )

    def test_recommendation_center_action(self):
        """Test accepting/rejecting a recommendation via POST."""
        rec = AIRecommendation.objects.create(
            recommendation_type=AIRecommendation.RecommendationType.REORDER,
            medicine=self.med,
            title='Reorder Paracetamol',
            message='Stock is low',
            status=AIRecommendation.Status.PENDING,
        )

        self.client.force_login(self.admin)
        res = self.client.post(
            reverse('ai_recommendation_action', kwargs={'pk': rec.pk}),
            {'action': 'ACCEPT'}
        )
        self.assertEqual(res.status_code, 302)
        rec.refresh_from_db()
        self.assertEqual(rec.status, AIRecommendation.Status.ACCEPTED)

    def test_chatbot_inventory_intelligence_api(self):
        """Test database-connected chatbot answering consumption and stock queries."""
        self.client.force_login(self.pharmacist)
        res = self.client.post(
            reverse('api_ai_chat'),
            data=json.dumps({'message': 'What is the consumption velocity of Paracetamol?'}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertIn('Paracetamol', data['answer'])

    def test_ai_recommendation_center_view_renders_without_url_error(self):
        """Verify /ai-intelligence/recommendations/ renders with HTTP 200 and no NoReverseMatch."""
        self.client.force_login(self.admin)
        res = self.client.get(reverse('ai_recommendation_center'))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'AI Recommendation Center')
        self.assertContains(res, reverse('purchase_orders_list'))
        self.assertContains(res, reverse('ai_dashboard'))
        self.assertContains(res, 'medivoice-floating-container')
        self.assertContains(res, 'Sanalogx AI Assistant')

    def test_purchases_dashboard_and_orders_url_reversal(self):
        """Verify URL routing for procurement dashboard and purchase order aliases."""
        url_dashboard = reverse('purchases_dashboard')
        url_orders = reverse('purchase_orders_list')
        url_po_list = reverse('po_list')

        self.assertEqual(url_orders, '/purchases/')
        self.assertEqual(url_po_list, '/purchases/po-list/')
        self.assertEqual(url_dashboard, '/purchases/dashboard/')

        # Ensure both admin and pharmacist can access procurement
        self.client.force_login(self.admin)
        res_admin = self.client.get(url_dashboard)
        self.assertEqual(res_admin.status_code, 200)

        self.client.force_login(self.pharmacist)
        res_pharm = self.client.get(url_dashboard)
        self.assertEqual(res_pharm.status_code, 200)

    def test_global_ai_assistant_presence_across_authenticated_dashboards(self):
        """Verify the MediVault AI Assistant launcher is mounted across authenticated dashboard areas."""
        self.client.force_login(self.admin)
        endpoints = [
            reverse('ai_recommendation_center'),
            reverse('ai_dashboard'),
            reverse('inventory_dashboard'),
            reverse('medicine_list'),
            reverse('purchase_orders_list'),
            reverse('admin_approvals'),
        ]
        for url in endpoints:
            with self.subTest(url=url):
                res = self.client.get(url)
                self.assertEqual(res.status_code, 200)
                self.assertContains(res, 'medivoice-floating-container')
                self.assertContains(res, 'medivoice-trigger-btn')
                self.assertContains(res, 'title="Sanalogx AI Assistant"')

    def test_ai_assistant_absent_for_unauthenticated_users(self):
        """Verify the AI Assistant widget is hidden on public authentication pages."""
        res = self.client.get(reverse('pharmacist_login'))
        self.assertEqual(res.status_code, 200)
        self.assertNotContains(res, 'medivoice-floating-container')

