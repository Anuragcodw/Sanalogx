from decimal import Decimal
from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.core.exceptions import ValidationError
from django.utils import timezone


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(
        max_length=100,
        unique=True,
        null=True,
        blank=True,
        help_text="Standard category code e.g. ANALGESIC_ANTIPYRETIC"
    )
    description = models.TextField(blank=True)
    notes = models.TextField(blank=True, help_text="Therapeutic and pharmacy review notes")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Categories"

    def __str__(self):
        return f"{self.name} ({self.code})" if self.code else self.name


class Medicine(models.Model):
    class Status(models.TextChoices):
        ACTIVE = 'ACTIVE', 'Active'
        INACTIVE = 'INACTIVE', 'Inactive'
        PENDING_REVIEW = 'PENDING_REVIEW', 'Pending Review (Draft)'

    class VerificationStatus(models.TextChoices):
        PENDING_REVIEW = 'PENDING_REVIEW', 'Pending Review (Draft)'
        VERIFIED = 'VERIFIED', 'Verified Catalogue'
        REJECTED = 'REJECTED', 'Rejected'

    class RegulatoryCategory(models.TextChoices):
        NORMAL = 'NORMAL', 'General / OTC'
        SCHEDULE_H = 'SCHEDULE_H', 'Schedule H (Prescription Only)'
        SCHEDULE_H1 = 'SCHEDULE_H1', 'Schedule H1 (Prescription & Register Required)'
        SCHEDULE_X = 'SCHEDULE_X', 'Schedule X (Special Controlled)'
        NARCOTIC = 'NARCOTIC', 'Narcotic / NDPS'
        CONTROLLED_OTHER = 'CONTROLLED_OTHER', 'Other Controlled Substance'

    medicine_name = models.CharField(max_length=255)
    generic_name = models.CharField(max_length=255)
    brand_name = models.CharField(max_length=255, blank=True)
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name='medicines'
    )
    dosage_form = models.CharField(max_length=100)
    strength = models.CharField(max_length=100)
    manufacturer = models.CharField(max_length=255, blank=True)
    unit_of_measure = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    barcode = models.CharField(max_length=100, unique=True, null=True, blank=True)
    internal_barcode = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        help_text="Unique internal Sanalogx barcode identifier e.g. SAN-MED-000123"
    )
    qr_code = models.CharField(max_length=255, null=True, blank=True)
    image = models.ImageField(upload_to='medicines/', null=True, blank=True)
    reorder_level = models.PositiveIntegerField(default=50)
    minimum_stock = models.PositiveIntegerField(default=10)
    maximum_stock = models.PositiveIntegerField(default=500)
    safety_stock = models.PositiveIntegerField(default=20)
    regulatory_category = models.CharField(
        max_length=30,
        choices=RegulatoryCategory.choices,
        default=RegulatoryCategory.NORMAL
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE
    )

    # Master Pricing & Tax Specifications
    tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('0.00'),
        blank=True,
        validators=[MinValueValidator(Decimal('0.00')), MaxValueValidator(Decimal('100.00'))],
        help_text="Standard applicable GST / tax rate (%)"
    )
    mrp = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
        blank=True,
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text="Standard Maximum Retail Price (₹)"
    )
    max_discount_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('20.00'),
        blank=True,
        validators=[MinValueValidator(Decimal('0.00')), MaxValueValidator(Decimal('100.00'))],
        help_text="Maximum allowable discount percentage"
    )

    # Classification, Clinical & Import Provenance
    atc_code = models.CharField(
        max_length=30,
        blank=True,
        null=True,
        help_text="WHO Anatomical Therapeutic Chemical code (e.g., N02BE01)"
    )
    storage_condition = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text="e.g. Store below 25°C, Cold chain 2-8°C"
    )
    source_reference = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text="Origin of catalogue entry (e.g., Starter_Examples, Medicine_Import)"
    )
    review_note = models.TextField(
        blank=True,
        default='',
        help_text="Pharmacy verification criteria and clinical review notes"
    )
    verification_status = models.CharField(
        max_length=30,
        choices=VerificationStatus.choices,
        default=VerificationStatus.VERIFIED,
        help_text="Clinical review status (PENDING_REVIEW, VERIFIED, REJECTED)"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def generate_internal_barcode(self):
        """Generates deterministic unique internal barcode identifier."""
        if self.pk:
            return f"SAN-MED-{self.pk:06d}"
        return None

    @property
    def effective_internal_barcode(self):
        """Returns the persisted internal barcode or the deterministic fallback."""
        if self.internal_barcode:
            return self.internal_barcode
        if self.pk:
            return f"SAN-MED-{self.pk:06d}"
        return ""

    def save(self, *args, **kwargs):
        # Assign deterministic internal barcode if pk exists and not yet set
        if self.pk and not self.internal_barcode:
            self.internal_barcode = self.generate_internal_barcode()
        super().save(*args, **kwargs)
        # For newly created medicines, assign internal barcode post-insert
        if not self.internal_barcode and self.pk:
            self.internal_barcode = self.generate_internal_barcode()
            Medicine.objects.filter(pk=self.pk, internal_barcode__isnull=True).update(
                internal_barcode=self.internal_barcode
            )

    def __str__(self):
        return f"{self.medicine_name} ({self.strength})"

    @property
    def is_controlled(self):
        return self.regulatory_category in [
            self.RegulatoryCategory.SCHEDULE_H1,
            self.RegulatoryCategory.SCHEDULE_X,
            self.RegulatoryCategory.NARCOTIC,
            self.RegulatoryCategory.CONTROLLED_OTHER,
        ]

    @property
    def is_prescription_required(self):
        return self.regulatory_category != self.RegulatoryCategory.NORMAL

    @property
    def requires_prescription(self):
        return self.is_prescription_required

    @property
    def is_controlled_substance(self):
        return self.is_controlled

    @property
    def controlled_category(self):
        return self.get_regulatory_category_display()

    @property
    def medicine_image(self):
        return self.image

    @property
    def current_stock(self):
        try:
            if hasattr(self, 'inventory') and self.inventory:
                return self.inventory.current_stock
        except Exception:
            pass
        total = self.batches.filter(status='ACTIVE').aggregate(models.Sum('quantity_available'))['quantity_available__sum']
        return total or 0

    @property
    def available_stock(self):
        try:
            if hasattr(self, 'inventory') and self.inventory:
                return self.inventory.available_stock
        except Exception:
            pass
        return self.current_stock

    @property
    def is_low_stock(self):
        return self.current_stock <= self.reorder_level

    @property
    def is_out_of_stock(self):
        return self.current_stock == 0

    @property
    def unit(self):
        class _UnitWrapper:
            def __init__(self, name):
                self.name = name or 'Unit'
            def __str__(self):
                return self.name
        return _UnitWrapper(self.unit_of_measure)

    @property
    def selling_price(self):
        active_batch = self.batches.filter(status='ACTIVE').order_by('expiry_date').first()
        if active_batch and active_batch.selling_price:
            return active_batch.selling_price
        return self.mrp or Decimal('0.00')

    @property
    def barcode_type(self):
        if not self.barcode:
            return "Not Assigned"
        return "EAN-13" if len(self.barcode) == 13 and self.barcode.isdigit() else "Code 128"

    @property
    def medicine_id(self):
        return f"MED-{self.id:04d}" if self.id else ""

    @property
    def is_active(self):
        return self.status == self.Status.ACTIVE

    @property
    def is_draft(self):
        return (
            self.status == self.Status.PENDING_REVIEW or
            self.verification_status == self.VerificationStatus.PENDING_REVIEW
        )

    @property
    def validation_status(self):
        if self.verification_status == self.VerificationStatus.REJECTED or self.status == 'REJECTED':
            return 'REJECTED'
        if self.verification_status == self.VerificationStatus.PENDING_REVIEW or self.status == self.Status.PENDING_REVIEW:
            return 'PENDING'
        return 'VALIDATED' if self.status == self.Status.ACTIVE else 'INACTIVE'


class MedicineBatch(models.Model):
    class BatchStatus(models.TextChoices):
        ACTIVE = 'ACTIVE', 'Active'
        NEAR_EXPIRY = 'NEAR_EXPIRY', 'Near Expiry'
        EXPIRED = 'EXPIRED', 'Expired'
        QUARANTINED = 'QUARANTINED', 'Quarantined'
        RETURNED = 'RETURNED', 'Returned'

    medicine = models.ForeignKey(
        Medicine,
        on_delete=models.CASCADE,
        related_name='batches'
    )
    batch_number = models.CharField(max_length=100)
    manufacturing_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField()
    purchase_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(0)]
    )
    selling_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(0)]
    )
    quantity_received = models.PositiveIntegerField()
    quantity_available = models.PositiveIntegerField(default=0)
    storage_condition = models.CharField(max_length=100, blank=True)
    is_controlled = models.BooleanField(default=False)
    status = models.CharField(
        max_length=20,
        choices=BatchStatus.choices,
        default=BatchStatus.ACTIVE
    )
    received_date = models.DateField(default=timezone.now)

    # Detailed Unit Cost, Landed Costs, MRP, Tax & Discount Fields
    additional_cost = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text="Additional landed costs (freight, packaging, handling) per unit"
    )
    unit_cost = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text="Total unit cost basis = purchase_price + additional_cost"
    )
    mrp = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text="Maximum Retail Price (₹) printed on packaging"
    )
    tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('0.00'),
        blank=True,
        validators=[MinValueValidator(Decimal('0.00')), MaxValueValidator(Decimal('100.00'))],
        help_text="Applicable GST / tax percentage"
    )
    discount_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[MinValueValidator(Decimal('0.00')), MaxValueValidator(Decimal('100.00'))],
        help_text="Batch-specific default discount percentage"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Medicine Batches"
        unique_together = [['medicine', 'batch_number']]

    def __str__(self):
        return f"{self.medicine.medicine_name} - Batch {self.batch_number}"

    def clean(self):
        super().clean()
        if self.purchase_price is not None and self.purchase_price < 0:
            raise ValidationError("Purchase price cannot be negative.")
        if self.selling_price is not None and self.selling_price < 0:
            raise ValidationError("Selling price cannot be negative.")
        if self.additional_cost is not None and self.additional_cost < 0:
            raise ValidationError("Additional cost cannot be negative.")
        if self.mrp is not None and self.mrp > 0:
            if self.selling_price is not None and self.selling_price > self.mrp:
                raise ValidationError("Selling price cannot exceed the Maximum Retail Price (MRP).")
        if self.tax_rate is not None and (self.tax_rate < 0 or self.tax_rate > 100):
            raise ValidationError("Tax rate must be between 0% and 100%.")

    def save(self, *args, **kwargs):
        base_purchase = self.purchase_price or Decimal('0.00')
        add_cost = self.additional_cost or Decimal('0.00')
        if not self.unit_cost or self.unit_cost == 0:
            self.unit_cost = base_purchase + add_cost
        super().save(*args, **kwargs)

    @property
    def gross_unit_margin(self):
        """Gross margin per unit = selling_price - unit_cost."""
        cost = self.unit_cost or self.purchase_price or Decimal('0.00')
        return (self.selling_price or Decimal('0.00')) - cost

    @property
    def gross_unit_margin_pct(self):
        sp = self.selling_price or Decimal('0.00')
        if sp > 0:
            return round((self.gross_unit_margin / sp) * Decimal('100.00'), 2)
        return Decimal('0.00')

    @property
    def batch_id(self):
        return f"BAT-{self.id:04d}" if self.id else ""

    @property
    def manufactured_date(self):
        return self.manufacturing_date

    @property
    def storage_location(self):
        return self.storage_condition

    @property
    def days_until_expiry(self):
        if self.expiry_date:
            return (self.expiry_date - timezone.now().date()).days
        return 0

    @property
    def supplier(self):
        """
        Return associated Supplier instance from GoodsReceiptItem if linked,
        or the medicine's preferred/first catalogue supplier, or None.
        """
        try:
            grn_item = self.grn_item.select_related('goods_receipt__supplier').first()
            if grn_item and grn_item.goods_receipt and grn_item.goods_receipt.supplier:
                return grn_item.goods_receipt.supplier
        except Exception:
            pass
        try:
            pref = self.medicine.supplier_medicines.select_related('supplier').filter(is_preferred=True).first()
            if pref and pref.supplier:
                return pref.supplier
            first_sm = self.medicine.supplier_medicines.select_related('supplier').first()
            if first_sm and first_sm.supplier:
                return first_sm.supplier
        except Exception:
            pass
        return None


class BatchPriceHistory(models.Model):
    """
    Audit log of price and cost changes on medicine batches.
    Preserves historical purchase cost, landed cost, selling price, and MRP.
    """
    batch = models.ForeignKey(
        MedicineBatch,
        on_delete=models.CASCADE,
        related_name='price_histories'
    )
    medicine = models.ForeignKey(
        Medicine,
        on_delete=models.CASCADE,
        related_name='batch_price_histories'
    )
    old_purchase_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    new_purchase_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    old_selling_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    new_selling_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    old_mrp = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    new_mrp = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    old_unit_cost = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    new_unit_cost = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='batch_price_changes'
    )
    reason = models.CharField(max_length=255, blank=True)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Batch Price History"
        verbose_name_plural = "Batch Price Histories"
        ordering = ['-changed_at']
        indexes = [
            models.Index(fields=['batch', '-changed_at']),
            models.Index(fields=['medicine', '-changed_at']),
        ]

    def __str__(self):
        return f"{self.medicine.medicine_name} ({self.batch.batch_number}) price updated at {self.changed_at}"


class MedicineImportLog(models.Model):
    """
    Provenance and audit log for Category Master and Medicine Master CSV/Excel imports.
    Records file metadata, uploader, row counts, and detailed error summaries.
    """
    class ImportType(models.TextChoices):
        CATEGORY_MASTER = 'CATEGORY_MASTER', 'Category Master'
        MEDICINE_IMPORT = 'MEDICINE_IMPORT', 'Medicine Import'
        STARTER_EXAMPLES = 'STARTER_EXAMPLES', 'Starter Examples'
        FULL_WORKBOOK = 'FULL_WORKBOOK', 'Full Workbook (Multi-Sheet)'

    class Status(models.TextChoices):
        SUCCESS = 'SUCCESS', 'Success'
        PARTIAL = 'PARTIAL', 'Partial Success (Some Errors)'
        FAILED = 'FAILED', 'Failed'

    import_type = models.CharField(max_length=50, choices=ImportType.choices)
    filename = models.CharField(max_length=255)
    file_sha256 = models.CharField(max_length=64, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='medicine_imports'
    )
    total_rows = models.PositiveIntegerField(default=0)
    created_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    duplicate_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SUCCESS)
    summary_report = models.JSONField(default=dict, blank=True)
    error_details = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Medicine Import Log"
        verbose_name_plural = "Medicine Import Logs"
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.get_import_type_display()} - {self.filename} ({self.created_at.strftime('%Y-%m-%d %H:%M')})"

