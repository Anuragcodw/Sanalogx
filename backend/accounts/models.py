from django.contrib.auth.models import AbstractUser, UserManager as BaseUserManager
from django.db import models
import uuid
import re


class UserManager(BaseUserManager):
    def _create_user(self, username, email, password, **extra_fields):
        license_number = extra_fields.pop('license_number', None)
        qualification = extra_fields.pop('qualification', None)
        
        user = super()._create_user(username, email, password, **extra_fields)
        
        if license_number is not None:
            user.license_number = license_number
        if qualification is not None:
            user.qualification = qualification
        if license_number is not None or qualification is not None:
            user.save()
            
        return user

    def create(self, **kwargs):
        license_number = kwargs.pop('license_number', None)
        qualification = kwargs.pop('qualification', None)
        
        user = super().create(**kwargs)
        
        if license_number is not None:
            user.license_number = license_number
        if qualification is not None:
            user.qualification = qualification
        if license_number is not None or qualification is not None:
            user.save()
            
        return user


class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = 'ADMIN', 'Admin'
        PHARMACIST = 'PHARMACIST', 'Pharmacist'
        DOCTOR = 'DOCTOR', 'Doctor'

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        APPROVED = 'APPROVED', 'Approved'
        REJECTED = 'REJECTED', 'Rejected'

    email = models.EmailField(unique=True, help_text="Unique email address")
    full_name = models.CharField(max_length=255, blank=True, help_text="Full Name of the user")
    phone = models.CharField(max_length=20, blank=True, help_text="Contact Phone Number")
    
    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.PHARMACIST,
        help_text="User role (ADMIN, PHARMACIST, or DOCTOR)"
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        help_text="Pharmacist approval status"
    )

    email_verified = models.BooleanField(
        default=False,
        help_text="Whether the user's email address has been verified"
    )
    
    # Rejection tracking
    rejection_reason = models.TextField(
        blank=True,
        default='',
        help_text="Reason provided by Admin when rejecting a pharmacist registration"
    )

    # Registration ID — generated server-side on approval (e.g. MV-PH-2026-00001)
    registration_id = models.CharField(
        max_length=30,
        unique=True,
        null=True,
        blank=True,
        help_text="Unique pharmacist Registration ID generated on approval"
    )

    # Force first-login password change after system-generated credentials are assigned
    must_change_password = models.BooleanField(
        default=False,
        help_text="If True, pharmacist must change their temporary password before accessing the dashboard"
    )

    first_login_completed = models.BooleanField(
        default=False,
        help_text="Tracks whether the pharmacist has completed their initial login"
    )
    approval_date = models.DateTimeField(null=True, blank=True, help_text="Timestamp when approved by admin")
    approved_by = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_users'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    @property
    def license_number(self):
        try:
            return self.pharmacist_profile.registration_number
        except PharmacistProfile.DoesNotExist:
            return getattr(self, '_temp_license_number', "")

    @license_number.setter
    def license_number(self, value):
        self._temp_license_number = value

    @property
    def qualification(self):
        try:
            return self.pharmacist_profile.qualification
        except PharmacistProfile.DoesNotExist:
            return getattr(self, '_temp_qualification', "")

    @qualification.setter
    def qualification(self, value):
        self._temp_qualification = value

    def save(self, *args, **kwargs):
        if self.is_superuser or self.is_staff:
            self.role = self.Role.ADMIN
            self.status = self.Status.APPROVED
            self.email_verified = True
        
        super().save(*args, **kwargs)

        temp_license = getattr(self, '_temp_license_number', None)
        temp_qual = getattr(self, '_temp_qualification', None)
        temp_is_demo = getattr(self, '_temp_is_demo', None)
        temp_verif_status = getattr(self, '_temp_verification_status', None)
        if temp_license is not None or temp_qual is not None or temp_is_demo is not None:
            profile, created = PharmacistProfile.objects.get_or_create(user=self)
            if temp_license is not None:
                profile.registration_number = temp_license
            if temp_qual is not None:
                profile.qualification = temp_qual
            if temp_is_demo is not None:
                profile.is_demo = temp_is_demo
            elif temp_license and bool(re.match(r'^PH-[0-9]{4}$', str(temp_license).strip())):
                profile.is_demo = True
            if temp_verif_status is not None:
                profile.verification_status = temp_verif_status
            elif profile.is_demo:
                profile.verification_status = 'DEMO_UNVERIFIED'
            profile.save()

    def __str__(self):
        return f"{self.username} ({self.get_role_display()} - {self.get_status_display()})"

    @property
    def is_demo(self):
        """Returns True if this user's pharmacist profile is marked as a demo registration."""
        try:
            return self.pharmacist_profile.is_demo
        except PharmacistProfile.DoesNotExist:
            return getattr(self, '_temp_is_demo', False)

    @property
    def license_verification_status(self):
        """Returns verification status string (e.g. DEMO_UNVERIFIED, UNVERIFIED, VERIFIED)."""
        try:
            return self.pharmacist_profile.verification_status
        except PharmacistProfile.DoesNotExist:
            return getattr(self, '_temp_verification_status', 'UNVERIFIED')

    @property
    def is_doctor(self):
        """Returns True if user is a medical doctor or hospital administrator."""
        return self.role in [self.Role.DOCTOR, self.Role.ADMIN]

    @property
    def can_login(self):
        """Returns True only if this user is fully eligible to access the dashboard."""
        if self.role == self.Role.ADMIN:
            return True
        if self.role == self.Role.DOCTOR:
            return self.is_active
        return (
            self.role == self.Role.PHARMACIST
            and self.status == self.Status.APPROVED
            and self.email_verified
            and self.is_active
        )


class PharmacistProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='pharmacist_profile')
    registration_number = models.CharField(max_length=100, unique=True, help_text="Unique registration number")
    qualification = models.CharField(max_length=150, help_text="Pharmacist qualification")
    is_demo = models.BooleanField(
        default=False,
        help_text="Whether this is an academic demo registration (unverified)"
    )
    verification_status = models.CharField(
        max_length=50,
        default='UNVERIFIED',
        help_text="Council verification status: DEMO_UNVERIFIED, UNVERIFIED, or VERIFIED"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        demo_tag = " [DEMO]" if self.is_demo else ""
        return f"Profile for {self.user.username} ({self.registration_number}){demo_tag}"


class EmailVerification(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='email_verifications')
    token = models.UUIDField(default=uuid.uuid4, editable=False, help_text="UUID verification token")
    is_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    def __str__(self):
        return f"Verification for {self.user.email} (Verified: {self.is_verified})"


class PasswordResetRequest(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='password_reset_requests', null=True, blank=True)
    email = models.EmailField(blank=True, default='')
    hashed_code = models.CharField(max_length=255, help_text="Hashed verification code")
    expires_at = models.DateTimeField()
    is_used = models.BooleanField(default=False)
    attempt_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Password Reset Code for {self.email} (Used: {self.is_used})"


class UsernameRecoveryRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        APPROVED = 'APPROVED', 'Approved'
        REJECTED = 'REJECTED', 'Rejected'
        COMPLETED = 'COMPLETED', 'Completed'

    email = models.EmailField()
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='username_recovery_requests')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    requested_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='reviewed_username_recoveries')
    notes = models.TextField(blank=True, default='')
    recovery_sent_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Username Recovery for {self.email} ({self.status})"


class AuditLog(models.Model):
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_logs')
    action = models.CharField(max_length=100)
    description = models.TextField()
    performed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='performed_audit_logs')
    ip_address = models.CharField(max_length=45, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"[{self.action}] {self.description[:50]} at {self.created_at}"


def log_audit(user, action, description, performed_by=None, ip_address=None):
    """Utility helper to safely record an audit log without throwing exceptions."""
    try:
        AuditLog.objects.create(
            user=user,
            action=action,
            description=description,
            performed_by=performed_by,
            ip_address=ip_address
        )
    except Exception as exc:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Failed to record audit log: {exc}")

