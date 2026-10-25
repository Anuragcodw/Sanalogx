"""
inventory/tests.py
MediVault — Inventory Intelligence & Consumption Unit and Integration Tests
"""

from decimal import Decimal
from datetime import timedelta, date
from django.test import TestCase, Client
from django.utils import timezone
from django.urls import reverse

from accounts.models import User
from medicines.models import Medicine, MedicineBatch, Category
from inventory.models import Inventory, StockTransaction, ConsumptionCache, StockAlert
from inventory import consumption_service, health_service, historical_import


class InventoryIntelligenceTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_superuser(
            username='admin_test',
            email='admin@medivault.com',
            password='Password123!',
            role='ADMIN',
            status='APPROVED',
        )
        self.pharmacist = User.objects.create_user(
            username='pharm_test',
            email='pharm@medivault.com',
            password='Password123!',
            role='PHARMACIST',
            status='APPROVED',
        )

        self.category = Category.objects.create(name='Antibiotics')
        self.med = Medicine.objects.create(
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
            status='ACTIVE',
        )
        self.inv = Inventory.objects.create(
            medicine=self.med,
            current_stock=100,
            available_stock=100,
            reorder_level=50,
            safety_stock=20,
            minimum_stock=10,
            maximum_stock=500,
            stock_status='NORMAL',
        )
        self.batch = MedicineBatch.objects.create(
            medicine=self.med,
            batch_number='AMX-2026-01',
            expiry_date=timezone.now().date() + timedelta(days=180),
            purchase_price=Decimal('20.00'),
            selling_price=Decimal('30.00'),
            quantity_received=100,
            quantity_available=100,
            status='ACTIVE',
        )

    def test_consumption_calculation(self):
        """Test that only SALE and DISPENSE transactions count towards consumption."""
        StockTransaction.objects.create(
            inventory=self.inv,
            medicine=self.med,
            transaction_type='SALE',
            quantity=15,
            previous_stock=100,
            new_stock=85,
            performed_by=self.pharmacist,
        )
        StockTransaction.objects.create(
            inventory=self.inv,
            medicine=self.med,
            transaction_type='DISPENSE',
            quantity=10,
            previous_stock=85,
            new_stock=75,
            performed_by=self.pharmacist,
        )
        # Damage should NOT count towards consumption
        StockTransaction.objects.create(
            inventory=self.inv,
            medicine=self.med,
            transaction_type='DAMAGE',
            quantity=5,
            previous_stock=75,
            new_stock=70,
            performed_by=self.pharmacist,
        )

        consumed_30d = consumption_service.get_consumption_for_period(self.med, 30)
        self.assertEqual(consumed_30d, 25)

        analytics = consumption_service.get_consumption_analytics(self.med)
        self.assertEqual(analytics['consumed_30d'], 25)
        self.assertAlmostEqual(analytics['daily_avg_30d'], round(25 / 30.0, 2))

    def test_days_of_stock_and_health_classification(self):
        """Test calculation of Days of Stock Remaining and health state."""
        # Create 30 units consumption over 30 days -> 1 unit/day
        StockTransaction.objects.create(
            inventory=self.inv,
            medicine=self.med,
            transaction_type='SALE',
            quantity=30,
            previous_stock=100,
            new_stock=70,
            performed_by=self.pharmacist,
        )

        dos = health_service.get_days_of_stock_remaining(self.med)
        self.assertAlmostEqual(dos, 100.0)

        health = health_service.classify_inventory_health(self.med)
        self.assertEqual(health['status'], health_service.HealthStatus.NORMAL)

        # Drop stock to critical level (<= safety stock of 20)
        self.inv.current_stock = 15
        self.inv.save()
        health_crit = health_service.classify_inventory_health(self.med)
        self.assertEqual(health_crit['status'], health_service.HealthStatus.CRITICAL)

    def test_historical_data_import(self):
        """Test validating and executing historical consumption import."""
        raw_rows = [
            {'medicine': 'Amoxicillin 500mg', 'quantity': '20', 'date': '2026-08-01', 'transaction_type': 'SALE'},
            {'medicine': 'Amoxicillin 500mg', 'quantity': '15', 'date': '2026-08-05', 'transaction_type': 'DISPENSE'},
            {'medicine': 'NonExistentMedicine', 'quantity': '10', 'date': '2026-08-10'},
        ]
        preview = historical_import.validate_and_preview_import(raw_rows)
        self.assertEqual(preview['valid_count'], 2)
        self.assertEqual(preview['error_count'], 1)

        count = historical_import.execute_historical_import(preview['preview_rows'], self.admin)
        self.assertEqual(count, 2)

        cache = ConsumptionCache.objects.filter(medicine=self.med).first()
        self.assertIsNotNone(cache)

    def test_consumption_analytics_view_access(self):
        """Test HTTP view access for approved pharmacist."""
        self.client.force_login(self.pharmacist)
        res = self.client.get(reverse('inventory_consumption_analytics'))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Consumption Analytics')

    def test_medicine_detail_view_intelligence_context(self):
        """Test that medicine detail view provides all intelligence metrics."""
        self.client.force_login(self.pharmacist)
        res = self.client.get(reverse('medicine_inventory_detail', kwargs={'pk': self.med.pk}))
        self.assertEqual(res.status_code, 200)
        self.assertIn('consumption_analytics', res.context)
        self.assertIn('multi_forecast', res.context)
        self.assertIn('health_info', res.context)

    def test_inventory_dashboard_ui_redesign_and_kpi_metrics(self):
        """Verify redesigned Inventory Dashboard layout, 8 KPI cards, optimization flow, and AI widget."""
        self.client.force_login(self.admin)
        res = self.client.get(reverse('inventory_dashboard'))
        self.assertEqual(res.status_code, 200)

        # Header & Actions
        self.assertContains(res, 'Inventory &amp; Stock Management')
        self.assertContains(res, 'Monitor medicine availability, batch-level stock, expiry risk, and stock movements.')
        self.assertContains(res, 'Add Stock')
        self.assertContains(res, 'Adjust Stock')
        self.assertContains(res, 'Record Damage')

        # 8 KPI metrics
        self.assertContains(res, 'Total Medicines')
        self.assertContains(res, 'Total Stock Units')
        self.assertContains(res, 'Normal Stock')
        self.assertContains(res, 'Low Stock')
        self.assertContains(res, 'Out of Stock')
        self.assertContains(res, 'Overstock')
        self.assertContains(res, 'Near Expiry')
        self.assertContains(res, 'Expired Batches')

        # Continuous Inventory Optimization Flow steps (2 rows)
        self.assertContains(res, 'Continuous Inventory Optimization Flow')
        self.assertContains(res, 'Batch-wise stock &amp; availability')
        self.assertContains(res, 'Sales &amp; dispense deduplication')
        self.assertContains(res, 'Expected 30-day demand')
        self.assertContains(res, 'Low stock, stockout &amp; expiry')
        self.assertContains(res, 'AI-suggested quantities')
        self.assertContains(res, 'Price, lead time &amp; stock')
        self.assertContains(res, 'Human approval required')
        self.assertContains(res, 'Approved procurement')
        self.assertContains(res, 'In-transit monitoring')
        self.assertContains(res, 'Verify batch &amp; expiry')
        self.assertContains(res, 'Atomic FEFO batch entry')
        self.assertContains(res, 'Forecasts refresh automatically')

        # Reorder Recommendations button retained & integrated
        self.assertContains(res, reverse('inventory_reorder_recommendations'))

        # Inventory Table columns
        self.assertContains(res, 'Medicine Inventory')
        self.assertContains(res, 'Current Stock')
        self.assertContains(res, 'Available')
        self.assertContains(res, 'Reorder Level')

        # MediVoice AI Assistant
        self.assertContains(res, 'medivoice-floating-container')
        self.assertContains(res, 'Sanalogx AI Assistant')

    def test_inventory_dashboard_empty_states_when_no_records(self):
        """Verify clean empty states when inventory is empty."""
        MedicineBatch.objects.all().delete()
        Inventory.objects.all().delete()
        Medicine.objects.all().delete()
        StockAlert.objects.all().delete()

        self.client.force_login(self.admin)
        res = self.client.get(reverse('inventory_dashboard'))
        self.assertEqual(res.status_code, 200)

        # Empty distribution
        self.assertContains(res, 'Distribution will appear after stock records are created.')
        # Empty alerts
        self.assertContains(res, 'No active stock alerts.')
        # Empty table
        self.assertContains(res, 'No inventory records found')
        self.assertContains(res, 'Inventory records will appear after medicine batches and stock transactions are recorded.')

    def test_inventory_dashboard_search_and_filter(self):
        """Verify search query and status filters work on the inventory table."""
        self.client.force_login(self.pharmacist)
        # Search match
        res = self.client.get(reverse('inventory_dashboard') + '?search=Amoxicillin')
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Amoxicillin 500mg')

        # Search no match shows filter-aware empty state
        res_empty = self.client.get(reverse('inventory_dashboard') + '?search=NonExistentMedicine999')
        self.assertEqual(res_empty.status_code, 200)
        self.assertContains(res_empty, 'No records match the current filters.')

    def test_expiry_service_classification_thresholds(self):
        """Verify strict non-overlapping expiry boundary conditions."""
        from inventory.services import get_batch_expiry_info, ExpiryRiskCategory
        today = timezone.now().date()

        # Batch 1: Expired (-10 days)
        b_exp = MedicineBatch(expiry_date=today - timedelta(days=10), quantity_available=20, status='EXPIRED')
        info_exp = get_batch_expiry_info(b_exp, today=today)
        self.assertEqual(info_exp['risk_category'], ExpiryRiskCategory.EXPIRED)
        self.assertTrue(info_exp['is_expired'])
        self.assertFalse(info_exp['is_dispensable'])
        self.assertEqual(info_exp['badge_class'], 'danger')

        # Batch 2: Expires Today (0 days)
        b_today = MedicineBatch(expiry_date=today, quantity_available=15, status='EXPIRED')
        info_today = get_batch_expiry_info(b_today, today=today)
        self.assertEqual(info_today['risk_category'], ExpiryRiskCategory.EXPIRED)
        self.assertTrue(info_today['is_expired'])
        self.assertFalse(info_today['is_dispensable'])

        # Batch 3: Critical boundary (25 days left)
        b_crit = MedicineBatch(expiry_date=today + timedelta(days=25), quantity_available=30, status='ACTIVE')
        info_crit = get_batch_expiry_info(b_crit, today=today)
        self.assertEqual(info_crit['risk_category'], ExpiryRiskCategory.CRITICAL)
        self.assertFalse(info_crit['is_expired'])
        self.assertTrue(info_crit['is_dispensable'])
        self.assertEqual(info_crit['badge_class'], 'danger')

        # Batch 4: Warning boundary (45 days left)
        b_warn = MedicineBatch(expiry_date=today + timedelta(days=45), quantity_available=40, status='ACTIVE')
        info_warn = get_batch_expiry_info(b_warn, today=today)
        self.assertEqual(info_warn['risk_category'], ExpiryRiskCategory.WARNING)
        self.assertFalse(info_warn['is_expired'])
        self.assertTrue(info_warn['is_dispensable'])
        self.assertEqual(info_warn['badge_class'], 'warning')

        # Batch 5: Near Expiry boundary (75 days left)
        b_near = MedicineBatch(expiry_date=today + timedelta(days=75), quantity_available=50, status='ACTIVE')
        info_near = get_batch_expiry_info(b_near, today=today)
        self.assertEqual(info_near['risk_category'], ExpiryRiskCategory.NEAR_EXPIRY)
        self.assertFalse(info_near['is_expired'])
        self.assertTrue(info_near['is_dispensable'])
        self.assertEqual(info_near['badge_class'], 'info')

        # Batch 6: Normal (> 90 days, e.g. 150 days)
        b_norm = MedicineBatch(expiry_date=today + timedelta(days=150), quantity_available=60, status='ACTIVE')
        info_norm = get_batch_expiry_info(b_norm, today=today)
        self.assertEqual(info_norm['risk_category'], ExpiryRiskCategory.NORMAL)
        self.assertFalse(info_norm['is_expired'])
        self.assertTrue(info_norm['is_dispensable'])
        self.assertEqual(info_norm['badge_class'], 'success')

        # Batch 7: Missing expiry date
        b_none = MedicineBatch(expiry_date=None, quantity_available=10, status='ACTIVE')
        info_none = get_batch_expiry_info(b_none, today=today)
        self.assertEqual(info_none['risk_category'], ExpiryRiskCategory.UNKNOWN)
        self.assertFalse(info_none['is_dispensable'])
        self.assertEqual(info_none['badge_class'], 'secondary')

    def test_expiry_service_kpis_segregation(self):
        """Verify that expired batches are strictly segregated and not counted in near-expiry."""
        from inventory.services import get_expiry_kpis
        today = timezone.now().date()

        MedicineBatch.objects.create(
            medicine=self.med,
            batch_number='AMX-EXP-01',
            expiry_date=today - timedelta(days=5),
            quantity_received=50,
            quantity_available=25,
            purchase_price=Decimal('20.00'),
            selling_price=Decimal('30.00'),
            status='EXPIRED',
        )
        MedicineBatch.objects.create(
            medicine=self.med,
            batch_number='AMX-CRIT-01',
            expiry_date=today + timedelta(days=15),
            quantity_received=50,
            quantity_available=30,
            purchase_price=Decimal('20.00'),
            selling_price=Decimal('30.00'),
            status='NEAR_EXPIRY',
        )

        kpis = get_expiry_kpis(today=today, near_expiry_days=90)
        self.assertEqual(kpis['expired_count'], 1)
        self.assertEqual(kpis['critical_count'], 1)
        # Near expiry must include critical and warning batches but NEVER expired ones
        self.assertGreaterEqual(kpis['near_expiry_count'], 1)
        self.assertTrue(kpis['total_batches'] >= 3)

    def test_expiry_management_view(self):
        """Verify expiry management view renders with filters, KPIs, and export link."""
        self.client.force_login(self.admin)
        res = self.client.get(reverse('expiry_management') + '?days=60')
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Expiry Management')
        self.assertContains(res, '/reports/preview/')
        self.assertContains(res, 'Near Expiry Batches')
        self.assertContains(res, 'Expired Batches')

    def test_batch_stock_view_with_kpis(self):
        """Verify batch_stock_view uses centralized KPIs and displays batches."""
        self.client.force_login(self.pharmacist)
        res = self.client.get(reverse('batch_stock'))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Batch-wise Stock Ledger')
        self.assertIn('total_batches', res.context)
        self.assertIn('active_batches', res.context)
        self.assertIn('near_expiry_count', res.context)
        self.assertIn('expired_count', res.context)

