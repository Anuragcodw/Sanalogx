import secrets
from django.db import models
from django.conf import settings
from django.utils import timezone
from django.core.exceptions import ValidationError
from medicines.models import Medicine, MedicineBatch


class PharmacyQRToken(models.Model):
    token = models.CharField(max_length=64, unique=True, default=secrets.token_urlsafe)
    name = models.CharField(max_length=150, default="Pharmacy Counter QR")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.token[:8]}...)"


class MedicineRequest(models.Model):
    class RequestStatus(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        UNDER_REVIEW = 'UNDER_REVIEW', 'Under Review'
        PROCESSING = 'PROCESSING', 'Processing'
        PARTIALLY_COMPLETED = 'PARTIALLY_COMPLETED', 'Partially Completed'
        COMPLETED = 'COMPLETED', 'Completed'
        REJECTED = 'REJECTED', 'Rejected'
        CANCELLED = 'CANCELLED', 'Cancelled'

    request_id = models.CharField(max_length=30, unique=True, help_text="e.g. REQ-2026-0001")
    patient_name = models.CharField(max_length=255)
    patient_id = models.CharField(max_length=100, help_text="Patient ID / UHID")
    mobile_number = models.CharField(max_length=20)
    email = models.EmailField(blank=True, default='')
    department = models.CharField(max_length=100, blank=True, default='')
    
    prescription_number = models.CharField(max_length=100, blank=True, default='', help_text="Rx / Prescription Number")
    doctor_name = models.CharField(max_length=255, blank=True, default='Dr. Attending Physician')
    priority = models.CharField(max_length=20, choices=[('NORMAL', 'Normal'), ('URGENT', 'Urgent')], default='NORMAL')

    request_status = models.CharField(
        max_length=30,
        choices=RequestStatus.choices,
        default=RequestStatus.PENDING
    )
    total_items = models.PositiveIntegerField(default=0)
    notes = models.TextField(blank=True, default='')
    
    qr_token = models.ForeignKey(PharmacyQRToken, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.request_id} — {self.patient_name} ({self.get_request_status_display()})"


class MedicineRequestItem(models.Model):
    class ItemStatus(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        AVAILABLE = 'AVAILABLE', 'Available'
        PROCESSING = 'PROCESSING', 'Processing'
        DISPENSED = 'DISPENSED', 'Dispensed'
        PARTIALLY_AVAILABLE = 'PARTIALLY_AVAILABLE', 'Partially Available'
        NOT_AVAILABLE = 'NOT_AVAILABLE', 'Not Available'
        REJECTED = 'REJECTED', 'Rejected'

    request = models.ForeignKey(MedicineRequest, on_delete=models.CASCADE, related_name='items')
    medicine = models.ForeignKey(Medicine, on_delete=models.CASCADE, related_name='request_items')
    requested_quantity = models.PositiveIntegerField()
    approved_quantity = models.PositiveIntegerField(default=0)
    dispensed_quantity = models.PositiveIntegerField(default=0)
    
    item_status = models.CharField(
        max_length=30,
        choices=ItemStatus.choices,
        default=ItemStatus.PENDING
    )
    dispensed_batch = models.ForeignKey(MedicineBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name='dispensed_request_items')
    notes = models.TextField(blank=True, default='')

    def __str__(self):
        return f"{self.medicine.medicine_name} (x{self.requested_quantity}) - {self.get_item_status_display()}"


class DispensingInvoice(models.Model):
    class PaymentStatus(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        PAID = 'PAID', 'Paid'
        PARTIALLY_PAID = 'PARTIALLY_PAID', 'Partially Paid'
        FAILED = 'FAILED', 'Failed'
        CANCELLED = 'CANCELLED', 'Cancelled'

    class PaymentMethod(models.TextChoices):
        CASH = 'CASH', 'Cash'
        UPI = 'UPI', 'UPI / QR'
        CARD = 'CARD', 'Credit / Debit Card'
        UNPAID = 'UNPAID', 'Unpaid / Pending'

    invoice_number = models.CharField(max_length=30, unique=True, help_text="e.g. INV-2026-0001")
    request = models.OneToOneField(MedicineRequest, on_delete=models.CASCADE, related_name='invoice')
    patient_name = models.CharField(max_length=255)
    patient_id = models.CharField(max_length=100)
    
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    gst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    
    payment_status = models.CharField(
        max_length=20,
        choices=PaymentStatus.choices,
        default=PaymentStatus.PENDING
    )
    payment_method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices,
        default=PaymentMethod.UNPAID
    )
    
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.invoice_number} — ₹{self.total_amount} ({self.get_payment_status_display()})"


class InvoiceItem(models.Model):
    invoice = models.ForeignKey(DispensingInvoice, on_delete=models.CASCADE, related_name='items')
    medicine = models.ForeignKey(Medicine, on_delete=models.SET_NULL, null=True)
    batch = models.ForeignKey(MedicineBatch, on_delete=models.SET_NULL, null=True)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    total_price = models.DecimalField(max_digits=12, decimal_places=2)

    def __str__(self):
        return f"{self.medicine} (x{self.quantity}) — ₹{self.total_price}"


class Patient(models.Model):
    """
    Hospital Patient Registry record for identity verification, traceability, and clinical dispensing.
    """
    class Gender(models.TextChoices):
        MALE = 'MALE', 'Male'
        FEMALE = 'FEMALE', 'Female'
        OTHER = 'OTHER', 'Other'

    BLOOD_GROUP_CHOICES = [
        ('A+', 'A+'), ('A-', 'A-'),
        ('B+', 'B+'), ('B-', 'B-'),
        ('AB+', 'AB+'), ('AB-', 'AB-'),
        ('O+', 'O+'), ('O-', 'O-'),
        ('', 'Unknown'),
    ]

    uhid = models.CharField(max_length=50, unique=True, db_index=True, help_text="e.g. UHID-2026-00001")
    name = models.CharField(max_length=255)
    gender = models.CharField(max_length=10, choices=Gender.choices, default=Gender.MALE)
    date_of_birth = models.DateField(null=True, blank=True)
    age = models.PositiveIntegerField(null=True, blank=True)
    mobile_number = models.CharField(max_length=20, blank=True, default='',
                                     help_text="Primary contact phone number")
    email = models.EmailField(blank=True, default='')
    address = models.TextField(blank=True, default='')
    blood_group = models.CharField(max_length=5, blank=True, default='', choices=BLOOD_GROUP_CHOICES,
                                   help_text="ABO blood group")
    allergies = models.TextField(blank=True, default='', help_text="Known drug/food allergies")
    medical_history = models.TextField(blank=True, default='', help_text="Relevant past medical history")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Patient"
        verbose_name_plural = "Patients"

    def __str__(self):
        return f"{self.name} ({self.uhid})"

    @property
    def phone(self):
        """Alias for mobile_number — used by scan service and API serializers."""
        return self.mobile_number

    @phone.setter
    def phone(self, value):
        self.mobile_number = value or ''

    @classmethod
    def generate_uhid(cls):
        year = timezone.now().year
        count = cls.objects.filter(created_at__year=year).count() + 1
        for i in range(100):
            candidate = f"UHID-{year}-{count + i:05d}"
            if not cls.objects.filter(uhid=candidate).exists() and not Prescription.objects.filter(patient_id=candidate).exists():
                return candidate
        return f"UHID-{year}-{secrets.randbelow(99999):05d}"


class Doctor(models.Model):
    """
    Hospital Medical Doctor / Prescriber profile authorized to issue clinical prescriptions.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='doctor_profile'
    )
    doctor_code = models.CharField(max_length=50, unique=True, help_text="e.g. DOC-001")
    name = models.CharField(max_length=255)
    registration_number = models.CharField(max_length=100, blank=True, default='', help_text="MCI/SMC Reg #")
    license_number = models.CharField(max_length=100, blank=True, default='')
    specialization = models.CharField(max_length=150, blank=True, default='General Medicine')
    department = models.CharField(max_length=100, blank=True, default='General Medicine')
    phone = models.CharField(max_length=20, blank=True, default='')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        verbose_name = "Doctor"
        verbose_name_plural = "Doctors"

    def __str__(self):
        return f"{self.name} ({self.specialization}) [{self.doctor_code}]"

    @classmethod
    def generate_doctor_code(cls):
        count = cls.objects.count() + 1
        for i in range(100):
            candidate = f"DOC-{count + i:03d}"
            if not cls.objects.filter(doctor_code=candidate).exists():
                return candidate
        return f"DOC-{secrets.randbelow(999):03d}"


class Prescription(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending Verification'
        UNDER_REVIEW = 'UNDER_REVIEW', 'Under Review'
        VERIFIED = 'VERIFIED', 'Verified'
        READY_FOR_DISPENSING = 'READY_FOR_DISPENSING', 'Ready for Dispensing'
        PARTIALLY_DISPENSED = 'PARTIALLY_DISPENSED', 'Partially Dispensed'
        DISPENSED = 'DISPENSED', 'Dispensed'
        REJECTED = 'REJECTED', 'Rejected'
        EXPIRED = 'EXPIRED', 'Expired'
        CANCELLED = 'CANCELLED', 'Cancelled'

    prescription_id = models.CharField(max_length=30, unique=True, help_text="e.g. RX-2026-0001")
    prescription_number = models.CharField(max_length=100, blank=True, default='', help_text="Physical Rx / Slip #")
    patient_record = models.ForeignKey(Patient, on_delete=models.SET_NULL, null=True, blank=True, related_name='prescriptions')
    patient_name = models.CharField(max_length=255)
    patient_id = models.CharField(max_length=100, help_text="UHID / Patient ID")
    doctor_record = models.ForeignKey(Doctor, on_delete=models.SET_NULL, null=True, blank=True, related_name='prescriptions')
    doctor_name = models.CharField(max_length=255, default="Dr. Attending Physician")
    doctor_registration = models.CharField(max_length=100, blank=True, default='', help_text="MCI/SMC Reg #")
    doctor_license = models.CharField(max_length=100, blank=True, default='')
    prescription_date = models.DateField(default=timezone.now)
    expiry_date = models.DateField(null=True, blank=True)
    department = models.CharField(max_length=100, blank=True, default='General Medicine')
    diagnosis = models.CharField(max_length=255, blank=True, default='')
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.PENDING)
    notes = models.TextField(blank=True, default='')
    rejection_reason = models.CharField(max_length=255, blank=True, default='')

    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='prescriptions_verified'
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='prescriptions_created'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.prescription_id} — {self.patient_name} ({self.get_status_display()})"


class PrescriptionItem(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        VERIFIED = 'VERIFIED', 'Verified'
        PARTIALLY_DISPENSED = 'PARTIALLY_DISPENSED', 'Partially Dispensed'
        DISPENSED = 'DISPENSED', 'Dispensed'
        NOT_AVAILABLE = 'NOT_AVAILABLE', 'Not Available'
        REJECTED = 'REJECTED', 'Rejected'

    prescription = models.ForeignKey(Prescription, on_delete=models.CASCADE, related_name='items')
    medicine = models.ForeignKey(Medicine, on_delete=models.CASCADE, related_name='prescription_items')
    strength = models.CharField(max_length=100, blank=True, default='')
    dosage_form = models.CharField(max_length=100, blank=True, default='')
    dosage = models.CharField(max_length=100, blank=True, default='1 tablet daily')
    frequency = models.CharField(max_length=100, blank=True, default='1-0-1')
    duration = models.CharField(max_length=100, blank=True, default='5 days')
    route = models.CharField(max_length=50, blank=True, default='Oral', help_text="e.g. Oral, IV, IM, Topical, Inhalation")
    dosage_instructions = models.TextField(blank=True, default='')
    prescribed_quantity = models.PositiveIntegerField(default=1)
    requested_quantity = models.PositiveIntegerField(default=1)  # alias for backward compatibility
    dispensed_quantity = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.PENDING)

    def save(self, *args, **kwargs):
        if not self.requested_quantity and self.prescribed_quantity:
            self.requested_quantity = self.prescribed_quantity
        elif not self.prescribed_quantity and self.requested_quantity:
            self.prescribed_quantity = self.requested_quantity
        if self.medicine and not self.strength:
            self.strength = self.medicine.strength
        if self.medicine and not self.dosage_form:
            self.dosage_form = self.medicine.dosage_form
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.medicine.medicine_name} x {self.prescribed_quantity} ({self.get_status_display()})"


class DispensingRecord(models.Model):
    """
    Master record of a finalized, secure medication dispensing transaction.
    Stores multi-batch allocations, patient reference, pharmacist authorization, and verification checklist.
    """
    class Status(models.TextChoices):
        CONFIRMED = 'CONFIRMED', 'Confirmed'
        COMPLETED = 'COMPLETED', 'Completed'
        CANCELLED = 'CANCELLED', 'Cancelled'
        REVERSED = 'REVERSED', 'Reversed / Returned'

    dispensing_id = models.CharField(max_length=35, unique=True, db_index=True, help_text="e.g. DISP-2026-00001")
    prescription = models.ForeignKey(
        Prescription,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='dispensing_records'
    )
    patient_record = models.ForeignKey(
        Patient,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='dispensing_records'
    )
    patient_name = models.CharField(max_length=255)
    patient_id = models.CharField(max_length=100, help_text="UHID / Patient Reference")
    invoice = models.ForeignKey(
        DispensingInvoice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='dispensing_records'
    )
    medicine = models.ForeignKey(
        Medicine,
        on_delete=models.PROTECT,
        related_name='dispensing_records'
    )
    total_quantity = models.PositiveIntegerField(help_text="Total units dispensed across all batches")
    pharmacist = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='dispensing_actions'
    )
    regulatory_category = models.CharField(
        max_length=30,
        choices=Medicine.RegulatoryCategory.choices,
        default=Medicine.RegulatoryCategory.NORMAL
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.COMPLETED
    )
    verification_checklist = models.JSONField(
        default=dict,
        help_text="Audit checklist of verified parameters (Rx, Medicine, Batch, Expiry, Stock, FEFO, Regulatory)"
    )
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Dispensing Record"
        verbose_name_plural = "Dispensing Records"

    def __str__(self):
        return f"{self.dispensing_id} — {self.medicine.medicine_name} ({self.total_quantity} units) by {self.pharmacist.username}"

    @classmethod
    def generate_dispensing_id(cls):
        year = timezone.now().year
        count = cls.objects.filter(created_at__year=year).count() + 1
        for i in range(100):
            d_id = f"DISP-{year}-{count + i:05d}"
            if not cls.objects.filter(dispensing_id=d_id).exists():
                return d_id
        return f"DISP-{year}-{secrets.token_hex(4).upper()}"

    def delete(self, *args, **kwargs):
        if self.status in [self.Status.CONFIRMED, self.Status.COMPLETED]:
            raise ValidationError(
                f"Cannot delete completed dispensing record {self.dispensing_id}. "
                "Use the clinical reversal workflow to restore inventory."
            )
        return super().delete(*args, **kwargs)


class DispensingAllocation(models.Model):
    """
    Child allocation preserving exact batch-level breakdown for a DispensingRecord.
    E.g. Dispensing 70 units -> Batch B: 50 units, Batch A: 20 units.
    """
    dispensing = models.ForeignKey(
        DispensingRecord,
        on_delete=models.CASCADE,
        related_name='allocations'
    )
    batch = models.ForeignKey(
        MedicineBatch,
        on_delete=models.PROTECT,
        related_name='dispensing_allocations'
    )
    quantity = models.PositiveIntegerField(help_text="Units allocated from this specific batch")
    unit_price = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    total_price = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    stock_transaction = models.ForeignKey(
        'inventory.StockTransaction',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='dispensing_allocations'
    )

    @property
    def batch_number(self):
        return self.batch.batch_number if self.batch else ''

    @property
    def quantity_allocated(self):
        return self.quantity

    @property
    def expiry_date(self):
        return self.batch.expiry_date if self.batch else None

    def __str__(self):
        return f"{self.dispensing.dispensing_id} | Batch {self.batch.batch_number} ({self.quantity} units)"


class ControlledMedicineRegister(models.Model):
    """
    Legally compliant, running audit ledger for Controlled & Scheduled medicines
    (Schedule H1, Schedule X, Narcotics). Balance is strictly derived from verified stock transactions.
    """
    class TransactionType(models.TextChoices):
        OPENING = 'OPENING', 'Opening Balance'
        PURCHASE = 'PURCHASE', 'Purchase Receipt'
        DISPENSE = 'DISPENSE', 'Prescription Dispense'
        RETURN = 'RETURN', 'Patient Return'
        DAMAGE = 'DAMAGE', 'Damaged Stock'
        EXPIRY = 'EXPIRY', 'Expiry Write-off'
        ADJUSTMENT = 'ADJUSTMENT', 'Stock Adjustment'

    entry_id = models.CharField(max_length=35, unique=True, db_index=True, help_text="e.g. CR-2026-00001")
    medicine = models.ForeignKey(
        Medicine,
        on_delete=models.PROTECT,
        related_name='controlled_register_entries'
    )
    batch = models.ForeignKey(
        MedicineBatch,
        on_delete=models.PROTECT,
        related_name='controlled_register_entries'
    )
    transaction_type = models.CharField(max_length=20, choices=TransactionType.choices)
    
    opening_balance = models.IntegerField(help_text="Batch balance before transaction")
    quantity_in = models.PositiveIntegerField(default=0, help_text="Units received or returned")
    quantity_out = models.PositiveIntegerField(default=0, help_text="Units dispensed or written off")
    closing_balance = models.IntegerField(help_text="Batch balance after transaction")

    prescription_number = models.CharField(max_length=100, blank=True, default='')
    doctor_name = models.CharField(max_length=255, blank=True, default='')
    patient_name = models.CharField(max_length=255, blank=True, default='')
    patient_id = models.CharField(max_length=100, blank=True, default='')

    dispensing_record = models.ForeignKey(
        DispensingRecord,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='controlled_entries'
    )
    pharmacist = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='controlled_register_entries'
    )
    reference_number = models.CharField(max_length=100, blank=True, default='')
    reason = models.TextField(blank=True, default='')
    recorded_at = models.DateTimeField(auto_now_add=True, db_index=True)

    @property
    def register_id(self):
        return self.entry_id

    @property
    def quantity_dispensed(self):
        return self.quantity_out

    @property
    def prescribing_doctor(self):
        return self.doctor_name

    @property
    def doctor_registration(self):
        if self.dispensing_record and self.dispensing_record.prescription:
            return self.dispensing_record.prescription.doctor_registration
        return ''

    @property
    def transaction_date(self):
        return self.recorded_at

    @property
    def regulatory_category(self):
        return self.medicine.regulatory_category if self.medicine else ''

    def get_regulatory_category_display(self):
        return self.medicine.get_regulatory_category_display() if self.medicine else ''

    class Meta:
        ordering = ['-recorded_at']
        verbose_name = "Controlled Medicine Register Entry"
        verbose_name_plural = "Controlled Medicine Register"

    def __str__(self):
        return f"{self.entry_id} | {self.medicine.medicine_name} | {self.transaction_type} (Balance: {self.closing_balance})"

    @classmethod
    def generate_entry_id(cls):
        year = timezone.now().year
        count = cls.objects.filter(recorded_at__year=year).count() + 1
        for i in range(100):
            e_id = f"CR-{year}-{count + i:05d}"
            if not cls.objects.filter(entry_id=e_id).exists():
                return e_id
        return f"CR-{year}-{secrets.token_hex(4).upper()}"


class StockDiscrepancy(models.Model):
    """
    Tracks detected inventory mismatches, unexpected deductions, controlled register variances,
    or expired batch dispensing attempts.
    """
    class DiscrepancyType(models.TextChoices):
        PHYSICAL_VS_SYSTEM = 'PHYSICAL_VS_SYSTEM', 'Physical vs System Mismatch'
        CONTROLLED_MISMATCH = 'CONTROLLED_MISMATCH', 'Controlled Register Mismatch'
        UNEXPECTED_REDUCTION = 'UNEXPECTED_REDUCTION', 'Unexpected Stock Reduction'
        EXPIRED_ATTEMPT = 'EXPIRED_ATTEMPT', 'Expired Batch Dispensing Attempt'
        UNAUTHORIZED_ADJUSTMENT = 'UNAUTHORIZED_ADJUSTMENT', 'Unauthorized Adjustment'
        QUANTITY_MISMATCH = 'QUANTITY_MISMATCH', 'Prescription Quantity Mismatch'
        NO_PRESCRIPTION = 'NO_PRESCRIPTION', 'Dispensing Without Required Rx'

    class Severity(models.TextChoices):
        INFO = 'INFO', 'Info'
        WARNING = 'WARNING', 'Warning'
        HIGH = 'HIGH', 'High'
        CRITICAL = 'CRITICAL', 'Critical'

    class Status(models.TextChoices):
        OPEN = 'OPEN', 'Open'
        UNDER_REVIEW = 'UNDER_REVIEW', 'Under Review'
        RESOLVED = 'RESOLVED', 'Resolved'
        ESCALATED = 'ESCALATED', 'Escalated'
        CLOSED = 'CLOSED', 'Closed'

    @property
    def variance(self):
        return self.difference

    @variance.setter
    def variance(self, val):
        self.difference = val

    discrepancy_id = models.CharField(max_length=35, unique=True, db_index=True, help_text="e.g. DISC-2026-00001")
    medicine = models.ForeignKey(
        Medicine,
        on_delete=models.CASCADE,
        related_name='discrepancies'
    )
    batch = models.ForeignKey(
        MedicineBatch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='discrepancies'
    )
    discrepancy_type = models.CharField(max_length=30, choices=DiscrepancyType.choices)
    severity = models.CharField(max_length=15, choices=Severity.choices, default=Severity.WARNING)
    expected_quantity = models.IntegerField(default=0)
    actual_quantity = models.IntegerField(default=0)
    difference = models.IntegerField(default=0, help_text="actual - expected")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    
    reason = models.TextField(blank=True, default='')
    resolution_notes = models.TextField(blank=True, default='')
    
    detected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='discrepancies_detected'
    )
    detected_at = models.DateTimeField(auto_now_add=True, db_index=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='discrepancies_resolved'
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    @property
    def created_at(self):
        return self.detected_at

    class Meta:
        ordering = ['-detected_at']
        verbose_name = "Stock Discrepancy"
        verbose_name_plural = "Stock Discrepancies"

    def __str__(self):
        return f"[{self.severity}] {self.discrepancy_id} — {self.medicine.medicine_name} ({self.get_discrepancy_type_display()})"

    @classmethod
    def generate_discrepancy_id(cls):
        year = timezone.now().year
        count = cls.objects.filter(detected_at__year=year).count() + 1
        for i in range(100):
            d_id = f"DISC-{year}-{count + i:05d}"
            if not cls.objects.filter(discrepancy_id=d_id).exists():
                return d_id
        return f"DISC-{year}-{secrets.token_hex(4).upper()}"


StockDiscrepancy.Status.INVESTIGATING = StockDiscrepancy.Status.UNDER_REVIEW


class StockReconciliation(models.Model):
    """
    Physical Inventory count vs System Stock reconciliation session.
    """
    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'
        COMPLETED = 'COMPLETED', 'Completed'
        APPROVED = 'APPROVED', 'Approved by Admin'

    class VarianceType(models.TextChoices):
        MATCH = 'MATCH', 'Match'
        SURPLUS = 'SURPLUS', 'Surplus'
        DEFICIT = 'DEFICIT', 'Deficit'

    reconciliation_id = models.CharField(max_length=35, unique=True, db_index=True, help_text="e.g. REC-2026-00001")
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='stock_reconciliations'
    )
    reconciliation_date = models.DateField(default=timezone.now)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.COMPLETED)
    total_items_checked = models.PositiveIntegerField(default=0)
    discrepancies_found = models.PositiveIntegerField(default=0)
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    medicine = models.ForeignKey(
        Medicine,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='stock_reconciliations'
    )
    batch = models.ForeignKey(
        MedicineBatch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='stock_reconciliations'
    )
    system_quantity = models.IntegerField(default=0, help_text="System quantity recorded at count time")
    physical_quantity = models.IntegerField(default=0, help_text="Physical count entered by pharmacist")
    variance = models.IntegerField(default=0, help_text="Difference (physical - system)")
    variance_type = models.CharField(max_length=15, choices=VarianceType.choices, default=VarianceType.MATCH)
    stock_transaction = models.ForeignKey(
        'inventory.StockTransaction',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='stock_reconciliations'
    )

    class Meta:
        ordering = ['-created_at']

    @property
    def session_id(self):
        return self.reconciliation_id

    @session_id.setter
    def session_id(self, val):
        self.reconciliation_id = val

    @property
    def pharmacist(self):
        return self.performed_by

    @pharmacist.setter
    def pharmacist(self, val):
        self.performed_by = val

    def __str__(self):
        return f"{self.reconciliation_id} ({self.reconciliation_date}) — {self.performed_by.username}"

    @classmethod
    def generate_reconciliation_id(cls):
        year = timezone.now().year
        count = cls.objects.filter(created_at__year=year).count() + 1
        for i in range(100):
            r_id = f"REC-{year}-{count + i:05d}"
            if not cls.objects.filter(reconciliation_id=r_id).exists():
                return r_id
        return f"REC-{year}-{secrets.token_hex(4).upper()}"


