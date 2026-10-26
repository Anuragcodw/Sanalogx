from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator
from django.core.exceptions import ValidationError
from django.utils import timezone


class Sale(models.Model):
    """
    Record of a pharmacy sale transaction.
    Connects Medicine, Batch, FEFO, Inventory, Invoice, Payment, and Stock Transactions.
    """

    class SaleStatus(models.TextChoices):
        COMPLETED = 'COMPLETED', 'Completed'
        RETURNED = 'RETURNED', 'Returned'
        PARTIAL_RETURN = 'PARTIAL_RETURN', 'Partially Returned'
        CANCELLED = 'CANCELLED', 'Cancelled'

    class PaymentMethod(models.TextChoices):
        CASH = 'CASH', 'Cash'
        CARD = 'CARD', 'Credit / Debit Card'
        UPI = 'UPI', 'UPI / Mobile QR'
        INSURANCE = 'INSURANCE', 'Insurance'
        CREDIT = 'CREDIT', 'Credit (Hospital Account)'
        FREE = 'FREE', 'Free / Charitable'

    # Identifiers
    sale_number = models.CharField(max_length=30, unique=True, help_text="e.g. SALE-2026-00001")
    invoice_number = models.CharField(max_length=30, unique=True, blank=True, null=True, help_text="e.g. INV-2026-00001")

    # Customer / Patient Info
    patient_name = models.CharField(max_length=255, default='Walk-in Customer')
    patient_id = models.CharField(max_length=50, blank=True, help_text="Hospital patient / UHID number")
    mobile_number = models.CharField(max_length=20, blank=True)
    doctor_name = models.CharField(max_length=255, blank=True)
    prescription_number = models.CharField(max_length=100, blank=True)

    # Financials
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    outstanding_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)],
        help_text="Pending / unpaid balance on invoice"
    )
    refund_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)],
        help_text="Total refund amount issued against this sale"
    )

    payment_method = models.CharField(max_length=15, choices=PaymentMethod.choices, default=PaymentMethod.CASH)
    status = models.CharField(max_length=20, choices=SaleStatus.choices, default=SaleStatus.COMPLETED)
    notes = models.TextField(blank=True)

    # Cancellation tracking
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='cancelled_sales'
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.CharField(max_length=255, blank=True)

    # Audit & Staff tracking
    sold_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='sales')
    sale_date = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Sale"
        verbose_name_plural = "Sales"
        ordering = ['-sale_date']
        indexes = [
            models.Index(fields=['-sale_date']),
            models.Index(fields=['status']),
            models.Index(fields=['patient_id']),
            models.Index(fields=['invoice_number']),
        ]

    def __str__(self):
        return f"{self.sale_number} — ₹{self.total_amount} [{self.get_status_display()}]"

    @classmethod
    def generate_sale_number(cls):
        year = timezone.now().year
        count = cls.objects.filter(sale_date__year=year).count() + 1
        return f"SALE-{year}-{count:05d}"

    @classmethod
    def generate_invoice_number(cls):
        year = timezone.now().year
        count = cls.objects.filter(sale_date__year=year).count() + 1
        return f"INV-{year}-{count:05d}"

    def calculate_totals(self):
        """Server-side recalculation of invoice subtotals, taxes, discounts, and outstanding amounts."""
        items = list(self.items.all())
        from decimal import Decimal
        subtotal = sum((item.unit_price * item.quantity for item in items), Decimal('0.00'))
        item_discounts = sum((item.discount_amount for item in items), Decimal('0.00'))
        item_taxes = sum((item.tax_amount for item in items), Decimal('0.00'))
        
        self.subtotal = subtotal
        self.discount_amount = item_discounts
        self.tax_amount = item_taxes
        self.total_amount = max(Decimal('0.00'), subtotal - self.discount_amount + self.tax_amount)
        self.outstanding_amount = max(Decimal('0.00'), self.total_amount - self.amount_paid)
        self.save(update_fields=['subtotal', 'discount_amount', 'tax_amount', 'total_amount', 'outstanding_amount'])

    @property
    def balance_due(self):
        from decimal import Decimal
        return max(Decimal('0.00'), self.total_amount - self.amount_paid)

    @property
    def is_fully_paid(self):
        return self.amount_paid >= self.total_amount

    @property
    def total_cost(self):
        """Historical purchase cost basis across all sold items."""
        from decimal import Decimal
        return sum((item.purchase_unit_price * item.quantity for item in self.items.all()), Decimal('0.00'))

    @property
    def net_cost(self):
        """Net Cost of Goods Sold (COGS) excluding restocked returns."""
        from decimal import Decimal
        if self.status == self.SaleStatus.CANCELLED:
            return Decimal('0.00')
        return sum((item.net_cost for item in self.items.all()), Decimal('0.00'))

    @property
    def net_sales(self):
        """Gross invoice revenue minus customer refunds."""
        from decimal import Decimal
        if self.status == self.SaleStatus.CANCELLED:
            return Decimal('0.00')
        return max(Decimal('0.00'), self.total_amount - self.refund_amount)

    @property
    def net_revenue_excl_tax(self):
        """
        Net revenue excluding statutory taxes collected and refunded.
        Sales tax collected is a government liability, not pharmacy revenue.
        """
        from decimal import Decimal
        if self.status == self.SaleStatus.CANCELLED:
            return Decimal('0.00')
        taxable_sales = max(Decimal('0.00'), self.subtotal - self.discount_amount)
        tax_portion_refunded = Decimal('0.00')
        for r in self.returns.all():
            tax_portion_refunded += getattr(r, 'tax_refund_amount', Decimal('0.00')) or Decimal('0.00')
        net_refund_excl_tax = max(Decimal('0.00'), self.refund_amount - tax_portion_refunded)
        return max(Decimal('0.00'), taxable_sales - net_refund_excl_tax)

    @property
    def gross_profit(self):
        """
        Gross Profit = Net Sales Revenue (Excluding Tax) - Net COGS.
        """
        from decimal import Decimal
        if self.status == self.SaleStatus.CANCELLED:
            return Decimal('0.00')
        return self.net_revenue_excl_tax - self.net_cost

    @property
    def gross_profit_margin_pct(self):
        from decimal import Decimal
        rev = self.net_revenue_excl_tax
        if rev > Decimal('0.00'):
            return round((self.gross_profit / rev) * Decimal('100.00'), 2)
        return Decimal('0.00')

    @property
    def total_profit(self):
        """Backward-compatible alias returning gross_profit."""
        return self.gross_profit

    def delete(self, *args, **kwargs):
        if self.status in [self.SaleStatus.COMPLETED, self.SaleStatus.PARTIAL_RETURN]:
            raise ValidationError(
                f"Cannot delete completed sale {self.sale_number}. "
                "Use the return or cancellation workflow to adjust inventory."
            )
        return super().delete(*args, **kwargs)


class SaleItem(models.Model):
    """Individual medicine line within a sale, linked to a specific batch (FEFO)."""
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name='items')
    medicine = models.ForeignKey('medicines.Medicine', on_delete=models.PROTECT, related_name='sale_items')
    batch = models.ForeignKey('medicines.MedicineBatch', on_delete=models.PROTECT, related_name='sale_items')

    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    unit_price = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    purchase_unit_price = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="Historical purchase cost per unit at the time of sale for profit calculation"
    )
    discount_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0, validators=[MinValueValidator(0)], help_text="GST / tax %")
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    total_price = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)])

    # Return tracking
    quantity_returned = models.PositiveIntegerField(default=0)
    return_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        verbose_name = "Sale Item"
        verbose_name_plural = "Sale Items"

    def save(self, *args, **kwargs):
        from decimal import Decimal
        sub = round(Decimal(str(self.quantity)) * Decimal(str(self.unit_price)), 2)
        if self.discount_pct and (not self.discount_amount or self.discount_amount == 0):
            self.discount_amount = round(sub * (Decimal(str(self.discount_pct)) / Decimal('100.00')), 2)
        taxable = max(Decimal('0.00'), sub - (self.discount_amount or Decimal('0.00')))
        if self.tax_rate and (not self.tax_amount or self.tax_amount == 0):
            self.tax_amount = round(taxable * (Decimal(str(self.tax_rate)) / Decimal('100.00')), 2)
        self.total_price = taxable + (self.tax_amount or Decimal('0.00'))
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.sale.sale_number} | {self.medicine.medicine_name} × {self.quantity}"

    @property
    def active_quantity(self):
        """Remaining sold units after subtracting returns."""
        return max(0, self.quantity - self.quantity_returned)

    @property
    def cost_total(self):
        """Historical cost basis for entire sold quantity."""
        from decimal import Decimal
        return round(self.purchase_unit_price * Decimal(str(self.quantity)), 2)

    @property
    def net_cost(self):
        """Net Cost of Goods Sold for active unreturned quantity."""
        from decimal import Decimal
        return round(self.purchase_unit_price * Decimal(str(self.active_quantity)), 2)

    @property
    def taxable_amount(self):
        """Pre-tax taxable value = line subtotal minus discount."""
        from decimal import Decimal
        sub = round(Decimal(str(self.quantity)) * self.unit_price, 2)
        return max(Decimal('0.00'), sub - (self.discount_amount or Decimal('0.00')))

    @property
    def net_taxable_amount(self):
        """Active pre-tax value after returns."""
        from decimal import Decimal
        disc_factor = (Decimal('1.00') - (self.discount_pct / Decimal('100.00'))) if self.discount_pct else Decimal('1.00')
        return round(Decimal(str(self.active_quantity)) * self.unit_price * disc_factor, 2)

    @property
    def gross_profit(self):
        """Gross profit = Net Taxable Revenue - Net Cost."""
        return self.net_taxable_amount - self.net_cost

    @property
    def profit(self):
        """Backward-compatible alias returning gross_profit."""
        return self.gross_profit


class SaleReturn(models.Model):
    """Record of a medicine return / refund against a completed Sale."""
    return_number = models.CharField(max_length=30, unique=True, help_text="e.g. RET-2026-00001")
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name='returns')
    return_date = models.DateTimeField(auto_now_add=True)
    refund_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_refund_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        validators=[MinValueValidator(0)],
        help_text="Statutory tax portion of refund"
    )
    reason = models.CharField(max_length=255, default="Customer Return — Unused")
    notes = models.TextField(blank=True)
    processed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        verbose_name = "Sale Return"
        verbose_name_plural = "Sale Returns"
        ordering = ['-return_date']

    def __str__(self):
        return f"{self.return_number} — {self.sale.sale_number} (₹{self.refund_amount})"

    @classmethod
    def generate_return_number(cls):
        year = timezone.now().year
        count = cls.objects.filter(return_date__year=year).count() + 1
        return f"RET-{year}-{count:05d}"

    @property
    def net_refund(self):
        from decimal import Decimal
        return max(Decimal('0.00'), self.refund_amount - (self.tax_refund_amount or Decimal('0.00')))


class SaleReturnItem(models.Model):
    """Line item returned within a SaleReturn transaction."""
    sale_return = models.ForeignKey(SaleReturn, on_delete=models.CASCADE, related_name='items')
    sale_item = models.ForeignKey(SaleItem, on_delete=models.CASCADE, related_name='return_records')
    quantity_returned = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    refund_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax_refund_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        validators=[MinValueValidator(0)],
        help_text="Tax portion reversed on this item"
    )
    cogs_reversed = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        validators=[MinValueValidator(0)],
        help_text="Historical cost reversed to inventory"
    )
    restocked_to_batch = models.BooleanField(default=True, help_text="Whether stock was safely restored to batch")

    class Meta:
        verbose_name = "Sale Return Item"
        verbose_name_plural = "Sale Return Items"

    def __str__(self):
        return f"{self.sale_return.return_number} | {self.sale_item.medicine.medicine_name} x {self.quantity_returned}"
