import io
import qrcode
import base64
from datetime import date
from decimal import Decimal
from django.shortcuts import get_object_or_404
from django.db import transaction, models
from django.core.exceptions import ValidationError
from django.utils import timezone

from medicines.models import Medicine, MedicineBatch
from inventory.models import Inventory, StockTransaction, StockAlert
from inventory import services as inventory_services
from accounts.models import log_audit
from .models import (
    PharmacyQRToken, MedicineRequest, MedicineRequestItem,
    DispensingInvoice, InvoiceItem
)


def get_or_create_pharmacy_qr():
    """Get active pharmacy counter QR token or create a default one."""
    token_obj = PharmacyQRToken.objects.filter(is_active=True).first()
    if not token_obj:
        token_obj = PharmacyQRToken.objects.create(
            name="Pharmacy Counter Main QR",
            is_active=True
        )
    return token_obj


def generate_qr_code_image_base64(target_url):
    """Generate a Base64-encoded PNG data URL for a given URL."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(target_url)
    qr.make(fit=True)

    img = qr.make_image(fill_color="#0F2744", back_color="#FFFFFF")
    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode('utf-8')
    return f"data:image/png;base64,{img_str}"


def generate_unique_request_id():
    """Generate unique Request ID in format REQ-YYYY-NNNN."""
    year = timezone.now().year
    count = MedicineRequest.objects.count() + 1
    for i in range(100):
        req_id = f"REQ-{year}-{count + i:04d}"
        if not MedicineRequest.objects.filter(request_id=req_id).exists():
            return req_id
    return f"REQ-{year}-{timezone.now().strftime('%m%d%H%M%S')}"


def generate_unique_invoice_number():
    """Generate unique Invoice Number in format INV-YYYY-NNNN."""
    year = timezone.now().year
    count = DispensingInvoice.objects.count() + 1
    for i in range(100):
        inv_num = f"INV-{year}-{count + i:04d}"
        if not DispensingInvoice.objects.filter(invoice_number=inv_num).exists():
            return inv_num
    return f"INV-{year}-{timezone.now().strftime('%m%d%H%M%S')}"


@transaction.atomic
def create_medicine_request(patient_name, patient_id, mobile_number, email='', department='', cart_items=None, qr_token_obj=None):
    """
    Creates a single MedicineRequest with multiple MedicineRequestItems from patient cart.
    cart_items = [{'medicine_id': 1, 'quantity': 10}, ...]
    """
    if not patient_name or not patient_id or not mobile_number:
        raise ValidationError("Patient name, UHID/Patient ID, and mobile number are required.")

    if not cart_items or len(cart_items) == 0:
        raise ValidationError("Your request cart is empty. Please select at least one medicine.")

    req_id = generate_unique_request_id()

    request_obj = MedicineRequest.objects.create(
        request_id=req_id,
        patient_name=patient_name.strip(),
        patient_id=patient_id.strip(),
        mobile_number=mobile_number.strip(),
        email=email.strip().lower() if email else '',
        department=department.strip() if department else '',
        request_status=MedicineRequest.RequestStatus.PENDING,
        total_items=len(cart_items),
        qr_token=qr_token_obj
    )

    created_items = []
    seen_medicines = set()

    for item_data in cart_items:
        med_id = item_data.get('medicine_id')
        qty = int(item_data.get('quantity', 0))

        if qty <= 0:
            continue

        if med_id in seen_medicines:
            continue
        seen_medicines.add(med_id)

        try:
            medicine = Medicine.objects.get(pk=med_id, status='ACTIVE')
        except Medicine.DoesNotExist:
            raise ValidationError(f"Medicine with ID {med_id} is not available.")

        item_obj = MedicineRequestItem.objects.create(
            request=request_obj,
            medicine=medicine,
            requested_quantity=qty,
            item_status=MedicineRequestItem.ItemStatus.PENDING
        )
        created_items.append(item_obj)

    if not created_items:
        request_obj.delete()
        raise ValidationError("No valid medicine items were added to the request.")

    return request_obj


def validate_barcode_for_item(request_item_id, scanned_barcode):
    """
    Validates scanned barcode against requested medicine and available batches.
    Checks:
      1. Medicine match
      2. Batch existence & non-expired status
      3. Stock availability
      4. FEFO recommendation comparison
    """
    scanned_barcode = str(scanned_barcode).strip()
    try:
        item = MedicineRequestItem.objects.select_related('medicine', 'request').get(pk=request_item_id)
    except MedicineRequestItem.DoesNotExist:
        return {'valid': False, 'error': "Request item not found."}

    medicine = item.medicine
    today = timezone.now().date()

    # Find matching batch by barcode or batch_number
    batches = MedicineBatch.objects.filter(
        medicine=medicine
    ).filter(
        models.Q(batch_number__iexact=scanned_barcode) | models.Q(medicine__barcode__iexact=scanned_barcode)
    )

    if not batches.exists():
        # Check if barcode belongs to another medicine entirely
        other_med = Medicine.objects.filter(barcode__iexact=scanned_barcode).first()
        if other_med and other_med.id != medicine.id:
            return {
                'valid': False,
                'error': f"Scanned barcode belongs to '{other_med.medicine_name}', which does not match requested medicine '{medicine.medicine_name}'."
            }
        return {
            'valid': False,
            'error': f"No valid batch found with barcode/batch number '{scanned_barcode}' for {medicine.medicine_name}."
        }

    batch = batches.first()

    # Check batch status & expiry
    if batch.expiry_date <= today:
        return {
            'valid': False,
            'error': f"Batch '{batch.batch_number}' has EXPIRED on {batch.expiry_date.strftime('%d %b %Y')}. Cannot dispense expired stock."
        }

    if batch.status in ['QUARANTINED', 'RETURNED', 'EXPIRED']:
        return {
            'valid': False,
            'error': f"Batch '{batch.batch_number}' is marked as {batch.get_status_display()} and cannot be dispensed."
        }

    if batch.quantity_available <= 0:
        return {
            'valid': False,
            'error': f"Batch '{batch.batch_number}' has zero available stock."
        }

    # FEFO Check
    fefo_allocations = inventory_services.allocate_stock(medicine, item.requested_quantity, policy='FEFO')
    recommended_batch = fefo_allocations[0]['batch'] if fefo_allocations else None

    is_fefo_recommended = (recommended_batch and recommended_batch.id == batch.id)
    fefo_warning = None
    if not is_fefo_recommended and recommended_batch:
        fefo_warning = (
            f"FEFO Recommendation Notice: Batch '{recommended_batch.batch_number}' "
            f"expires earlier ({recommended_batch.expiry_date.strftime('%d %b %Y')}) and should preferably be dispensed first."
        )

    return {
        'valid': True,
        'item_id': item.id,
        'medicine_name': medicine.medicine_name,
        'batch_id': batch.id,
        'batch_number': batch.batch_number,
        'barcode': medicine.barcode or batch.batch_number or '',
        'available_quantity': batch.quantity_available,
        'expiry_date': batch.expiry_date.strftime('%Y-%m-%d'),
        'selling_price': str(batch.selling_price),
        'requested_quantity': item.requested_quantity,
        'is_fefo_recommended': is_fefo_recommended,
        'recommended_batch_number': recommended_batch.batch_number if recommended_batch else '',
        'fefo_warning': fefo_warning
    }


@transaction.atomic
def dispense_request_item(request_item_id, batch_id, dispense_quantity, pharmacist_user):
    """
    Executes secure dispensing of a request item from a specific batch inside atomic transaction.
    Reduces batch quantity, updates total inventory, writes StockTransaction, updates item & request status.
    """
    try:
        item = MedicineRequestItem.objects.select_for_update().select_related('request', 'medicine').get(pk=request_item_id)
        batch = MedicineBatch.objects.select_for_update().select_related('medicine').get(pk=batch_id, medicine=item.medicine)
    except (MedicineRequestItem.DoesNotExist, MedicineBatch.DoesNotExist):
        raise ValidationError("Request item or medicine batch not found.")

    dispense_quantity = int(dispense_quantity)
    if dispense_quantity <= 0:
        raise ValidationError("Quantity to dispense must be greater than zero.")

    today = timezone.now().date()
    if batch.expiry_date <= today:
        raise ValidationError(f"Batch {batch.batch_number} has expired and cannot be dispensed.")

    if batch.quantity_available < dispense_quantity:
        raise ValidationError(
            f"Insufficient stock in batch {batch.batch_number}. "
            f"Requested: {dispense_quantity}, Available: {batch.quantity_available}."
        )

    if item.item_status == MedicineRequestItem.ItemStatus.DISPENSED:
        raise ValidationError("This item has already been fully dispensed.")

    # 1. Deduct stock safely via central StockService
    ref_num = f"DISP-{item.request.request_id}"
    txn = inventory_services.remove_stock(
        medicine=item.medicine,
        batch=batch,
        quantity=dispense_quantity,
        transaction_type=StockTransaction.TransactionType.DISPENSE,
        performed_by=pharmacist_user,
        reference_number=ref_num,
        notes=f"Dispensed for patient request {item.request.request_id} ({item.request.patient_name})",
    )
    batch.refresh_from_db()

    # 4. Update request item status
    item.dispensed_quantity += dispense_quantity
    item.dispensed_batch = batch
    if item.dispensed_quantity >= item.requested_quantity:
        item.item_status = MedicineRequestItem.ItemStatus.DISPENSED
    else:
        item.item_status = MedicineRequestItem.ItemStatus.PARTIALLY_AVAILABLE
    item.save()

    # 5. Recalculate parent request status
    parent_request = item.request
    all_items = parent_request.items.all()
    dispensed_count = all_items.filter(item_status=MedicineRequestItem.ItemStatus.DISPENSED).count()
    partial_count = all_items.filter(item_status=MedicineRequestItem.ItemStatus.PARTIALLY_AVAILABLE).count()
    total_count = all_items.count()

    if dispensed_count == total_count:
        parent_request.request_status = MedicineRequest.RequestStatus.COMPLETED
    elif dispensed_count > 0 or partial_count > 0:
        parent_request.request_status = MedicineRequest.RequestStatus.PARTIALLY_COMPLETED
    else:
        parent_request.request_status = MedicineRequest.RequestStatus.PROCESSING
    parent_request.save(update_fields=['request_status'])

    # 6. Audit log
    log_audit(
        user=pharmacist_user,
        action='DISPENSE_MEDICINE',
        description=f"Dispensed {dispense_quantity} units of {item.medicine.medicine_name} (Batch {batch.batch_number}) for {parent_request.request_id}.",
        performed_by=pharmacist_user
    )

    return item


@transaction.atomic
def generate_invoice_for_request(request_id, pharmacist_user=None):
    """
    Generates or retrieves a DispensingInvoice for a MedicineRequest.
    Only includes successfully dispensed items (dispensed_quantity > 0).
    Calculates subtotal, 5% GST, total.
    """
    request_obj = get_object_or_404(MedicineRequest, request_id=request_id) if isinstance(request_id, str) else request_id

    # Check if invoice already exists
    if hasattr(request_obj, 'invoice'):
        return request_obj.invoice

    dispensed_items = request_obj.items.filter(dispensed_quantity__gt=0)
    if not dispensed_items.exists():
        raise ValidationError("No medicines have been dispensed for this request yet.")

    inv_num = generate_unique_invoice_number()

    subtotal = Decimal('0.00')
    line_items_data = []

    for item in dispensed_items:
        batch = item.dispensed_batch
        unit_price = batch.selling_price if batch else Decimal('0.00')
        line_total = Decimal(str(item.dispensed_quantity)) * unit_price
        subtotal += line_total
        line_items_data.append({
            'medicine': item.medicine,
            'batch': batch,
            'quantity': item.dispensed_quantity,
            'unit_price': unit_price,
            'total_price': line_total
        })

    gst_amount = (subtotal * Decimal('0.05')).quantize(Decimal('0.01'))
    discount = Decimal('0.00')
    total_amount = subtotal + gst_amount - discount

    invoice = DispensingInvoice.objects.create(
        invoice_number=inv_num,
        request=request_obj,
        patient_name=request_obj.patient_name,
        patient_id=request_obj.patient_id,
        subtotal=subtotal,
        gst_amount=gst_amount,
        discount=discount,
        total_amount=total_amount,
        payment_status=DispensingInvoice.PaymentStatus.PENDING,
        payment_method=DispensingInvoice.PaymentMethod.UNPAID,
        created_by=pharmacist_user if (pharmacist_user and pharmacist_user.is_authenticated) else None
    )

    for l_data in line_items_data:
        InvoiceItem.objects.create(
            invoice=invoice,
            medicine=l_data['medicine'],
            batch=l_data['batch'],
            quantity=l_data['quantity'],
            unit_price=l_data['unit_price'],
            total_price=l_data['total_price']
        )

    return invoice


@transaction.atomic
def confirm_payment(invoice_id, payment_method, pharmacist_user=None):
    """
    Confirms payment for a DispensingInvoice, updating payment_status = PAID.
    """
    invoice = DispensingInvoice.objects.select_for_update().get(pk=invoice_id)
    if invoice.payment_status == DispensingInvoice.PaymentStatus.PAID:
        return invoice

    invoice.payment_status = DispensingInvoice.PaymentStatus.PAID
    invoice.payment_method = payment_method
    invoice.save()

    log_audit(
        user=pharmacist_user if (pharmacist_user and pharmacist_user.is_authenticated) else None,
        action='PAYMENT_CONFIRMED',
        description=f"Payment of ₹{invoice.total_amount} confirmed via {payment_method} for Invoice {invoice.invoice_number}.",
        performed_by=pharmacist_user if (pharmacist_user and pharmacist_user.is_authenticated) else None
    )

    return invoice


@transaction.atomic
def create_direct_dispense(patient_name, patient_id, mobile_number, items_to_dispense, pharmacist_user, prescription_number=''):
    """
    Flow B: Direct Pharmacist Dispensing / Prescription processing.
    items_to_dispense = [{'medicine_id': 1, 'batch_id': 5, 'quantity': 10}, ...]
    Directly creates MedicineRequest, MedicineRequestItem, performs atomic dispensing,
    and returns generated DispensingInvoice.
    """
    if not patient_name or not patient_id:
        patient_name = "Walk-in Patient / OTC"
        patient_id = f"OTC-{timezone.now().strftime('%d%H%M%S')}"

    if not items_to_dispense or len(items_to_dispense) == 0:
        raise ValidationError("Please select at least one medicine and batch to dispense.")

    req_id = generate_unique_request_id()

    request_obj = MedicineRequest.objects.create(
        request_id=req_id,
        patient_name=patient_name.strip(),
        patient_id=patient_id.strip(),
        mobile_number=mobile_number.strip() if mobile_number else 'N/A',
        prescription_number=prescription_number.strip() if prescription_number else '',
        request_status=MedicineRequest.RequestStatus.PROCESSING,
        total_items=len(items_to_dispense)
    )

    for item_data in items_to_dispense:
        med_id = item_data.get('medicine_id')
        batch_id = item_data.get('batch_id')
        qty = int(item_data.get('quantity', 0))

        if qty <= 0:
            continue

        medicine = Medicine.objects.get(pk=med_id)

        item_obj = MedicineRequestItem.objects.create(
            request=request_obj,
            medicine=medicine,
            requested_quantity=qty,
            item_status=MedicineRequestItem.ItemStatus.PROCESSING
        )

        dispense_request_item(
            request_item_id=item_obj.id,
            batch_id=batch_id,
            dispense_quantity=qty,
            pharmacist_user=pharmacist_user
        )

    invoice = generate_invoice_for_request(request_obj.request_id, pharmacist_user=pharmacist_user)
    return invoice


@transaction.atomic
def multi_batch_fefo_dispense(medicine, requested_qty, pharmacist_user, reference_number=''):
    """
    Multi-batch FEFO allocation engine.
    If requested_qty = 70, and Batch A has 50, Batch B has 100:
    Allocates 50 from Batch A and 20 from Batch B.
    Executes atomic deductions and returns list of allocated batch details.
    """
    today = timezone.now().date()
    valid_batches = MedicineBatch.objects.select_for_update().filter(
        medicine=medicine,
        expiry_date__gt=today,
        quantity_available__gt=0
    ).exclude(status__in=['EXPIRED', 'QUARANTINED', 'RETURNED']).order_by('expiry_date')

    total_avail = sum(b.quantity_available for b in valid_batches)
    if total_avail < requested_qty:
        raise ValidationError(f"Insufficient total active stock for {medicine.medicine_name}. Requested: {requested_qty}, Available: {total_avail}.")

    remaining_needed = requested_qty
    allocations = []

    for batch in valid_batches:
        if remaining_needed <= 0:
            break

        take_qty = min(remaining_needed, batch.quantity_available)

        # Deduct stock safely via central StockService
        ref_num = reference_number or f"FEFO-DISP-{medicine.id}"
        inventory_services.remove_stock(
            medicine=medicine,
            batch=batch,
            quantity=take_qty,
            transaction_type=StockTransaction.TransactionType.DISPENSE,
            performed_by=pharmacist_user,
            reference_number=ref_num,
            notes=f"FEFO allocation of {take_qty} units from Batch {batch.batch_number}.",
        )
        batch.refresh_from_db()

        remaining_needed -= take_qty
        allocations.append({
            'batch': batch,
            'allocated_quantity': take_qty
        })

    return allocations


@transaction.atomic
def record_medicine_return(medicine_id, batch_id, return_qty, reason, pharmacist_user):
    """
    Process medicine return safely:
    Routes through central inventory service record_return.
    """
    if return_qty <= 0:
        raise ValidationError("Return quantity must be greater than zero.")

    medicine = Medicine.objects.get(pk=medicine_id)
    batch = MedicineBatch.objects.get(pk=batch_id, medicine=medicine)

    inventory_services.record_return(
        medicine=medicine,
        batch=batch,
        quantity=return_qty,
        performed_by=pharmacist_user,
        reason=reason,
        reference_number=f"RET-{batch.batch_number}",
        notes=f"Medicine return: {reason}"
    )

    batch.refresh_from_db()
    if batch.status == 'EXHAUSTED' and batch.quantity_available > 0:
        batch.status = 'ACTIVE'
        batch.save(update_fields=['status'])

    return batch


