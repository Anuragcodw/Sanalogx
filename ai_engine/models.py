from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator


class AIRecommendation(models.Model):
    """
    AI/ML generated recommendations for procurement and inventory decisions.
    Supports both rule-based (deterministic) and ML model outputs.
    """

    class RecommendationType(models.TextChoices):
        REORDER = 'REORDER', 'Reorder Recommendation'
        SUPPLIER = 'SUPPLIER', 'Supplier Recommendation'
        DEAD_STOCK = 'DEAD_STOCK', 'Dead Stock Detection'
        EXPIRY_RISK = 'EXPIRY_RISK', 'Expiry Risk Warning'
        DEMAND_FORECAST = 'DEMAND_FORECAST', 'Demand Forecast'
        PRICE_TREND = 'PRICE_TREND', 'Price Trend Alert'
        OVERSTOCK = 'OVERSTOCK', 'Overstock Warning'
        SUBSTITUTION = 'SUBSTITUTION', 'Medicine Substitution Suggestion'

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending Review'
        REVIEWED = 'REVIEWED', 'Reviewed'
        ACCEPTED = 'ACCEPTED', 'Accepted'
        REJECTED = 'REJECTED', 'Rejected'
        EXPIRED = 'EXPIRED', 'Expired (No Longer Relevant)'
        AUTO_APPLIED = 'AUTO_APPLIED', 'Auto-Applied'

    class EngineType(models.TextChoices):
        RULE_BASED = 'RULE_BASED', 'Rule-Based Logic'
        ML_MODEL = 'ML_MODEL', 'Machine Learning Model'
        HYBRID = 'HYBRID', 'Hybrid (Rules + ML)'

    # Context
    recommendation_type = models.CharField(
        max_length=25, choices=RecommendationType.choices
    )
    engine_type = models.CharField(
        max_length=15, choices=EngineType.choices,
        default=EngineType.RULE_BASED
    )

    medicine = models.ForeignKey(
        'medicines.Medicine', on_delete=models.CASCADE,
        null=True, blank=True, related_name='ai_recommendations'
    )
    supplier = models.ForeignKey(
        'suppliers.Supplier', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='ai_recommendations'
    )

    # Recommendation details
    title = models.CharField(max_length=255)
    message = models.TextField(help_text="Human-readable recommendation explanation")
    action_data = models.JSONField(
        default=dict,
        help_text="Machine-readable structured data for auto-applying recommendation"
    )
    source_data = models.JSONField(
        default=dict,
        blank=True,
        help_text="Explainability source data and intermediate calculation metrics"
    )

    # Confidence and impact
    confidence_score = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Model confidence score (0–100)"
    )
    priority_score = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Business urgency/priority score (0–100)"
    )

    # Specific recommendation values
    recommended_quantity = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="For reorder recommendations: suggested quantity"
    )
    recommended_supplier = models.ForeignKey(
        'suppliers.Supplier', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='recommended_in'
    )
    forecasted_demand_30d = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Forecasted demand for next 30 days"
    )
    forecasted_demand_60d = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Forecasted demand for next 60 days"
    )

    status = models.CharField(
        max_length=15, choices=Status.choices,
        default=Status.PENDING
    )

    # Audit
    generated_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(
        null=True, blank=True,
        help_text="After this datetime the recommendation is no longer actionable"
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='ai_recommendations_reviewed'
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        verbose_name = "AI Recommendation"
        verbose_name_plural = "AI Recommendations"
        ordering = ['-priority_score', '-generated_at']
        indexes = [
            models.Index(fields=['recommendation_type', 'status']),
            models.Index(fields=['medicine']),
            models.Index(fields=['-generated_at']),
        ]

    def __str__(self):
        med = self.medicine.generic_name if self.medicine else 'General'
        return f"[{self.get_recommendation_type_display()}] {med} — {self.title}"


class AuditLog(models.Model):
    """
    Immutable system-wide audit trail for all significant actions.
    Compliant with pharmacy regulatory record-keeping requirements.
    """

    class ActionType(models.TextChoices):
        # Auth
        USER_LOGIN = 'USER_LOGIN', 'User Login'
        USER_LOGOUT = 'USER_LOGOUT', 'User Logout'
        USER_REGISTER = 'USER_REGISTER', 'User Registration'
        USER_APPROVED = 'USER_APPROVED', 'User Approved'
        USER_REJECTED = 'USER_REJECTED', 'User Rejected'
        PASSWORD_CHANGED = 'PASSWORD_CHANGED', 'Password Changed'

        # Medicine
        MEDICINE_CREATED = 'MEDICINE_CREATED', 'Medicine Created'
        MEDICINE_UPDATED = 'MEDICINE_UPDATED', 'Medicine Updated'
        MEDICINE_DEACTIVATED = 'MEDICINE_DEACTIVATED', 'Medicine Deactivated'

        # Inventory
        INVENTORY_ADJUSTED = 'INVENTORY_ADJUSTED', 'Inventory Adjusted'
        BATCH_QUARANTINED = 'BATCH_QUARANTINED', 'Batch Quarantined'
        BATCH_RETURNED = 'BATCH_RETURNED', 'Batch Returned to Supplier'
        EXPIRY_WRITEOFF = 'EXPIRY_WRITEOFF', 'Expiry Write-Off'

        # Purchases
        PO_CREATED = 'PO_CREATED', 'PO Created'
        PO_APPROVED = 'PO_APPROVED', 'PO Approved'
        PO_CANCELLED = 'PO_CANCELLED', 'PO Cancelled'
        GRN_CREATED = 'GRN_CREATED', 'GRN Created'
        GRN_VERIFIED = 'GRN_VERIFIED', 'GRN Verified'

        # Sales
        SALE_COMPLETED = 'SALE_COMPLETED', 'Sale Completed'
        SALE_RETURNED = 'SALE_RETURNED', 'Sale Returned'

        # Suppliers
        SUPPLIER_CREATED = 'SUPPLIER_CREATED', 'Supplier Created'
        SUPPLIER_UPDATED = 'SUPPLIER_UPDATED', 'Supplier Updated'
        PRICE_UPDATED = 'PRICE_UPDATED', 'Supplier Price Updated'

        # Reports
        REPORT_GENERATED = 'REPORT_GENERATED', 'Report Generated'

    action_type = models.CharField(max_length=30, choices=ActionType.choices)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='ai_audit_logs'
    )

    # Context
    object_type = models.CharField(max_length=50, blank=True)
    object_id = models.PositiveBigIntegerField(null=True, blank=True)
    object_repr = models.CharField(
        max_length=255, blank=True,
        help_text="Human-readable representation of the object at time of action"
    )

    # Change details
    changes = models.JSONField(
        default=dict,
        help_text="Dict of {field: [old_value, new_value]} for update actions"
    )
    description = models.TextField(blank=True)

    # Request metadata
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=500, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Audit Log"
        verbose_name_plural = "Audit Logs"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['action_type']),
            models.Index(fields=['performed_by', '-created_at']),
            models.Index(fields=['object_type', 'object_id']),
        ]

    def __str__(self):
        user = self.performed_by.username if self.performed_by else 'System'
        return f"{self.get_action_type_display()} by {user} at {self.created_at}"

    @classmethod
    def log(cls, action_type, performed_by=None, object_type='', object_id=None,
            object_repr='', changes=None, description='', ip_address=None, user_agent=''):
        """Convenience factory for creating audit log entries."""
        return cls.objects.create(
            action_type=action_type,
            performed_by=performed_by,
            object_type=object_type,
            object_id=object_id,
            object_repr=object_repr,
            changes=changes or {},
            description=description,
            ip_address=ip_address,
            user_agent=user_agent,
        )


class VoiceCommandLog(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='voice_command_logs')
    raw_text = models.CharField(max_length=500)
    intent = models.CharField(max_length=100)
    confidence = models.FloatField(default=0.0)
    response_text = models.TextField(blank=True, default='')
    action_url = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Voice Command Log"
        verbose_name_plural = "Voice Command Logs"
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.intent}] {self.user.username}: '{self.raw_text}' ({self.confidence:.2f})"

