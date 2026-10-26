from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal

from accounts.models import User
from medicines.models import Medicine, Category, MedicineBatch
from inventory.models import Inventory, StockTransaction
from notifications.models import Notification
from notifications import service as notif_service
from reports.models import ReportRecord
from reports import report_engine


class Phase7Tests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_superuser(
            username='admin_p7', email='admin_p7@hospital.org', password='Password123!', role='ADMIN', status='APPROVED'
        )
        self.pharmacist = User.objects.create_user(
            username='pharm_p7', email='pharm_p7@hospital.org', password='Password123!', role='PHARMACIST', status='APPROVED'
        )
        self.category = Category.objects.create(name='Analgesics')
        self.medicine = Medicine.objects.create(
            medicine_name='Paracetamol 500mg',
            category=self.category,
            minimum_stock=10,
            maximum_stock=200,
            reorder_level=50,
            safety_stock=15,
            status='ACTIVE'
        )
        self.inventory, _ = Inventory.objects.get_or_create(
            medicine=self.medicine,
            defaults={'current_stock': 30, 'reorder_level': 50, 'stock_status': 'LOW_STOCK'}
        )

    def test_notification_deduplication(self):
        """Test fingerprint deduplication in notification engine."""
        n1 = notif_service.notify(
            notification_type=Notification.NotificationType.LOW_STOCK,
            title="Low Stock Alert",
            message="Paracetamol is low",
            priority='HIGH',
            fingerprint="LOW_STOCK:1:2026:35"
        )
        self.assertEqual(len(n1), 1)

        # Second attempt with same fingerprint should be deduplicated
        n2 = notif_service.notify(
            notification_type=Notification.NotificationType.LOW_STOCK,
            title="Low Stock Alert",
            message="Paracetamol is low",
            priority='HIGH',
            fingerprint="LOW_STOCK:1:2026:35"
        )
        self.assertEqual(len(n2), 0)

    def test_report_record_generation_and_verification(self):
        """Test generating report record and public QR verification."""
        record = ReportRecord.objects.create(
            report_type='INVENTORY',
            format='PDF',
            generated_by=self.admin
        )
        self.assertTrue(record.report_id.startswith('RPT-'))
        self.assertIsNotNone(record.verification_token)

        # Public verification endpoint test
        url = reverse('report_verify', kwargs={'token': record.verification_token})
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Official Report Verified')
        self.assertContains(res, record.report_id)

    def test_report_engine_data_extraction(self):
        """Test report engine data extraction for inventory."""
        data = report_engine.get_inventory_report_data()
        self.assertIn('data', data)
        self.assertIn('summary', data)
        self.assertEqual(data['summary']['total_medicines'], 1)

    def test_management_dashboard_view_access(self):
        """Test management dashboard view returns 200 for authenticated user."""
        self.client.login(username='admin_p7', password='Password123!')
        res = self.client.get(reverse('management_dashboard'))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Management Analytics Dashboard')

    def test_profit_report_uses_actual_allocated_batch_cost(self):
        """Verify Profit Report uses SaleItem.purchase_unit_price (actual batch purchase price)."""
        from sales.models import Sale, SaleItem
        batch = MedicineBatch.objects.create(
            medicine=self.medicine,
            batch_number='PCM-COST-TEST',
            expiry_date=timezone.now().date() + timedelta(days=200),
            purchase_price=Decimal('12.50'),
            selling_price=Decimal('25.00'),
            quantity_received=100,
            quantity_available=100,
            status='ACTIVE'
        )
        sale = Sale.objects.create(
            sale_number='SL-PROFIT-TEST',
            invoice_number='INV-PROFIT-TEST',
            patient_name='Test Patient',
            total_amount=Decimal('50.00'),
            subtotal=Decimal('50.00'),
            status='COMPLETED',
            sold_by=self.pharmacist
        )
        SaleItem.objects.create(
            sale=sale,
            medicine=self.medicine,
            batch=batch,
            quantity=2,
            unit_price=Decimal('25.00'),
            purchase_unit_price=Decimal('12.50'),
            total_price=Decimal('50.00')
        )

        data = report_engine.get_profit_report_data()
        self.assertGreaterEqual(len(data['data']), 1)
        pcm_row = next((r for r in data['data'] if r['medicine'] == self.medicine.medicine_name), None)
        self.assertIsNotNone(pcm_row)
        # Cost for 2 units @ 12.50 = 25.00, Revenue = 50.00, Profit = 25.00
        self.assertEqual(pcm_row['cost'], 25.00)
        self.assertEqual(pcm_row['revenue'], 50.00)
        self.assertEqual(pcm_row['gross_profit'], 25.00)

    def test_all_report_engines_execute_cleanly(self):
        """Verify all 14 official report engines execute without errors."""
        engine_map = {
            'INVENTORY': report_engine.get_inventory_report_data,
            'SALES': report_engine.get_sales_report_data,
            'PURCHASE': report_engine.get_purchase_report_data,
            'EXPIRY': report_engine.get_expiry_report_data,
            'PROFIT': report_engine.get_profit_report_data,
            'LOW_STOCK': report_engine.get_low_stock_report_data,
            'STOCK_MOVEMENT': report_engine.get_stock_movement_report_data,
            'SUPPLIER': report_engine.get_supplier_report_data,
            'PRICE_HISTORY': report_engine.get_price_history_report_data,
            'AI_PREDICTION': report_engine.get_ai_prediction_report_data,
            'CONSUMPTION': report_engine.get_consumption_report_data,
            'OPTIMIZATION': report_engine.get_optimization_report_data,
            'DISPENSING': report_engine.get_dispensing_report_data,
            'CONTROLLED': report_engine.get_controlled_report_data,
        }
        for name, fn in engine_map.items():
            result = fn()
            self.assertIn('data', result, f"Report {name} missing 'data'")
            self.assertIn('summary', result, f"Report {name} missing 'summary'")
            self.assertIn('columns', result, f"Report {name} missing 'columns'")

    def test_report_preview_and_download_views(self):
        """Verify report_preview (GET & POST) and download_report_view (PDF, EXCEL, CSV)."""
        self.client.login(username='admin_p7', password='Password123!')

        # 1. Preview via GET
        res_get = self.client.get(reverse('report_preview') + '?report_type=EXPIRY')
        self.assertEqual(res_get.status_code, 200)
        self.assertContains(res_get, 'Sanalogx')
        self.assertContains(res_get, 'Expiry Report')

        # 2. Preview via POST
        res_post = self.client.post(reverse('report_preview'), {'report_type': 'INVENTORY', 'format': 'PDF'})
        self.assertEqual(res_post.status_code, 200)
        record = res_post.context['record']

        # 3. Download PDF
        url_pdf = reverse('download_report', kwargs={'report_id': record.report_id, 'fmt': 'pdf'})
        res_pdf = self.client.get(url_pdf)
        self.assertEqual(res_pdf.status_code, 200)
        self.assertEqual(res_pdf['Content-Type'], 'application/pdf')

        # 4. Download Excel
        url_excel = reverse('download_report', kwargs={'report_id': record.report_id, 'fmt': 'excel'})
        res_excel = self.client.get(url_excel)
        self.assertEqual(res_excel.status_code, 200)
        self.assertIn('spreadsheetml', res_excel['Content-Type'])

        # 5. Download CSV
        url_csv = reverse('download_report', kwargs={'report_id': record.report_id, 'fmt': 'csv'})
        res_csv = self.client.get(url_csv)
        self.assertEqual(res_csv.status_code, 200)
        self.assertEqual(res_csv['Content-Type'], 'text/csv; charset=utf-8')
