import secrets
from django.db import models
from django.conf import settings
from django.utils import timezone


def generate_verification_token():
    return secrets.token_urlsafe(32)


class ReportRecord(models.Model):
    """
    Official generated report record. Every PDF/Excel/CSV export is tracked here.
    Provides unique Report ID, QR verification token, audit trail.
    """

    class ReportType(models.TextChoices):
        INVENTORY = 'INVENTORY', 'Inventory Report'
        SALES = 'SALES', 'Sales Report'
        PURCHASE = 'PURCHASE', 'Purchase Report'
        SUPPLIER = 'SUPPLIER', 'Supplier Report'
        EXPIRY = 'EXPIRY', 'Expiry Report'
        LOW_STOCK = 'LOW_STOCK', 'Low Stock Report'
        PROFIT = 'PROFIT', 'Profit Report'
        REVENUE = 'REVENUE', 'Revenue Report'
        PRICE_HISTORY = 'PRICE_HISTORY', 'Price History Report'
        AI_PREDICTION = 'AI_PREDICTION', 'AI Prediction Report'
        STOCK_MOVEMENT = 'STOCK_MOVEMENT', 'Stock Movement Report'
        CONTROLLED = 'CONTROLLED', 'Controlled Medicine Report'
        DISPENSING = 'DISPENSING', 'Dispensing & Traceability Report'
        CONSUMPTION = 'CONSUMPTION', 'Consumption Analytics Report'
        OPTIMIZATION = 'OPTIMIZATION', 'Inventory Optimization Report'
        HOSPITAL_INTEGRATION = 'HOSPITAL_INTEGRATION', 'Hospital & EHR Integration Report'
        REORDER_RECOMMENDATIONS = 'REORDER_RECOMMENDATIONS', 'Reorder Recommendations Report'
        DEMAND_FORECAST = 'DEMAND_FORECAST', 'Demand Forecast Report'
        DEAD_STOCK = 'DEAD_STOCK', 'Dead Stock Report'
        OVERSTOCK = 'OVERSTOCK', 'Overstock Report'
        EXPIRY_RISK = 'EXPIRY_RISK', 'Expiry Risk Report'

    class ReportFormat(models.TextChoices):
        PDF = 'PDF', 'PDF'
        EXCEL = 'EXCEL', 'Excel'
        CSV = 'CSV', 'CSV'

    class ReportStatus(models.TextChoices):
        GENERATED = 'GENERATED', 'Generated'
        DOWNLOADED = 'DOWNLOADED', 'Downloaded'
        EMAILED = 'EMAILED', 'Emailed'
        FAILED = 'FAILED', 'Failed'

    report_id = models.CharField(max_length=20, unique=True, db_index=True)
    report_type = models.CharField(max_length=30, choices=ReportType.choices)
    format = models.CharField(max_length=10, choices=ReportFormat.choices, default=ReportFormat.PDF)
    status = models.CharField(max_length=15, choices=ReportStatus.choices, default=ReportStatus.GENERATED)

    # Date range for the report data
    date_from = models.DateField(null=True, blank=True)
    date_to = models.DateField(null=True, blank=True)

    # Serialized filter parameters (JSON)
    filters_json = models.JSONField(default=dict)

    # Who generated it
    generated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='generated_reports'
    )
    generated_at = models.DateTimeField(auto_now_add=True)

    # QR verification token — stored securely, NOT in QR directly
    # QR contains: MEDIVAULT_BASE_URL/reports/verify/<verification_token>/
    verification_token = models.CharField(
        max_length=64, unique=True,
        default=generate_verification_token
    )

    # Email delivery tracking
    emailed_to = models.EmailField(blank=True)
    emailed_at = models.DateTimeField(null=True, blank=True)

    # Download tracking
    download_count = models.PositiveIntegerField(default=0)
    last_downloaded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Report Record"
        verbose_name_plural = "Report Records"
        ordering = ['-generated_at']
        indexes = [
            models.Index(fields=['report_type', '-generated_at']),
            models.Index(fields=['generated_by']),
            models.Index(fields=['verification_token']),
        ]

    def __str__(self):
        return f"{self.report_id} — {self.get_report_type_display()}"

    def save(self, *args, **kwargs):
        if not self.report_id:
            self.report_id = self._generate_report_id()
        super().save(*args, **kwargs)

    @staticmethod
    def _generate_report_id():
        year = timezone.now().year
        last = ReportRecord.objects.filter(
            report_id__startswith=f'RPT-{year}-'
        ).count()
        return f'RPT-{year}-{str(last + 1).zfill(5)}'

    def get_verification_url(self):
        base_url = getattr(settings, 'MEDIVAULT_BASE_URL', 'http://127.0.0.1:8000')
        return f"{base_url}/reports/verify/{self.verification_token}/"

    def record_download(self):
        self.download_count += 1
        self.last_downloaded_at = timezone.now()
        if self.status == self.ReportStatus.GENERATED:
            self.status = self.ReportStatus.DOWNLOADED
        self.save(update_fields=['download_count', 'last_downloaded_at', 'status'])


class ReportSchedule(models.Model):
    """
    Admin-configured scheduled reports — daily, weekly, monthly email delivery.
    """

    class Frequency(models.TextChoices):
        DAILY = 'DAILY', 'Daily'
        WEEKLY = 'WEEKLY', 'Weekly'
        MONTHLY = 'MONTHLY', 'Monthly'

    report_type = models.CharField(max_length=30, choices=ReportRecord.ReportType.choices)
    frequency = models.CharField(max_length=10, choices=Frequency.choices)
    format = models.CharField(max_length=10, choices=ReportRecord.ReportFormat.choices, default='PDF')

    recipient_email = models.EmailField(help_text="Authorized recipient email address")
    recipient_name = models.CharField(max_length=100, blank=True)

    is_active = models.BooleanField(default=True)

    # For weekly schedules: 0=Monday … 6=Sunday
    day_of_week = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text="For weekly: 0=Monday, 6=Sunday"
    )
    # For monthly schedules: 1–28
    day_of_month = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text="For monthly: day of month (1–28)"
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='report_schedules'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    next_send_at = models.DateTimeField(null=True, blank=True)

    # Track last send status
    last_send_status = models.CharField(max_length=10, default='PENDING')
    last_error = models.TextField(blank=True)

    class Meta:
        verbose_name = "Report Schedule"
        verbose_name_plural = "Report Schedules"
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.get_frequency_display()} {self.get_report_type_display()} → {self.recipient_email}"
