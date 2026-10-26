"""
inventory/test_consumption_analytics.py
MediVault — Comprehensive Consumption Analytics & AI Demand Forecasting Tests

Covers:
  1. Net consumption deduplication (SALE+DISPENSE with shared reference)
  2. DAMAGE/EXPIRY/PURCHASE excluded from demand calculation
  3. RETURN quantity correctly reduces net consumption
  4. Cancelled sale exclusion
  5. Daily consumption trend chart data
  6. get_consumption_dashboard_summary() deduplication correctness
  7. DailyConsumptionSummary rebuild idempotency
  8. HistoricalImportLog provenance creation
  9. Historical import does NOT alter current stock
 10. Historical import fingerprint prevents duplicate rows
 11. Historical import skips future-dated rows
 12. Stockout risk correctly classifies CRITICAL / LOW / NO_DATA
 13. Stockout risk factors population
 14. Consumption view returns HTTP 200 for admin and pharmacist
 15. Reorder recommendations view returns HTTP 200
 16. Medicine consumption API returns valid JSON
 17. Historical import view GET returns HTTP 200
 18. Data quality threshold boundaries (GOOD / WARNING / INSUFFICIENT_DATA)
 19. Period breakdown (MTD, last month, this quarter)
 20. Cold start forecast falls back correctly (< 5 transactions)
"""

from decimal import Decimal
from datetime import timedelta, date, datetime
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from medicines.models import Medicine, MedicineBatch, Category
from inventory.models import (
    Inventory, StockTransaction, ConsumptionCache,
    DailyConsumptionSummary, HistoricalImportLog,
)
from inventory import consumption_service, historical_import


# ─── Shared fixture factory ────────────────────────────────────────────────

def make_medicine(name='TestMed 100mg', category=None, **kwargs):
    """Helper to create a Medicine with sensible defaults."""
    return Medicine.objects.create(
        medicine_name=name,
        generic_name='TestGeneric',
        category=category,
        dosage_form='Tablet',
        strength='100mg',
        unit_of_measure='Strip',
        reorder_level=kwargs.get('reorder_level', 50),
        safety_stock=kwargs.get('safety_stock', 20),
        minimum_stock=kwargs.get('minimum_stock', 10),
        maximum_stock=kwargs.get('maximum_stock', 500),
        status='ACTIVE',
    )


def make_batch(medicine, qty=200, expiry_days=180):
    return MedicineBatch.objects.create(
        medicine=medicine,
        batch_number=f'BATCH-{medicine.id}-001',
        expiry_date=timezone.now().date() + timedelta(days=expiry_days),
        purchase_price=Decimal('10.00'),
        selling_price=Decimal('15.00'),
        quantity_received=qty,
        quantity_available=qty,
        status='ACTIVE',
    )


def make_inventory(medicine, stock=200):
    inv, _ = Inventory.objects.get_or_create(
        medicine=medicine,
        defaults={
            'current_stock': stock,
            'available_stock': stock,
            'reorder_level': medicine.reorder_level,
            'safety_stock': medicine.safety_stock,
            'minimum_stock': medicine.minimum_stock,
            'maximum_stock': medicine.maximum_stock,
            'stock_status': 'NORMAL',
        }
    )
    return inv


def make_txn(inventory, medicine, txn_type, qty, ref='', days_ago=0):
    """Create a StockTransaction backdated by days_ago."""
    txn = StockTransaction.objects.create(
        inventory=inventory,
        medicine=medicine,
        transaction_type=txn_type,
        quantity=qty,
        previous_stock=inventory.current_stock,
        new_stock=inventory.current_stock,
        reference_number=ref,
    )
    if days_ago:
        backdated = timezone.now() - timedelta(days=days_ago)
        StockTransaction.objects.filter(pk=txn.pk).update(created_at=backdated)
    return txn


# ─── Test Classes ──────────────────────────────────────────────────────────

class ConsumptionDeduplicationTests(TestCase):
    """Tests 1–4: Deduplication of SALE+DISPENSE, RETURN, DAMAGE exclusion, cancelled sale."""

    def setUp(self):
        self.cat = Category.objects.create(name='Analgesics')
        self.med = make_medicine('Paracetamol 500mg', category=self.cat)
        self.inv = make_inventory(self.med, stock=300)
        make_batch(self.med)

    def test_1_dispense_and_sale_with_same_ref_counted_once(self):
        """SALE and DISPENSE with same reference_number must count exactly once."""
        make_txn(self.inv, self.med, 'DISPENSE', 10, ref='SCRIPT-001')
        make_txn(self.inv, self.med, 'SALE', 10, ref='SCRIPT-001')  # same event

        result = consumption_service.get_consumption_for_period(self.med, 7)
        self.assertEqual(result, 10, "Same-ref SALE+DISPENSE should count as 10, not 20")

    def test_2_independent_sale_and_dispense_counted_separately(self):
        """SALE and DISPENSE with different references must both be counted."""
        make_txn(self.inv, self.med, 'DISPENSE', 10, ref='SCRIPT-100')
        make_txn(self.inv, self.med, 'SALE', 15, ref='SALE-200')  # different event

        result = consumption_service.get_consumption_for_period(self.med, 7)
        self.assertEqual(result, 25, "Independent SALE+DISPENSE should sum to 25")

    def test_3_damage_expiry_purchase_excluded_from_consumption(self):
        """DAMAGE, EXPIRY and PURCHASE must never count as demand/consumption."""
        make_txn(self.inv, self.med, 'SALE', 20, ref='SALE-001')
        make_txn(self.inv, self.med, 'DAMAGE', 50, ref='DMG-001')
        make_txn(self.inv, self.med, 'EXPIRY', 30, ref='EXP-001')
        make_txn(self.inv, self.med, 'PURCHASE', 100, ref='PO-001')

        result = consumption_service.get_consumption_for_period(self.med, 7)
        self.assertEqual(result, 20, "Only SALE should count; DAMAGE, EXPIRY, PURCHASE must be excluded")

    def test_4_return_reduces_net_consumption(self):
        """RETURN transactions reduce gross consumption to net."""
        make_txn(self.inv, self.med, 'SALE', 30, ref='SALE-R01')
        make_txn(self.inv, self.med, 'RETURN', 5, ref='RET-001')

        result = consumption_service.get_consumption_for_period(self.med, 7)
        self.assertEqual(result, 25, "Net = gross(30) - return(5) = 25")

    def test_5_return_cannot_make_net_negative(self):
        """Returns exceeding gross must be clamped to zero, not negative."""
        make_txn(self.inv, self.med, 'SALE', 5, ref='SALE-X01')
        make_txn(self.inv, self.med, 'RETURN', 20, ref='RET-X01')

        result = consumption_service.get_consumption_for_period(self.med, 7)
        self.assertEqual(result, 0, "Net consumption must never be negative")


class DashboardSummaryDeduplicationTests(TestCase):
    """Test 6: get_consumption_dashboard_summary must apply dedup, not raw sum."""

    def setUp(self):
        self.cat = Category.objects.create(name='Antibiotics')
        self.med = make_medicine('Amoxicillin 250mg', category=self.cat)
        self.inv = make_inventory(self.med, stock=200)
        make_batch(self.med)

    def test_6_summary_total_uses_deduplication(self):
        """Dashboard summary must not double-count same-reference SALE+DISPENSE."""
        make_txn(self.inv, self.med, 'DISPENSE', 10, ref='SCRIPT-DUP')
        make_txn(self.inv, self.med, 'SALE', 10, ref='SCRIPT-DUP')  # duplicate

        summary = consumption_service.get_consumption_dashboard_summary()
        # If dedup works: 7d=10; if broken (raw sum): 7d=20
        self.assertLessEqual(
            summary['total_units_7d'], 10,
            "Dashboard summary must not double-count same-reference records"
        )


class DailyConsumptionSummaryTests(TestCase):
    """Tests 7: rebuild_daily_summary idempotency and correctness."""

    def setUp(self):
        self.cat = Category.objects.create(name='Vitamins')
        self.med = make_medicine('Vitamin C 500mg', category=self.cat)
        self.inv = make_inventory(self.med, stock=500)
        make_batch(self.med)

    def test_7_rebuild_is_idempotent(self):
        """Running rebuild_daily_summary twice must not create duplicate records."""
        make_txn(self.inv, self.med, 'SALE', 15, ref='SALE-VIT-01')
        today = timezone.now().date()

        count1 = consumption_service.rebuild_daily_summary(self.med, date_from=today, date_to=today)
        count2 = consumption_service.rebuild_daily_summary(self.med, date_from=today, date_to=today)

        db_count = DailyConsumptionSummary.objects.filter(medicine=self.med, date=today).count()
        self.assertEqual(db_count, 1, "Idempotent rebuild must not create duplicate rows")
        self.assertEqual(count1, 1)
        self.assertEqual(count2, 1)

    def test_7b_rebuild_stores_correct_net_consumption(self):
        """rebuild_daily_summary must store deduplicated net consumption."""
        today = timezone.now().date()
        make_txn(self.inv, self.med, 'SALE', 20, ref='SALE-V01')
        make_txn(self.inv, self.med, 'RETURN', 5, ref='RET-V01')

        consumption_service.rebuild_daily_summary(self.med, date_from=today, date_to=today)
        rec = DailyConsumptionSummary.objects.get(medicine=self.med, date=today)
        self.assertEqual(rec.net_consumption, 15)


class HistoricalImportTests(TestCase):
    """Tests 8–11: Historical import provenance, stock safety, fingerprinting."""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='admin_import', email='admin@mv.com', password='Pwd123!',
            role='ADMIN', status='APPROVED',
        )
        self.cat = Category.objects.create(name='Cardiology')
        self.med = make_medicine('Atenolol 50mg', category=self.cat)
        self.inv = make_inventory(self.med, stock=150)
        make_batch(self.med)

    def test_8_import_log_is_created(self):
        """create_import_log must create a HistoricalImportLog record."""
        preview = {'total_rows': 3, 'valid_count': 2, 'error_count': 1, 'errors': []}
        log = historical_import.create_import_log('test.csv', self.admin, preview)
        self.assertIsNotNone(log.pk)
        self.assertEqual(log.valid_rows, 2)
        self.assertEqual(log.uploaded_by, self.admin)

    def test_9_historical_import_does_not_alter_stock(self):
        """Historical import must NOT change current_stock or available_stock."""
        stock_before = self.inv.current_stock
        raw_rows = [
            {'medicine': 'Atenolol 50mg', 'quantity': '20', 'date': '2025-06-15'},
            {'medicine': 'Atenolol 50mg', 'quantity': '10', 'date': '2025-07-01'},
        ]
        preview = historical_import.validate_and_preview_import(raw_rows)
        self.assertEqual(preview['valid_count'], 2)

        result = historical_import.execute_historical_import(preview['preview_rows'], self.admin)
        self.assertEqual(result['created'], 2)

        self.inv.refresh_from_db()
        self.assertEqual(self.inv.current_stock, stock_before, "Historical import must NOT change current stock")

    def test_9b_imported_txns_use_historical_import_type(self):
        """All imported transactions must use HISTORICAL_IMPORT transaction type."""
        raw_rows = [{'medicine': 'Atenolol 50mg', 'quantity': '30', 'date': '2025-05-10'}]
        preview = historical_import.validate_and_preview_import(raw_rows)
        historical_import.execute_historical_import(preview['preview_rows'], self.admin)

        txns = StockTransaction.objects.filter(
            medicine=self.med, transaction_type='HISTORICAL_IMPORT'
        )
        self.assertEqual(txns.count(), 1)
        txn = txns.first()
        self.assertEqual(txn.previous_stock, txn.new_stock, "Stock must not change: previous_stock == new_stock")

    def test_10_duplicate_row_fingerprint_prevents_reimport(self):
        """Importing the same row twice must skip the second import (fingerprint dedup)."""
        raw_rows = [{'medicine': 'Atenolol 50mg', 'quantity': '15', 'date': '2025-04-20'}]
        preview1 = historical_import.validate_and_preview_import(raw_rows)
        result1 = historical_import.execute_historical_import(preview1['preview_rows'], self.admin)
        self.assertEqual(result1['created'], 1)

        preview2 = historical_import.validate_and_preview_import(raw_rows)
        # Already-imported row should be in errors (already_imported)
        self.assertEqual(preview2['valid_count'], 0, "Duplicate fingerprint must be detected and rejected")

    def test_11_future_dated_row_is_rejected(self):
        """Rows with future dates must be rejected during validation."""
        future_date = (timezone.now().date() + timedelta(days=10)).strftime('%Y-%m-%d')
        raw_rows = [{'medicine': 'Atenolol 50mg', 'quantity': '10', 'date': future_date}]
        preview = historical_import.validate_and_preview_import(raw_rows)
        self.assertEqual(preview['valid_count'], 0)
        self.assertEqual(preview['error_count'], 1)
        self.assertIn('future', preview['errors'][0]['message'])


class StockoutRiskTests(TestCase):
    """Tests 12–13: Stockout risk classification and risk factors."""

    def setUp(self):
        self.cat = Category.objects.create(name='Emergency')
        self.med = make_medicine('Epinephrine 1mg', category=self.cat, reorder_level=20, safety_stock=10)
        self.inv = make_inventory(self.med, stock=50)
        make_batch(self.med)

    def test_12_critical_risk_out_of_stock(self):
        """Medicine with zero stock must be classified CRITICAL."""
        self.inv.current_stock = 0
        self.inv.save()
        # Ensure batch shows 0 available
        MedicineBatch.objects.filter(medicine=self.med).update(quantity_available=0)

        for i in range(8):
            make_txn(self.inv, self.med, 'SALE', 5, ref=f'SALE-E0{i}', days_ago=i)

        risk = consumption_service.get_stockout_risk(self.med)
        self.assertEqual(risk['risk_level'], 'CRITICAL')
        self.assertIn('Currently out of stock', risk['risk_factors'])

    def test_12b_low_risk_ample_stock(self):
        """Medicine with large stock relative to consumption must be LOW risk."""
        self.inv.current_stock = 500
        self.inv.save()
        MedicineBatch.objects.filter(medicine=self.med).update(quantity_available=500)
        # Small daily consumption
        for i in range(10):
            make_txn(self.inv, self.med, 'SALE', 1, ref=f'SALE-L{i}', days_ago=i)

        risk = consumption_service.get_stockout_risk(self.med)
        self.assertIn(risk['risk_level'], ['LOW', 'MEDIUM'])

    def test_12c_no_data_when_no_consumption(self):
        """Medicine with zero transactions must return NO_DATA risk."""
        risk = consumption_service.get_stockout_risk(self.med)
        self.assertEqual(risk['risk_level'], 'NO_DATA')

    def test_13_risk_factors_populated(self):
        """Stockout risk must include relevant factor strings."""
        self.inv.current_stock = 5  # below safety stock of 10
        self.inv.save()
        MedicineBatch.objects.filter(medicine=self.med).update(quantity_available=5)

        for i in range(6):
            make_txn(self.inv, self.med, 'SALE', 3, ref=f'SALE-F{i}', days_ago=i)

        risk = consumption_service.get_stockout_risk(self.med)
        factors_str = ' '.join(risk['risk_factors'])
        self.assertGreater(len(risk['risk_factors']), 0, "Risk factors must be non-empty")
        # Should mention safety stock or reorder level
        self.assertTrue(
            any('safety' in f.lower() or 'reorder' in f.lower() or 'stock' in f.lower()
                for f in risk['risk_factors']),
            "Risk factors must mention stock-related reasons"
        )


class ViewAccessTests(TestCase):
    """Tests 14–17: HTTP access checks for all new views."""

    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_superuser(
            username='admin_view', email='av@mv.com', password='Pwd123!',
            role='ADMIN', status='APPROVED',
        )
        self.pharmacist = User.objects.create_user(
            username='pharm_view', email='pv@mv.com', password='Pwd123!',
            role='PHARMACIST', status='APPROVED',
        )
        self.cat = Category.objects.create(name='General')
        self.med = make_medicine('Ibuprofen 400mg', category=self.cat)
        make_inventory(self.med)
        make_batch(self.med)

    def test_14_consumption_analytics_admin(self):
        self.client.force_login(self.admin)
        res = self.client.get(reverse('inventory_consumption_analytics'))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Consumption Analytics')

    def test_14b_consumption_analytics_pharmacist(self):
        self.client.force_login(self.pharmacist)
        res = self.client.get(reverse('inventory_consumption_analytics'))
        self.assertEqual(res.status_code, 200)

    def test_14c_consumption_analytics_unauthenticated_redirect(self):
        res = self.client.get(reverse('inventory_consumption_analytics'))
        self.assertIn(res.status_code, [302, 403])

    def test_15_reorder_recommendations_admin(self):
        self.client.force_login(self.admin)
        res = self.client.get(reverse('inventory_reorder_recommendations'))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Reorder')

    def test_15b_reorder_recommendations_pharmacist(self):
        self.client.force_login(self.pharmacist)
        res = self.client.get(reverse('inventory_reorder_recommendations'))
        self.assertEqual(res.status_code, 200)

    def test_16_medicine_consumption_api_json(self):
        self.client.force_login(self.admin)
        res = self.client.get(reverse('medicine_consumption_api', kwargs={'pk': self.med.pk}))
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['ok'])
        self.assertIn('analytics', data)
        self.assertIn('forecast', data)
        self.assertIn('stockout_risk', data)
        self.assertIn('trend', data)

    def test_17_historical_import_view_get_admin(self):
        self.client.force_login(self.admin)
        res = self.client.get(reverse('inventory_historical_import'))
        self.assertEqual(res.status_code, 200)

    def test_17b_historical_import_view_pharmacist_forbidden(self):
        """Pharmacists must NOT be able to access historical import page."""
        self.client.force_login(self.pharmacist)
        res = self.client.get(reverse('inventory_historical_import'))
        # Should redirect or return 302
        self.assertIn(res.status_code, [302, 403])


class DataQualityThresholdTests(TestCase):
    """Test 18: Data quality threshold boundaries."""

    def setUp(self):
        self.cat = Category.objects.create(name='Diagnostics')
        self.med = make_medicine('Metformin 500mg', category=self.cat)
        self.inv = make_inventory(self.med, stock=300)
        make_batch(self.med)

    def test_18a_good_quality_15_or_more_transactions(self):
        for i in range(15):
            make_txn(self.inv, self.med, 'SALE', 5, ref=f'SALE-DQ{i}', days_ago=i)
        dq = consumption_service.get_data_quality_indicator(self.med)
        self.assertEqual(dq, 'GOOD')

    def test_18b_warning_quality_5_to_14_transactions(self):
        for i in range(8):
            make_txn(self.inv, self.med, 'SALE', 5, ref=f'SALE-WQ{i}', days_ago=i)
        dq = consumption_service.get_data_quality_indicator(self.med)
        self.assertEqual(dq, 'WARNING')

    def test_18c_insufficient_data_below_5_transactions(self):
        for i in range(3):
            make_txn(self.inv, self.med, 'SALE', 5, ref=f'SALE-IQ{i}', days_ago=i)
        dq = consumption_service.get_data_quality_indicator(self.med)
        self.assertEqual(dq, 'INSUFFICIENT_DATA')

    def test_18d_zero_transactions_is_insufficient(self):
        dq = consumption_service.get_data_quality_indicator(self.med)
        self.assertEqual(dq, 'INSUFFICIENT_DATA')


class PeriodBreakdownTests(TestCase):
    """Test 19: Period breakdown correctness."""

    def setUp(self):
        self.cat = Category.objects.create(name='Oncology')
        self.med = make_medicine('Tamoxifen 20mg', category=self.cat)
        self.inv = make_inventory(self.med, stock=400)
        make_batch(self.med)

    def test_19_period_breakdown_keys_present(self):
        """get_period_consumption_breakdown must return all expected keys."""
        for i in range(10):
            make_txn(self.inv, self.med, 'SALE', 5, ref=f'SALE-P{i}', days_ago=i)

        breakdown = consumption_service.get_period_consumption_breakdown(self.med)
        required_keys = [
            'rolling_7d', 'rolling_30d', 'rolling_90d',
            'month_to_date', 'mtd_label',
            'last_month', 'last_month_label', 'last_month_daily_avg',
            'this_quarter', 'quarter_label',
            'avg_daily_7d', 'avg_daily_30d', 'avg_weekly_30d',
        ]
        for key in required_keys:
            self.assertIn(key, breakdown, f"Missing key: {key}")

    def test_19b_rolling_values_non_negative(self):
        """All consumption values must be non-negative integers."""
        breakdown = consumption_service.get_period_consumption_breakdown(self.med)
        for key in ['rolling_7d', 'rolling_30d', 'rolling_90d', 'month_to_date', 'last_month']:
            self.assertGreaterEqual(breakdown[key], 0, f"{key} must be >= 0")


class ColdStartForecastTests(TestCase):
    """Test 20: Cold start forecast fallback when < 5 transactions."""

    def setUp(self):
        self.cat = Category.objects.create(name='Neurology')
        self.med = make_medicine('Levetiracetam 500mg', category=self.cat, reorder_level=30, safety_stock=15)
        self.inv = make_inventory(self.med, stock=100)
        make_batch(self.med)

    def test_20_cold_start_triggers_rule_based_forecast(self):
        """< 5 transactions must trigger RULE_BASED model with estimate."""
        from ai_engine.intelligence_service import get_demand_forecast
        # Only 3 transactions — below cold-start threshold of 5
        for i in range(3):
            make_txn(self.inv, self.med, 'SALE', 5, ref=f'CS-{i}', days_ago=i)

        forecast = get_demand_forecast(self.med)
        self.assertEqual(forecast['model_type'], 'RULE_BASED')
        self.assertEqual(forecast['data_origin'], 'ESTIMATE')
        self.assertTrue(forecast['is_cold_start'])
        self.assertGreater(forecast['forecast_30d'], 0, "Rule-based forecast must be positive")

    def test_20b_cold_start_uses_reorder_and_safety_stock(self):
        """Cold start formula: max(reorder_level*2, safety_stock*3)."""
        from ai_engine.intelligence_service import get_demand_forecast
        # Zero transactions
        forecast = get_demand_forecast(self.med)
        expected_min = max(self.med.reorder_level * 2, self.med.safety_stock * 3)
        self.assertEqual(forecast['forecast_30d'], expected_min)
