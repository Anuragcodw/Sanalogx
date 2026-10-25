from decimal import Decimal
from datetime import timedelta
from django.test import TestCase, Client
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

from medicines.models import Medicine, MedicineBatch, Category
from inventory.models import Inventory, StockTransaction
from inventory.services import add_stock, recalculate_inventory
from sales.models import Sale, SaleItem, SaleReturn
from sales import services

User = get_user_model()


class SalesTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.pharmacist = User.objects.create_user(
            username='pharmacist_sales',
            email='pharmacist_sales@hospital.org',
            password='Password123!',
            role='PHARMACIST',
            status='APPROVED'
        )
        self.category = Category.objects.create(name="Analgesics")

        self.medicine = Medicine.objects.create(
            medicine_name="Paracetamol 500mg",
            generic_name="Paracetamol",
            category=self.category,
            dosage_form="Tablet",
            strength="500mg",
            unit_of_measure="Strip",
            reorder_level=20,
            minimum_stock=10,
            maximum_stock=500,
            barcode="PARA889421"
        )

        today = timezone.now().date()
        self.batch = MedicineBatch.objects.create(
            medicine=self.medicine,
            batch_number="BATCH-PARA-2026",
            expiry_date=today + timedelta(days=180),
            purchase_price=Decimal('3.00'),
            selling_price=Decimal('5.00'),
            quantity_received=100,
            quantity_available=100,
            status='ACTIVE'
        )

        self.inventory, _ = Inventory.objects.get_or_create(medicine=self.medicine)
        recalculate_inventory(self.inventory)

    def test_atomic_checkout_and_stock_deduction(self):
        """Test atomic sale creation, stock deduction, and historical cost recording."""
        cart = [{'medicine_id': self.medicine.id, 'quantity': 20}]

        sale = services.create_sale_from_cart(
            patient_name="John Doe",
            cart_items=cart,
            payment_method='CASH',
            discount_amount=Decimal('5.00'),
            tax_amount=Decimal('4.75'),
            pharmacist_user=self.pharmacist
        )

        # Check Sale record
        self.assertEqual(sale.status, 'COMPLETED')
        self.assertEqual(sale.subtotal, Decimal('100.00'))  # 20 * 5.00
        self.assertEqual(sale.total_amount, Decimal('105.00'))  # 100 + 5.00 tax

        # Check stock deduction
        self.batch.refresh_from_db()
        self.inventory.refresh_from_db()
        self.assertEqual(self.batch.quantity_available, 80)
        self.assertEqual(self.inventory.current_stock, 80)

        # Check StockTransaction
        st = StockTransaction.objects.filter(medicine=self.medicine, transaction_type='SALE').first()
        self.assertIsNotNone(st)
        self.assertEqual(st.quantity, 20)

        # Check historical cost & profit calculation
        item = sale.items.first()
        self.assertEqual(item.purchase_unit_price, Decimal('3.00'))
        self.assertEqual(item.unit_price, Decimal('5.00'))
        self.assertEqual(sale.total_cost, Decimal('60.00'))  # 20 * 3.00
        self.assertEqual(sale.total_profit, Decimal('40.00'))  # Net Sales (100.00) - COGS (60.00)

    def test_insufficient_stock_protection(self):
        """Test that attempting to sell more than available stock raises ValidationError."""
        cart = [{'medicine_id': self.medicine.id, 'quantity': 150}]
        with self.assertRaises(ValidationError):
            services.create_sale_from_cart(
                cart_items=cart,
                pharmacist_user=self.pharmacist
            )

    def test_expired_batch_protection(self):
        """Test that selling an expired batch is strictly blocked."""
        today = timezone.now().date()
        expired_batch = MedicineBatch.objects.create(
            medicine=self.medicine,
            batch_number="EXPIRED-BATCH",
            expiry_date=today - timedelta(days=10),
            purchase_price=Decimal('2.00'),
            selling_price=Decimal('4.00'),
            quantity_received=50,
            quantity_available=50,
            status='EXPIRED'
        )

        cart = [{'medicine_id': self.medicine.id, 'quantity': 20}]
        # Set only expired batch available
        self.batch.quantity_available = 0
        self.batch.save()
        recalculate_inventory(self.inventory)

        with self.assertRaises(ValidationError):
            services.create_sale_from_cart(
                cart_items=cart,
                pharmacist_user=self.pharmacist
            )

    def test_barcode_scan_api(self):
        """Test that barcode scan API identifies medicine without deducting stock."""
        self.client.force_login(self.pharmacist)
        response = self.client.get('/api/sales/barcode-scan/?code=PARA889421')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['medicine_name'], "Paracetamol 500mg")
        self.assertEqual(data['available_stock'], 100)

        # Confirm stock was NOT deducted
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_available, 100)

    def test_sale_return(self):
        """Test sale return workflow and stock restoration."""
        cart = [{'medicine_id': self.medicine.id, 'quantity': 10}]
        sale = services.create_sale_from_cart(
            cart_items=cart,
            pharmacist_user=self.pharmacist
        )

        item = sale.items.first()
        return_items = [{'item_id': item.id, 'quantity': 5}]

        ret = services.process_sale_return(
            sale_id=sale.id,
            return_items_data=return_items,
            reason="Customer Return",
            pharmacist_user=self.pharmacist
        )

        self.assertEqual(ret.refund_amount, Decimal('26.25'))  # 5 * 5.25 (unit price + proportional tax)
        sale.refresh_from_db()
        self.assertEqual(sale.status, 'PARTIAL_RETURN')

        # Confirm stock was restored
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_available, 95)
