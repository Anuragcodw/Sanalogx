from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator
from django.utils import timezone


class PurchaseRequest(models.Model):
    """
    Pre-purchase-order restocking request.
    Generated manually by Admin or triggered by low-stock alerts.
    Lifecycle: DRAFT → PENDING_APPROVAL → APPROVED / REJECTED → CONVERTED_TO_PO
    """

    class PRStatus(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'
        PENDING_APPROVAL = 'PENDING_APPROVAL', 'Pending Approval'
        APPROVED = 'APPROVED', 'Approved'
        REJECTED = 'REJECTED', 'Rejected'
        CONVERTED_TO_PO = 'CONVERTED_TO_PO', 'Converted to Purchase Order'

    class Priority(models.TextChoices):
        NORMAL = 'NORMAL', 'Normal'
        URGENT = 'URGENT', 'Urgent'
        CRITICAL = 'CRITICAL', 'Critical'

    request_id = models.CharField(max_length=30, unique=True)
    priority = models.CharField(
        max_length=10, choices=Priority.choices, default=Priority.NORMAL
    )
    status = models.CharField(
        max_length=20, choices=PRStatus.choices, default=PRStatus.DRAFT
    )
    reason = models.TextField(blank=True, help_text="Reason for raising this purchase request")
    notes = models.TextField(blank=True)

    # Recommended supplier (advisory — Admin may override on PO)
    recommended_supplier = models.ForeignKey(
        'suppliers.Supplier', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='purchase_requests'
    )

    # Stock alert that triggered this request (optional linkage)
    triggered_by_alert = models.ForeignKey(
        'inventory.StockAlert', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='purchase_requests'
    )

    # Purchase Order generated after approval
    converted_to_po = models.ForeignKey(
        'PurchaseOrder', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='source_requests'
    )

    # Workflow tracking
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='purchase_requests_created'
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='purchase_requests_reviewed'
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Purchase Request"
        verbose_name_plural = "Purchase Requests"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['priority']),
            models.Index(fields=['request_id']),
        ]

    def __str__(self):
        return f"{self.request_id} [{self.get_status_display()}]"

    @classmethod
    def generate_request_id(cls):
        year = timezone.now().year
        count = cls.objects.filter(created_at__year=year).count() + 1
        return f"PR-{year}-{count:05d}"


class PurchaseRequestItem(models.Model):
    """Line item for each medicine in a Purchase Request."""
    purchase_request = models.ForeignKey(
        PurchaseRequest, on_delete=models.CASCADE, related_name='items'
    )
    medicine = models.ForeignKey(
        'medicines.Medicine', on_delete=models.PROTECT,
        related_name='purchase_request_items'
    )

    current_stock = models.PositiveIntegerField(default=0,
        help_text="Stock at time of request creation")
    reorder_level = models.PositiveIntegerField(default=0)
    ai_recommended_qty = models.PositiveIntegerField(default=0,
        help_text="Quantity recommended by AI engine")
    requested_qty = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        help_text="Quantity requested by pharmacist/admin"
    )
    approved_qty = models.PositiveIntegerField(default=0,
        help_text="Quantity approved by admin (may differ from requested)")

    # Best-price supplier for this specific medicine at request time
    suggested_supplier = models.ForeignKey(
        'suppliers.Supplier', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='suggested_for_items'
    )
    suggested_unit_price = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        validators=[MinValueValidator(0)]
    )

    notes = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Purchase Request Item"
        verbose_name_plural = "Purchase Request Items"
        unique_together = [['purchase_request', 'medicine']]

    def __str__(self):
        return (
            f"{self.purchase_request.request_id} | "
            f"{self.medicine.medicine_name} × {self.requested_qty}"
        )

    @property
    def estimated_cost(self):
        if self.suggested_unit_price:
            return self.requested_qty * self.suggested_unit_price
        return 0


class PurchaseOrder(models.Model):
    """
    Purchase order raised to a supplier for medicine restocking.
    Full lifecycle: DRAFT → SUBMITTED → APPROVED → ORDERED → PARTIAL_RECEIPT → COMPLETED / CANCELLED.
    """

    class POStatus(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'
        SUBMITTED = 'SUBMITTED', 'Submitted for Approval'
        APPROVED = 'APPROVED', 'Approved'
        ORDERED = 'ORDERED', 'Ordered (Sent to Supplier)'
        PARTIAL_RECEIPT = 'PARTIAL_RECEIPT', 'Partially Received'
        COMPLETED = 'COMPLETED', 'Completed'
        CANCELLED = 'CANCELLED', 'Cancelled'

    class Priority(models.TextChoices):
        LOW = 'LOW', 'Low'
        NORMAL = 'NORMAL', 'Normal'
        HIGH = 'HIGH', 'High'
        URGENT = 'URGENT', 'Urgent'

    class EmailStatus(models.TextChoices):
        NOT_SENT = 'NOT_SENT', 'Not Sent'
        SENT = 'SENT', 'Sent'
        FAILED = 'FAILED', 'Failed'

    # PO number (e.g., PO-2026-00042)
    po_number = models.CharField(max_length=30, unique=True)

    supplier = models.ForeignKey(
        'suppliers.Supplier', on_delete=models.PROTECT,
        related_name='purchase_orders'
    )

    status = models.POStatusField = models.CharField(
        max_length=20, choices=POStatus.choices,
        default=POStatus.DRAFT
    )
    priority = models.CharField(
        max_length=10, choices=Priority.choices,
        default=Priority.NORMAL
    )

    # Dates
    order_date = models.DateField(null=True, blank=True)
    expected_delivery_date = models.DateField(null=True, blank=True)
    actual_delivery_date = models.DateField(null=True, blank=True)

    # Financials
    subtotal = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        validators=[MinValueValidator(0)]
    )
    tax_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        validators=[MinValueValidator(0)]
    )
    discount_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        validators=[MinValueValidator(0)]
    )
    shipping_cost = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        validators=[MinValueValidator(0)]
    )
    total_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        validators=[MinValueValidator(0)]
    )

    notes = models.TextField(blank=True)
    terms_and_conditions = models.TextField(blank=True)
    email_status = models.CharField(
        max_length=10,
        choices=EmailStatus.choices,
        default=EmailStatus.NOT_SENT,
    )
    email_sent_at = models.DateTimeField(null=True, blank=True)
    supplier_confirmation_reference = models.CharField(max_length=255, blank=True)
    supplier_confirmed_at = models.DateTimeField(null=True, blank=True)
    supplier_confirmed_eta = models.DateField(null=True, blank=True)

    # Workflow tracking
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='purchase_orders_created'
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='purchase_orders_approved'
    )
    supplier_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='purchase_orders_supplier_confirmed'
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='purchase_orders_cancelled'
    )
    cancellation_reason = models.TextField(blank=True)

    # Alert that triggered this PO (optional linkage)
    triggered_by_alert = models.ForeignKey(
        'inventory.StockAlert', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='purchase_orders'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Purchase Order"
        verbose_name_plural = "Purchase Orders"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['supplier']),
            models.Index(fields=['po_number']),
        ]

    def __str__(self):
        return f"{self.po_number} — {self.supplier.company_name} [{self.get_status_display()}]"

    def calculate_totals(self):
        """Recalculate subtotal and total from line items."""
        items = self.items.all()
        subtotal = sum(item.total_price for item in items)
        self.subtotal = subtotal
        self.total_amount = subtotal + self.tax_amount + self.shipping_cost - self.discount_amount
        self.save(update_fields=['subtotal', 'total_amount'])

    @property
    def is_editable(self):
        return self.status in (self.POStatus.DRAFT, self.POStatus.SUBMITTED)

    @property
    def is_receivable(self):
        return self.status in (self.POStatus.APPROVED, self.POStatus.ORDERED,
                               self.POStatus.PARTIAL_RECEIPT)

    @classmethod
    def generate_po_number(cls):
        """Generate a sequential PO number in format PO-YYYY-NNNNN."""
        from django.utils import timezone
        year = timezone.now().year
        count = cls.objects.filter(created_at__year=year).count() + 1
        return f"PO-{year}-{count:05d}"


class PurchaseOrderItem(models.Model):
    """Individual line item within a Purchase Order."""
    purchase_order = models.ForeignKey(
        PurchaseOrder, on_delete=models.CASCADE,
        related_name='items'
    )
    medicine = models.ForeignKey(
        'medicines.Medicine', on_delete=models.PROTECT,
        related_name='purchase_order_items'
    )

    quantity_ordered = models.PositiveIntegerField(
        validators=[MinValueValidator(1)]
    )
    quantity_received = models.PositiveIntegerField(default=0)
    quantity_pending = models.PositiveIntegerField(default=0)

    unit_price = models.DecimalField(
        max_digits=10, decimal_places=2,
        validators=[MinValueValidator(0)]
    )
    total_price = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        validators=[MinValueValidator(0)]
    )

    notes = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Purchase Order Item"
        verbose_name_plural = "Purchase Order Items"
        unique_together = [['purchase_order', 'medicine']]

    def save(self, *args, **kwargs):
        self.total_price = self.quantity_ordered * self.unit_price
        self.quantity_pending = self.quantity_ordered - self.quantity_received
        super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"{self.purchase_order.po_number} | "
            f"{self.medicine.generic_name} × {self.quantity_ordered}"
        )


class GoodsReceipt(models.Model):
    """
    Records the actual physical receipt of medicines against a Purchase Order.
    Supports partial receipts — multiple GRNs can exist per PO.
    """

    class GRNStatus(models.TextChoices):
        PENDING_VERIFICATION = 'PENDING_VERIFICATION', 'Pending QC Verification'
        VERIFIED = 'VERIFIED', 'Verified & Accepted'
        REJECTED = 'REJECTED', 'Rejected'
        PARTIAL = 'PARTIAL', 'Partial — Pending Balance'

    # GRN number (e.g., GRN-2026-00011)
    grn_number = models.CharField(max_length=30, unique=True)

    purchase_order = models.ForeignKey(
        PurchaseOrder, on_delete=models.CASCADE,
        related_name='goods_receipts'
    )
    supplier = models.ForeignKey(
        'suppliers.Supplier', on_delete=models.PROTECT,
        related_name='goods_receipts'
    )

    receipt_date = models.DateField()
    invoice_number = models.CharField(max_length=100, blank=True)
    invoice_date = models.DateField(null=True, blank=True)

    status = models.CharField(
        max_length=25, choices=GRNStatus.choices,
        default=GRNStatus.PENDING_VERIFICATION
    )

    # Financials
    invoice_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        validators=[MinValueValidator(0)]
    )

    notes = models.TextField(blank=True)
    rejection_reason = models.TextField(blank=True)

    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='goods_receipts_received'
    )
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='goods_receipts_verified'
    )
    verified_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Goods Receipt Note"
        verbose_name_plural = "Goods Receipt Notes"
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.grn_number} — {self.purchase_order.po_number}"

    @classmethod
    def generate_grn_number(cls):
        from django.utils import timezone
        year = timezone.now().year
        count = cls.objects.filter(created_at__year=year).count() + 1
        return f"GRN-{year}-{count:05d}"


class GoodsReceiptItem(models.Model):
    """Individual medicine line in a Goods Receipt Note."""
    goods_receipt = models.ForeignKey(
        GoodsReceipt, on_delete=models.CASCADE,
        related_name='items'
    )
    po_item = models.ForeignKey(
        PurchaseOrderItem, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='receipt_items'
    )
    medicine = models.ForeignKey(
        'medicines.Medicine', on_delete=models.PROTECT,
        related_name='goods_receipt_items'
    )

    quantity_received = models.PositiveIntegerField(
        validators=[MinValueValidator(1)]
    )
    quantity_accepted = models.PositiveIntegerField(default=0)
    quantity_rejected = models.PositiveIntegerField(default=0)

    # Batch information for this receipt
    batch_number = models.CharField(max_length=100)
    manufactured_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField()

    unit_price = models.DecimalField(
        max_digits=10, decimal_places=2,
        validators=[MinValueValidator(0)]
    )

    rejection_reason = models.CharField(max_length=255, blank=True)
    notes = models.CharField(max_length=255, blank=True)

    # Link to MedicineBatch created when this GRN item is verified
    medicine_batch = models.ForeignKey(
        'medicines.MedicineBatch', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='grn_item'
    )

    class Meta:
        verbose_name = "Goods Receipt Item"
        verbose_name_plural = "Goods Receipt Items"

    def __str__(self):
        return (
            f"{self.goods_receipt.grn_number} | "
            f"{self.medicine.generic_name} × {self.quantity_received}"
        )
