from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.utils import timezone


class Inventory(models.Model):
    class StockStatus(models.TextChoices):
        OUT_OF_STOCK = 'OUT_OF_STOCK', 'Out of Stock'
        CRITICAL = 'CRITICAL', 'Critical Stock'
        LOW_STOCK = 'LOW_STOCK', 'Low Stock'
        NORMAL = 'NORMAL', 'Normal Stock'
        OVERSTOCK = 'OVERSTOCK', 'Overstock'

    medicine = models.OneToOneField(
        'medicines.Medicine',
        on_delete=models.CASCADE,
        related_name='inventory'
    )
    current_stock = models.PositiveIntegerField(default=0)
    available_stock = models.PositiveIntegerField(default=0)
    minimum_stock = models.PositiveIntegerField(default=10)
    maximum_stock = models.PositiveIntegerField(default=500)
    reorder_level = models.PositiveIntegerField(default=50)
    safety_stock = models.PositiveIntegerField(default=20)
    
    stock_status = models.CharField(
        max_length=20,
        choices=StockStatus.choices,
        default=StockStatus.OUT_OF_STOCK
    )
    last_updated = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Inventory"
        verbose_name_plural = "Inventories"

    def __str__(self):
        return f"{self.medicine.medicine_name} — {self.current_stock} units"

    def clean(self):
        if self.current_stock < 0:
            raise ValidationError("Current stock cannot be negative.")
        if self.available_stock < 0:
            raise ValidationError("Available stock cannot be negative.")

    def save(self, *args, **kwargs):
        self.clean()
        # Automatically calculate stock status
        if self.current_stock == 0:
            self.stock_status = self.StockStatus.OUT_OF_STOCK
        elif self.current_stock <= self.safety_stock:
            self.stock_status = self.StockStatus.CRITICAL
        elif self.current_stock <= self.reorder_level:
            self.stock_status = self.StockStatus.LOW_STOCK
        elif self.current_stock > self.maximum_stock:
            self.stock_status = self.StockStatus.OVERSTOCK
        else:
            self.stock_status = self.StockStatus.NORMAL
        super().save(*args, **kwargs)

    @property
    def is_low_stock(self):
        return self.stock_status in (self.StockStatus.LOW_STOCK, self.StockStatus.CRITICAL)

    @property
    def is_out_of_stock(self):
        return self.stock_status == self.StockStatus.OUT_OF_STOCK

    @property
    def is_overstock(self):
        return self.stock_status == self.StockStatus.OVERSTOCK


class StockTransaction(models.Model):
    class TransactionType(models.TextChoices):
        OPENING = 'OPENING', 'Opening Stock'
        PURCHASE = 'PURCHASE', 'Purchase Receipt'
        SALE = 'SALE', 'Sale'
        DISPENSE = 'DISPENSE', 'Prescription Dispense'
        RETURN = 'RETURN', 'Return'
        ADJUSTMENT = 'ADJUSTMENT', 'Stock Adjustment'
        DAMAGE = 'DAMAGE', 'Damaged Stock'
        EXPIRY = 'EXPIRY', 'Expiry Write-off'
        MANUAL_UPDATE = 'MANUAL_UPDATE', 'Manual Update'
        HISTORICAL_IMPORT = 'HISTORICAL_IMPORT', 'Historical Import'

    inventory = models.ForeignKey(
        Inventory,
        on_delete=models.CASCADE,
        related_name='transactions'
    )
    medicine = models.ForeignKey(
        'medicines.Medicine',
        on_delete=models.CASCADE,
        related_name='stock_transactions'
    )
    batch = models.ForeignKey(
        'medicines.MedicineBatch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='stock_transactions'
    )
    transaction_type = models.CharField(
        max_length=20,
        choices=TransactionType.choices
    )
    transaction_number = models.CharField(max_length=100, blank=True, default='', db_index=True, help_text="Unique transaction reference e.g. TX-2026-00001")
    quantity = models.PositiveIntegerField(help_text="Positive quantity moved")
    previous_stock = models.IntegerField(default=0)
    new_stock = models.IntegerField(default=0)
    reference_number = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='stock_transactions'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.transaction_type} | {self.medicine.medicine_name} | {self.quantity}"


class ConsumptionCache(models.Model):
    """
    Performance caching for high-speed dashboard and analytics queries.
    Stores precomputed 7/14/30/90-day consumption velocity per medicine.
    """
    medicine = models.OneToOneField(
        'medicines.Medicine',
        on_delete=models.CASCADE,
        related_name='consumption_cache'
    )
    consumed_7d = models.PositiveIntegerField(default=0)
    consumed_14d = models.PositiveIntegerField(default=0)
    consumed_30d = models.PositiveIntegerField(default=0)
    consumed_90d = models.PositiveIntegerField(default=0)
    daily_avg_30d = models.DecimalField(max_digits=8, decimal_places=2, default=0.0)
    data_quality = models.CharField(max_length=20, default='GOOD')
    last_movement_date = models.DateField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Consumption Cache"
        verbose_name_plural = "Consumption Caches"

    def __str__(self):
        return f"{self.medicine.medicine_name} Cache (30d: {self.consumed_30d})"


class StockAlert(models.Model):
    class AlertType(models.TextChoices):
        LOW_STOCK = 'LOW_STOCK', 'Low Stock'
        CRITICAL_STOCK = 'CRITICAL_STOCK', 'Critical Stock'
        OUT_OF_STOCK = 'OUT_OF_STOCK', 'Out of Stock'
        NEAR_EXPIRY = 'NEAR_EXPIRY', 'Near Expiry'
        EXPIRED = 'EXPIRED', 'Expired Batch'

    class AlertSeverity(models.TextChoices):
        INFO = 'INFO', 'Info'
        WARNING = 'WARNING', 'Warning'
        CRITICAL = 'CRITICAL', 'Critical'

    medicine = models.ForeignKey(
        'medicines.Medicine',
        on_delete=models.CASCADE,
        related_name='stock_alerts'
    )
    batch = models.ForeignKey(
        'medicines.MedicineBatch',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='alerts'
    )
    alert_type = models.CharField(max_length=25, choices=AlertType.choices)
    severity = models.CharField(
        max_length=10,
        choices=AlertSeverity.choices,
        default=AlertSeverity.WARNING
    )
    message = models.TextField()
    is_active = models.BooleanField(default=True)
    is_acknowledged = models.BooleanField(default=False)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='acknowledged_alerts'
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    recommended_reorder_qty = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = [['medicine', 'alert_type', 'is_active']]

    def __str__(self):
        return f"[{self.severity}] {self.alert_type} — {self.medicine.medicine_name}"


class DailyConsumptionSummary(models.Model):
    """
    Precomputed daily net consumption per medicine.
    Always rebuildable from StockTransaction records — never an independent source of truth.
    Idempotent: unique_together (medicine, date) prevents duplicate aggregates.
    Stores separate sale_qty and dispense_qty for auditability, but net_consumption
    is the deduplicated total (SALE+DISPENSE minus RETURN, same-reference deduped).
    Does NOT affect physical stock balances.
    """
    medicine = models.ForeignKey(
        'medicines.Medicine',
        on_delete=models.CASCADE,
        related_name='daily_consumption'
    )
    date = models.DateField(db_index=True)
    sale_qty = models.PositiveIntegerField(default=0, help_text="Raw SALE transaction quantity for this day")
    dispense_qty = models.PositiveIntegerField(default=0, help_text="Raw DISPENSE transaction quantity for this day")
    return_qty = models.PositiveIntegerField(default=0, help_text="RETURN quantity reducing consumption")
    net_consumption = models.PositiveIntegerField(default=0, help_text="Deduplicated net consumption")
    transaction_count = models.PositiveIntegerField(default=0)
    data_source = models.CharField(
        max_length=30,
        default='TRANSACTION',
        help_text="TRANSACTION = from stock transactions; IMPORTED = from historical import"
    )
    last_calculated = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [['medicine', 'date']]
        ordering = ['-date']
        indexes = [
            models.Index(fields=['medicine', 'date']),
            models.Index(fields=['date']),
        ]

    def __str__(self):
        return f"{self.medicine.medicine_name} | {self.date} | net={self.net_consumption}"


class HistoricalImportLog(models.Model):
    """
    Provenance record for each historical consumption CSV/XLSX import batch.
    Links imported StockTransaction records back to their source file.
    """
    class ImportStatus(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        COMPLETED = 'COMPLETED', 'Completed'
        FAILED = 'FAILED', 'Failed'
        PARTIAL = 'PARTIAL', 'Partial'

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='import_logs'
    )
    filename = models.CharField(max_length=255)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=ImportStatus.choices, default=ImportStatus.PENDING)
    total_rows = models.PositiveIntegerField(default=0)
    valid_rows = models.PositiveIntegerField(default=0)
    error_rows = models.PositiveIntegerField(default=0)
    imported_rows = models.PositiveIntegerField(default=0)
    import_batch_ref = models.CharField(
        max_length=64, unique=True,
        help_text="Fingerprint used to detect duplicate imports of the same file"
    )
    row_errors = models.JSONField(default=list, blank=True)
    notes = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return f"Import: {self.filename} by {self.uploaded_by} ({self.status})"
