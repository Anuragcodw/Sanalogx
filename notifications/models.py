from django.db import models
from django.conf import settings


class Notification(models.Model):
    """
    In-app notification sent to one or all users.
    Covers: stock alerts, PO status changes, approvals, expiry warnings, AI recommendations, etc.
    Deduplication via fingerprint field — prevents repeat notifications for same event.
    """

    class NotificationType(models.TextChoices):
        # Stock & Inventory
        LOW_STOCK = 'LOW_STOCK', 'Low Stock Alert'
        CRITICAL_STOCK = 'CRITICAL_STOCK', 'Critical Stock Alert'
        OUT_OF_STOCK = 'OUT_OF_STOCK', 'Out of Stock Alert'
        OVERSTOCK = 'OVERSTOCK', 'Overstock Warning'
        NEAR_EXPIRY = 'NEAR_EXPIRY', 'Near Expiry Warning'
        EXPIRED_BATCH = 'EXPIRED_BATCH', 'Expired Batch Detected'
        EXPIRY_RISK = 'EXPIRY_RISK', 'Expiry Risk Alert'
        REORDER_SUGGESTED = 'REORDER_SUGGESTED', 'Reorder Suggested'

        # Purchase Order
        PO_CREATED = 'PO_CREATED', 'Purchase Order Created'
        PO_AWAITING_APPROVAL = 'PO_AWAITING_APPROVAL', 'PO Awaiting Approval'
        PO_APPROVED = 'PO_APPROVED', 'Purchase Order Approved'
        PO_REJECTED = 'PO_REJECTED', 'Purchase Order Rejected'
        PO_SENT = 'PO_SENT', 'PO Sent to Supplier'
        PO_DELIVERED = 'PO_DELIVERED', 'PO Delivery Received'
        PO_OVERDUE = 'PO_OVERDUE', 'Purchase Order Overdue'
        GRN_CREATED = 'GRN_CREATED', 'Goods Receipt Created'

        # Supplier
        PRICE_DROP = 'PRICE_DROP', 'Supplier Price Drop'
        PRICE_INCREASE = 'PRICE_INCREASE', 'Supplier Price Increase'
        SUPPLIER_DELAY = 'SUPPLIER_DELAY', 'Supplier Delivery Delay'

        # User management
        PHARMACIST_REGISTERED = 'PHARMACIST_REGISTERED', 'New Pharmacist Registration'
        PHARMACIST_APPROVED = 'PHARMACIST_APPROVED', 'Pharmacist Approved'
        PHARMACIST_REJECTED = 'PHARMACIST_REJECTED', 'Pharmacist Rejected'

        # AI / Recommendations
        AI_RECOMMENDATION = 'AI_RECOMMENDATION', 'AI Recommendation'
        AI_DEAD_STOCK = 'AI_DEAD_STOCK', 'AI Dead Stock Detection'
        AI_OVERSTOCK = 'AI_OVERSTOCK', 'AI Overstock Warning'
        AI_HIGH_DEMAND = 'AI_HIGH_DEMAND', 'AI High Demand Prediction'

        # Reports
        REPORT_GENERATED = 'REPORT_GENERATED', 'Report Generated'

        # System
        SYSTEM = 'SYSTEM', 'System Notification'

    class Priority(models.TextChoices):
        INFO = 'INFO', 'Info'
        LOW = 'LOW', 'Low'
        MEDIUM = 'MEDIUM', 'Medium'
        HIGH = 'HIGH', 'High'
        CRITICAL = 'CRITICAL', 'Critical'

    class Status(models.TextChoices):
        UNREAD = 'UNREAD', 'Unread'
        READ = 'READ', 'Read'
        ARCHIVED = 'ARCHIVED', 'Archived'

    # Target: either a specific user or broadcast to all (recipient = None)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        null=True, blank=True, related_name='notifications',
        help_text="Target user. If None, this is a broadcast to all staff."
    )

    notification_type = models.CharField(max_length=30, choices=NotificationType.choices)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.UNREAD)

    title = models.CharField(max_length=255)
    message = models.TextField()

    # Contextual link (e.g., medicine detail, PO detail)
    action_url = models.CharField(max_length=500, blank=True)
    action_label = models.CharField(max_length=100, blank=True)

    # Related object references (generic)
    related_object_type = models.CharField(max_length=50, blank=True)
    related_object_id = models.PositiveBigIntegerField(null=True, blank=True)

    # Deduplication fingerprint — unique key per event to prevent repeat notifications
    # Format: "{notification_type}:{object_type}:{object_id}:{date_key}"
    fingerprint = models.CharField(max_length=255, blank=True, db_index=True)

    # Legacy field (kept for backwards compatibility)
    is_read = models.BooleanField(default=False)
    is_archived = models.BooleanField(default=False)
    email_sent = models.BooleanField(default=False)

    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Notification"
        verbose_name_plural = "Notifications"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['recipient', 'status']),
            models.Index(fields=['recipient', 'is_read']),
            models.Index(fields=['notification_type']),
            models.Index(fields=['-created_at']),
            models.Index(fields=['fingerprint']),
        ]

    def __str__(self):
        target = self.recipient.username if self.recipient else 'ALL'
        return f"[{self.priority}] {self.title} → {target}"

    def mark_read(self):
        from django.utils import timezone
        if not self.is_read:
            self.is_read = True
            self.status = self.Status.READ
            self.read_at = timezone.now()
            self.save(update_fields=['is_read', 'status', 'read_at'])

    def archive(self):
        self.is_archived = True
        self.status = self.Status.ARCHIVED
        self.save(update_fields=['is_archived', 'status'])

    @classmethod
    def fingerprint_exists(cls, fingerprint, cooldown_seconds=None):
        """
        Check if a notification with this fingerprint was already sent.
        If cooldown_seconds is provided, only matches notifications sent within that cooldown window.
        """
        if not fingerprint:
            return False
        qs = cls.objects.filter(fingerprint=fingerprint)
        if cooldown_seconds is not None and cooldown_seconds > 0:
            from django.utils import timezone
            from datetime import timedelta
            cutoff = timezone.now() - timedelta(seconds=cooldown_seconds)
            return qs.filter(created_at__gte=cutoff).exists()
        return qs.exists()

    @classmethod
    def create_broadcast(cls, notification_type, title, message, priority='MEDIUM',
                         action_url='', action_label='', related_object_type='',
                         related_object_id=None, fingerprint='', cooldown_seconds=None):
        """Create a notification visible to all staff (recipient=None)."""
        if fingerprint and cls.fingerprint_exists(fingerprint, cooldown_seconds=cooldown_seconds):
            return None  # Deduplicated
        return cls.objects.create(
            recipient=None,
            notification_type=notification_type,
            title=title, message=message, priority=priority,
            action_url=action_url,
            action_label=action_label,
            related_object_type=related_object_type,
            related_object_id=related_object_id,
            fingerprint=fingerprint,
        )

    @classmethod
    def create_for_user(cls, user, notification_type, title, message,
                        priority='MEDIUM', action_url='', action_label='',
                        related_object_type='', related_object_id=None,
                        fingerprint='', cooldown_seconds=None):
        """Create a notification targeted at a specific user."""
        if fingerprint and cls.fingerprint_exists(fingerprint, cooldown_seconds=cooldown_seconds):
            return None  # Deduplicated
        return cls.objects.create(
            recipient=user,
            notification_type=notification_type,
            title=title, message=message, priority=priority,
            action_url=action_url,
            action_label=action_label,
            related_object_type=related_object_type,
            related_object_id=related_object_id,
            fingerprint=fingerprint,
        )

    @classmethod
    def create_for_role(cls, role, notification_type, title, message,
                        priority='MEDIUM', action_url='', action_label='',
                        related_object_type='', related_object_id=None,
                        fingerprint='', cooldown_seconds=None):
        """Create notifications for all users with a given role."""
        from accounts.models import User
        if fingerprint and cls.fingerprint_exists(fingerprint, cooldown_seconds=cooldown_seconds):
            return []  # Deduplicated
        users = User.objects.filter(role=role, status='APPROVED', is_active=True)
        created = []
        for user in users:
            user_fp = f"{fingerprint}:user:{user.id}" if fingerprint else ''
            if user_fp and cls.fingerprint_exists(user_fp, cooldown_seconds=cooldown_seconds):
                continue
            n = cls.objects.create(
                recipient=user,
                notification_type=notification_type,
                title=title, message=message, priority=priority,
                action_url=action_url,
                action_label=action_label,
                related_object_type=related_object_type,
                related_object_id=related_object_id,
                fingerprint=user_fp,
            )
            created.append(n)
        return created
