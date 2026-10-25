from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from django.conf import settings


class Supplier(models.Model):
    """
    Internal business entity representing a medicine supplier/vendor.
    NOT a login role — managed by Admin/Pharmacist.
    """

    class PaymentTerms(models.TextChoices):
        IMMEDIATE = 'IMMEDIATE', 'Immediate Payment'
        NET_15 = 'NET_15', 'Net 15 Days'
        NET_30 = 'NET_30', 'Net 30 Days'
        NET_45 = 'NET_45', 'Net 45 Days'
        NET_60 = 'NET_60', 'Net 60 Days'
        COD = 'COD', 'Cash on Delivery'

    class SupplierStatus(models.TextChoices):
        ACTIVE = 'ACTIVE', 'Active'
        INACTIVE = 'INACTIVE', 'Inactive'
        BLACKLISTED = 'BLACKLISTED', 'Blacklisted'
        UNDER_REVIEW = 'UNDER_REVIEW', 'Under Review'

    # Identification
    company_name = models.CharField(max_length=255, unique=True)
    contact_person = models.CharField(max_length=255, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    alternate_phone = models.CharField(max_length=30, blank=True)

    # Address
    address_line1 = models.CharField(max_length=255, blank=True)
    address_line2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, default='India')
    postal_code = models.CharField(max_length=20, blank=True)

    # Business details
    registration_number = models.CharField(
        max_length=100, blank=True,
        help_text="Drug license / GST / business registration number"
    )
    drug_license_number = models.CharField(max_length=100, blank=True)
    gst_number = models.CharField(max_length=20, blank=True)

    # Performance metrics (updated automatically from purchase orders)
    average_delivery_days = models.PositiveSmallIntegerField(
        default=7,
        help_text="Average number of days to deliver after order placement"
    )
    on_time_delivery_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=100.00,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Percentage of orders delivered on or before promised date"
    )
    fill_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=100.00,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Percentage of ordered quantity actually supplied"
    )
    rating = models.DecimalField(
        max_digits=3, decimal_places=1, default=3.0,
        validators=[MinValueValidator(0), MaxValueValidator(5)],
        help_text="Overall rating out of 5 (manually set or auto-calculated)"
    )

    # Business terms
    payment_terms = models.CharField(
        max_length=20, choices=PaymentTerms.choices,
        default=PaymentTerms.NET_30
    )
    minimum_order_value = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        validators=[MinValueValidator(0)]
    )

    # Status
    status = models.CharField(
        max_length=20, choices=SupplierStatus.choices,
        default=SupplierStatus.ACTIVE
    )
    notes = models.TextField(blank=True)

    # Audit
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='suppliers_created'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Supplier"
        verbose_name_plural = "Suppliers"
        ordering = ['company_name']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['company_name']),
        ]

    def __str__(self):
        return self.company_name

    @property
    def supplier_score(self):
        """
        Composite supplier intelligence score (0–100).
        Formula:
          - Price competitiveness: 40% weight (lower avg price = higher score)
            Computed relative to avg price across all suppliers for same medicine.
            Stored here as a cached float; recalculated by the analytics module.
          - On-time delivery rate: 20% weight
          - Fill rate (availability): 20% weight
          - Overall rating: 20% weight (normalized to 0–100)
        This is an indicative score; detailed per-medicine scores are in SupplierMedicine.
        """
        delivery_score = float(self.on_time_delivery_rate)          # 0-100
        availability_score = float(self.fill_rate)                  # 0-100
        rating_score = (float(self.rating) / 5.0) * 100            # 0-100

        # Price score contribution needs medicine context; use 50 as neutral placeholder
        price_score = 50.0

        return round(
            (price_score * 0.40) +
            (delivery_score * 0.20) +
            (availability_score * 0.20) +
            (rating_score * 0.20),
            2
        )


class SupplierMedicine(models.Model):
    """
    Catalogue of medicines a specific supplier can provide,
    along with their pricing and delivery specifics.
    """
    supplier = models.ForeignKey(
        Supplier, on_delete=models.CASCADE,
        related_name='supplier_medicines'
    )
    medicine = models.ForeignKey(
        'medicines.Medicine', on_delete=models.CASCADE,
        related_name='supplier_medicines'
    )

    # Pricing
    unit_price = models.DecimalField(
        max_digits=10, decimal_places=2,
        validators=[MinValueValidator(0)],
        help_text="Current price per unit offered by this supplier"
    )
    minimum_order_quantity = models.PositiveIntegerField(
        default=1,
        help_text="Minimum quantity that must be ordered"
    )
    pack_size = models.PositiveIntegerField(
        default=1,
        help_text="Units per pack (e.g., 10 tablets per strip)"
    )

    # Lead time
    delivery_days = models.PositiveSmallIntegerField(
        default=7,
        help_text="Expected delivery days from order date"
    )

    # Availability
    is_available = models.BooleanField(
        default=True,
        help_text="Whether this supplier currently has the medicine in stock"
    )
    is_preferred = models.BooleanField(
        default=False,
        help_text="Flagged as preferred supplier for this medicine"
    )

    # Per-medicine score (calculated by analytics module)
    price_score = models.DecimalField(
        max_digits=5, decimal_places=2, default=50.00,
        help_text="Price competitiveness score for this medicine (0–100)"
    )
    composite_score = models.DecimalField(
        max_digits=5, decimal_places=2, default=50.00,
        help_text="Weighted composite score for this supplier-medicine pair (0–100)"
    )

    notes = models.CharField(max_length=255, blank=True)
    last_price_update = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Supplier Medicine"
        verbose_name_plural = "Supplier Medicines"
        unique_together = [['supplier', 'medicine']]
        ordering = ['composite_score']  # Best score first

    def __str__(self):
        return f"{self.supplier.company_name} → {self.medicine.generic_name} @ ₹{self.unit_price}"

    def calculate_composite_score(self, all_prices_for_medicine=None):
        """
        Calculate this supplier-medicine pair's composite score.
        Args:
            all_prices_for_medicine: list of all unit_prices for same medicine across suppliers.
                                      If None, uses self.unit_price as 50 (neutral).
        """
        # Price score (40%): lower price = higher score
        if all_prices_for_medicine and len(all_prices_for_medicine) > 1:
            min_price = min(all_prices_for_medicine)
            max_price = max(all_prices_for_medicine)
            price_range = max_price - min_price
            if price_range > 0:
                price_score = ((max_price - float(self.unit_price)) / price_range) * 100
            else:
                price_score = 100.0
        else:
            price_score = float(self.price_score)

        # Delivery score (20%): fewer days = higher score (assuming max 30 days)
        max_days = 30
        delivery_score = max(0, ((max_days - self.delivery_days) / max_days) * 100)

        # Availability score (20%)
        availability_score = float(self.supplier.fill_rate)

        # Rating score (20%)
        rating_score = (float(self.supplier.rating) / 5.0) * 100

        composite = (
            (price_score * 0.40) +
            (delivery_score * 0.20) +
            (availability_score * 0.20) +
            (rating_score * 0.20)
        )
        return round(composite, 2)


class SupplierPriceHistory(models.Model):
    """Tracks price changes for a supplier-medicine pair over time."""
    supplier = models.ForeignKey(
        Supplier, on_delete=models.CASCADE,
        related_name='price_history'
    )
    medicine = models.ForeignKey(
        'medicines.Medicine', on_delete=models.CASCADE,
        related_name='supplier_price_history'
    )

    old_price = models.DecimalField(
        max_digits=10, decimal_places=2,
        validators=[MinValueValidator(0)]
    )
    new_price = models.DecimalField(
        max_digits=10, decimal_places=2,
        validators=[MinValueValidator(0)]
    )
    price_change_pct = models.DecimalField(
        max_digits=7, decimal_places=2, default=0,
        help_text="Percentage change (positive = increase, negative = decrease)"
    )

    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True
    )
    reason = models.CharField(max_length=255, blank=True)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Supplier Price History"
        verbose_name_plural = "Supplier Price Histories"
        ordering = ['-changed_at']
        indexes = [
            models.Index(fields=['supplier', 'medicine']),
            models.Index(fields=['changed_at']),
        ]

    def save(self, *args, **kwargs):
        if self.old_price and self.new_price and self.old_price != 0:
            self.price_change_pct = ((self.new_price - self.old_price) / self.old_price) * 100
        super().save(*args, **kwargs)

    def __str__(self):
        direction = "↑" if self.new_price > self.old_price else "↓"
        return (
            f"{self.supplier.company_name} | {self.medicine.generic_name} "
            f"{direction} ₹{self.old_price} → ₹{self.new_price}"
        )
