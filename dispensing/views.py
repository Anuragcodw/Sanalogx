import json
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse, HttpResponse
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone
from datetime import timedelta

from django.core.paginator import Paginator
from medicines.models import Medicine, Category, MedicineBatch
from inventory.models import Inventory, StockTransaction, StockAlert
from inventory.views import admin_or_pharmacist_required, admin_required, pharmacist_required
from accounts.models import log_audit

from .models import (
    Patient, Doctor,
    PharmacyQRToken, MedicineRequest, MedicineRequestItem,
    DispensingInvoice, InvoiceItem, Prescription, PrescriptionItem,
    DispensingRecord, DispensingAllocation, ControlledMedicineRegister,
    StockDiscrepancy, StockReconciliation
)
from . import services
from . import secure_dispensing_service


# ---------------------------------------------------------------------------
# Pharmacist Module: Medicine Request QR Display
# ---------------------------------------------------------------------------

@admin_or_pharmacist_required
def qr_display_view(request):
    """
    Displays Pharmacy Medicine Request QR Code in Pharmacist Dashboard.
    Supports Regenerate QR token, Print, Download.
    """
    qr_obj = services.get_or_create_pharmacy_qr()

    if request.method == 'POST' and 'regenerate_qr' in request.POST:
        qr_obj.is_active = False
        qr_obj.save()
        qr_obj = PharmacyQRToken.objects.create(
            name="Pharmacy Counter Main QR",
            is_active=True
        )
        messages.success(request, "A new secure QR token has been generated successfully.")

    request_url = request.build_absolute_uri(f"/medicine-request/{qr_obj.token}/")
    qr_image_base64 = services.generate_qr_code_image_base64(request_url)

    context = {
        'qr_obj': qr_obj,
        'request_url': request_url,
        'qr_image_base64': qr_image_base64,
    }
    return render(request, 'dispensing/qr_display.html', context)


# ---------------------------------------------------------------------------
# Public Patient Mobile Medicine Request Form
# ---------------------------------------------------------------------------

def mobile_medicine_request_view(request, token):
    """
    Mobile-friendly patient medicine request form.
    Opens when patient scans pharmacy QR code.
    Allows multi-medicine selection with category filtering & cart.
    """
    try:
        qr_token_obj = PharmacyQRToken.objects.get(token=token, is_active=True)
    except PharmacyQRToken.DoesNotExist:
        return render(request, 'dispensing/invalid_qr.html', {
            'message': 'This Medicine Request QR link is invalid or expired. Please scan an active counter QR code.'
        })

    categories = Category.objects.filter(is_active=True).order_by('name')
    medicines = Medicine.objects.filter(status='ACTIVE').select_related('category').order_by('medicine_name')

    if request.method == 'POST':
        patient_name = request.POST.get('patient_name', '').strip()
        patient_id = request.POST.get('patient_id', '').strip()
        mobile_number = request.POST.get('mobile_number', '').strip()
        email = request.POST.get('email', '').strip()
        department = request.POST.get('department', '').strip()
        cart_json = request.POST.get('cart_json', '[]')

        try:
            cart_items = json.loads(cart_json)
        except Exception:
            cart_items = []

        try:
            req_obj = services.create_medicine_request(
                patient_name=patient_name,
                patient_id=patient_id,
                mobile_number=mobile_number,
                email=email,
                department=department,
                cart_items=cart_items,
                qr_token_obj=qr_token_obj
            )
            return redirect('patient_request_success', request_id=req_obj.request_id)
        except ValidationError as err:
            messages.error(request, str(err.message if hasattr(err, 'message') else err))

    context = {
        'qr_token_obj': qr_token_obj,
        'categories': categories,
        'medicines': medicines,
    }
    return render(request, 'dispensing/mobile_request.html', context)


def ajax_get_medicines_by_category(request):
    """AJAX endpoint: returns active medicines filtered by category_id."""
    category_id = request.GET.get('category_id')
    qs = Medicine.objects.filter(status='ACTIVE')
    if category_id and category_id.isdigit():
        qs = qs.filter(category_id=int(category_id))

    data = []
    for med in qs.order_by('medicine_name'):
        data.append({
            'id': med.id,
            'name': med.medicine_name,
            'generic_name': med.generic_name or '',
            'dosage_form': med.dosage_form or '',
            'strength': med.strength or '',
            'unit_of_measure': med.unit_of_measure or '',
        })
    return JsonResponse({'medicines': data})


def patient_request_success_view(request, request_id):
    """Public confirmation page shown to patient after submitting request."""
    req_obj = get_object_or_404(MedicineRequest, request_id=request_id)
    return render(request, 'dispensing/request_success.html', {'request_obj': req_obj})


# ---------------------------------------------------------------------------
# Pharmacist Live Queue & Request Management
# ---------------------------------------------------------------------------

@admin_or_pharmacist_required
def live_requests_queue_view(request):
    """
    Pharmacist Live Request Queue.
    Displays summary counts, filterable request table, live status.
    """
    today = timezone.now().date()
    requests_qs = MedicineRequest.objects.prefetch_related('items__medicine').order_by('-created_at')

    # Summary metrics
    today_requests = requests_qs.filter(created_at__date=today).count()
    pending_count = requests_qs.filter(request_status=MedicineRequest.RequestStatus.PENDING).count()
    under_review_count = requests_qs.filter(request_status=MedicineRequest.RequestStatus.UNDER_REVIEW).count()
    processing_count = requests_qs.filter(request_status=MedicineRequest.RequestStatus.PROCESSING).count()
    completed_count = requests_qs.filter(request_status=MedicineRequest.RequestStatus.COMPLETED).count()
    rejected_count = requests_qs.filter(request_status=MedicineRequest.RequestStatus.REJECTED).count()

    # Filter
    status_filter = request.GET.get('status', '')
    search_q = request.GET.get('search', '').strip()

    if status_filter:
        requests_qs = requests_qs.filter(request_status=status_filter)
    if search_q:
        requests_qs = requests_qs.filter(
            Q(request_id__icontains=search_q) |
            Q(patient_name__icontains=search_q) |
            Q(patient_id__icontains=search_q) |
            Q(mobile_number__icontains=search_q)
        )

    context = {
        'requests_list': requests_qs[:50],
        'today_requests': today_requests,
        'pending_count': pending_count,
        'under_review_count': under_review_count,
        'processing_count': processing_count,
        'completed_count': completed_count,
        'rejected_count': rejected_count,
        'status_filter': status_filter,
        'search_q': search_q,
    }
    return render(request, 'dispensing/live_queue.html', context)


@admin_or_pharmacist_required
def request_detail_view(request, request_id):
    """
    Pharmacist Request Detail & Processing Page.
    Lists all requested medicines with stock info, barcode scanner & dispensing action.
    """
    req_obj = get_object_or_404(MedicineRequest.objects.prefetch_related('items__medicine'), request_id=request_id)

    # Auto update status to UNDER_REVIEW when opened first time
    if req_obj.request_status == MedicineRequest.RequestStatus.PENDING:
        req_obj.request_status = MedicineRequest.RequestStatus.UNDER_REVIEW
        req_obj.save(update_fields=['request_status'])

    # Build stock summary & active batches for each requested item
    items_data = []
    today = timezone.now().date()

    for item in req_obj.items.all():
        medicine = item.medicine
        inventory, _ = Inventory.objects.get_or_create(medicine=medicine)
        
        # Active batches
        active_batches = MedicineBatch.objects.filter(
            medicine=medicine,
            expiry_date__gt=today,
            quantity_available__gt=0
        ).exclude(status__in=['EXPIRED', 'QUARANTINED', 'RETURNED']).order_by('expiry_date')

        # FEFO recommended batch
        fefo_batch = active_batches.first()

        items_data.append({
            'item': item,
            'inventory': inventory,
            'active_batches': active_batches,
            'fefo_batch': fefo_batch,
        })

    # Check if invoice exists
    invoice = getattr(req_obj, 'invoice', None)

    context = {
        'req_obj': req_obj,
        'items_data': items_data,
        'invoice': invoice,
    }
    return render(request, 'dispensing/request_detail.html', context)


# ---------------------------------------------------------------------------
# AJAX Dispensing & Barcode APIs
# ---------------------------------------------------------------------------

@admin_or_pharmacist_required
def barcode_scan_api(request):
    """AJAX POST: Scans & validates barcode for a request item."""
    if request.method != 'POST':
        return JsonResponse({'valid': False, 'error': 'Method not allowed.'}, status=405)

    try:
        data = json.loads(request.body)
        item_id = data.get('item_id')
        barcode = data.get('barcode', '').strip()
        result = services.validate_barcode_for_item(item_id, barcode)
        return JsonResponse(result)
    except Exception as exc:
        return JsonResponse({'valid': False, 'error': str(exc)}, status=400)


@pharmacist_required
def dispense_item_api(request):
    """AJAX POST: Executes dispensing for a request item."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed.'}, status=405)

    try:
        data = json.loads(request.body)
        item_id = data.get('item_id')
        batch_id = data.get('batch_id')
        qty = data.get('quantity')

        item = services.dispense_request_item(
            request_item_id=item_id,
            batch_id=batch_id,
            dispense_quantity=qty,
            pharmacist_user=request.user
        )

        return JsonResponse({
            'success': True,
            'message': f"Successfully dispensed {item.dispensed_quantity} units of {item.medicine.medicine_name}.",
            'item_status': item.get_item_status_display(),
            'request_status': item.request.get_request_status_display()
        })
    except ValidationError as err:
        return JsonResponse({'success': False, 'error': str(err.message if hasattr(err, 'message') else err)}, status=400)
    except Exception as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=500)


@admin_or_pharmacist_required
def reject_request_item_api(request):
    """AJAX POST: Mark a request item as NOT_AVAILABLE / REJECTED."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed.'}, status=405)

    try:
        data = json.loads(request.body)
        item_id = data.get('item_id')
        reason = data.get('reason', 'Medicine not available in stock.')

        item = get_object_or_404(MedicineRequestItem, pk=item_id)
        item.item_status = MedicineRequestItem.ItemStatus.NOT_AVAILABLE
        item.notes = reason
        item.save()

        # Recalculate parent request status
        parent = item.request
        all_items = parent.items.all()
        dispensed_count = all_items.filter(item_status=MedicineRequestItem.ItemStatus.DISPENSED).count()
        not_avail_count = all_items.filter(item_status=MedicineRequestItem.ItemStatus.NOT_AVAILABLE).count()
        
        if not_avail_count == all_items.count():
            parent.request_status = MedicineRequest.RequestStatus.REJECTED
        elif dispensed_count > 0:
            parent.request_status = MedicineRequest.RequestStatus.PARTIALLY_COMPLETED
        parent.save()

        return JsonResponse({'success': True, 'item_status': 'Not Available'})
    except Exception as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)


# ---------------------------------------------------------------------------
# Invoice & Payment Management
# ---------------------------------------------------------------------------

@admin_or_pharmacist_required
def view_invoice_view(request, request_id):
    """
    Combined Dispensing Invoice View & Printable Page.
    Only includes successfully dispensed medicines.
    """
    req_obj = get_object_or_404(MedicineRequest, request_id=request_id)
    
    try:
        invoice = services.generate_invoice_for_request(req_obj, pharmacist_user=request.user)
    except ValidationError as err:
        messages.error(request, str(err))
        return redirect('request_detail', request_id=request_id)

    context = {
        'invoice': invoice,
        'req_obj': req_obj,
    }
    return render(request, 'dispensing/invoice.html', context)


@admin_or_pharmacist_required
def confirm_payment_api(request):
    """AJAX POST: Confirm payment for an invoice (Cash, UPI, Card)."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed.'}, status=405)

    try:
        data = json.loads(request.body)
        invoice_id = data.get('invoice_id')
        payment_method = data.get('payment_method', 'CASH')

        invoice = services.confirm_payment(
            invoice_id=invoice_id,
            payment_method=payment_method,
            pharmacist_user=request.user
        )

        return JsonResponse({
            'success': True,
            'invoice_number': invoice.invoice_number,
            'payment_status': invoice.get_payment_status_display(),
            'payment_method': invoice.get_payment_method_display()
        })
    except Exception as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)


@pharmacist_required
def direct_dispensing_view(request):
    """
    Flow B: Pharmacist Direct Dispensing / Walk-in / Prescription processing view.
    Allows searching medicine / scanning barcode, selecting FEFO batch, entering quantity,
    executing atomic stock deduction, and generating an invoice.
    """
    medicines = Medicine.objects.filter(status='ACTIVE').order_by('medicine_name')

    if request.method == 'POST':
        patient_name = request.POST.get('patient_name', '').strip()
        patient_id = request.POST.get('patient_id', '').strip()
        mobile_number = request.POST.get('mobile_number', '').strip()
        prescription_number = request.POST.get('prescription_number', '').strip()

        medicine_id = request.POST.get('medicine_id')
        batch_id = request.POST.get('batch_id')
        quantity_str = request.POST.get('quantity', '1')

        try:
            quantity = int(quantity_str)
            if not medicine_id or not batch_id or quantity <= 0:
                raise ValidationError("Please select a medicine, valid batch, and quantity greater than zero.")

            items_to_dispense = [{
                'medicine_id': medicine_id,
                'batch_id': batch_id,
                'quantity': quantity
            }]

            invoice = services.create_direct_dispense(
                patient_name=patient_name,
                patient_id=patient_id,
                mobile_number=mobile_number,
                prescription_number=prescription_number,
                items_to_dispense=items_to_dispense,
                pharmacist_user=request.user
            )
            messages.success(request, f"Dispensing completed successfully. Invoice {invoice.invoice_number} generated.")
            return redirect('view_invoice', request_id=invoice.request.request_id)
        except (ValidationError, Exception) as e:
            messages.error(request, str(e))

    context = {
        'medicines': medicines,
    }
    return render(request, 'dispensing/direct_dispensing.html', context)


@admin_or_pharmacist_required
def prescription_list_view(request):
    """List of Doctor Prescriptions & verification queue."""
    prescriptions = Prescription.objects.prefetch_related('items__medicine').order_by('-created_at')
    
    context = {
        'prescriptions': prescriptions,
        'pending_count': prescriptions.filter(status='PENDING').count(),
        'verified_count': prescriptions.filter(status='VERIFIED').count(),
    }
    return render(request, 'dispensing/prescription_list.html', context)


@admin_or_pharmacist_required
def create_prescription_view(request):
    """Pharmacist view: Record a new Doctor Prescription."""
    medicines = Medicine.objects.filter(status='ACTIVE').order_by('medicine_name')

    if request.method == 'POST':
        patient_name = request.POST.get('patient_name', '').strip()
        patient_id = request.POST.get('patient_id', '').strip()
        doctor_name = request.POST.get('doctor_name', '').strip()
        doctor_license = request.POST.get('doctor_license', '').strip()
        department = request.POST.get('department', 'General Medicine').strip()
        notes = request.POST.get('notes', '').strip()

        medicine_id = request.POST.get('medicine_id')
        quantity_str = request.POST.get('quantity', '1')

        try:
            quantity = int(quantity_str)
            if not patient_name or not patient_id or not medicine_id or quantity <= 0:
                raise ValidationError("Please fill in patient details, select a medicine, and specify quantity.")

            year = timezone.now().year
            count = Prescription.objects.filter(created_at__year=year).count() + 1
            rx_id = f"RX-{year}-{count:04d}"

            rx = Prescription.objects.create(
                prescription_id=rx_id,
                patient_name=patient_name,
                patient_id=patient_id,
                doctor_name=doctor_name or "Dr. Attending Physician",
                doctor_license=doctor_license,
                department=department,
                status=Prescription.Status.VERIFIED,
                notes=notes,
                created_by=request.user
            )

            med = Medicine.objects.get(pk=medicine_id)
            PrescriptionItem.objects.create(
                prescription=rx,
                medicine=med,
                requested_quantity=quantity,
                status=PrescriptionItem.Status.VERIFIED
            )

            messages.success(request, f"Prescription {rx.prescription_id} verified successfully.")
            return redirect('prescription_list')
        except (ValidationError, Exception) as e:
            messages.error(request, str(e))

    context = {'medicines': medicines}
    return render(request, 'dispensing/create_prescription.html', context)


@admin_or_pharmacist_required
def record_return_view(request):
    """Pharmacist view: Process medicine return (patient return / unused stock)."""
    medicines = Medicine.objects.filter(status='ACTIVE').order_by('medicine_name')

    if request.method == 'POST':
        medicine_id = request.POST.get('medicine')
        batch_id = request.POST.get('batch')
        quantity_str = request.POST.get('quantity', '0')
        reason = request.POST.get('reason', '').strip()

        try:
            quantity = int(quantity_str)
            if quantity <= 0 or not reason:
                raise ValidationError("Valid return quantity and reason are required.")

            batch = services.record_medicine_return(
                medicine_id=medicine_id,
                batch_id=batch_id,
                return_qty=quantity,
                reason=reason,
                pharmacist_user=request.user
            )
            messages.success(request, f"Successfully processed return of {quantity} units to Batch {batch.batch_number}.")
            return redirect('inventory_dashboard')
        except (ValidationError, Exception) as e:
            messages.error(request, str(e))

    return render(request, 'dispensing/record_return.html', {'medicines': medicines})


# ==============================================================================
# SECURE MEDICATION DISPENSING & CONTROLLED DRUG ENGINE (ENTERPRISE UPGRADE)
# ==============================================================================

@pharmacist_required
def secure_workspace_view(request):
    """
    Enterprise Secure Dispensing Workspace:
    - Barcode & QR code scanning lookup
    - Active & verified prescription selector
    - Regulatory safety verification checklist
    - Multi-batch FEFO preview breakdown
    - Pharmacist atomic execution
    """
    today = timezone.now().date()
    medicines = Medicine.objects.filter(status='ACTIVE').select_related('category').prefetch_related('batches').order_by('medicine_name')
    prescriptions = Prescription.objects.filter(
        status__in=[Prescription.Status.VERIFIED, Prescription.Status.READY_FOR_DISPENSING, Prescription.Status.PARTIALLY_DISPENSED]
    ).prefetch_related('items__medicine').order_by('-created_at')[:50]

    # Dashboard quick stats
    pending_rx_count = Prescription.objects.filter(status=Prescription.Status.PENDING).count()
    controlled_rx_count = Prescription.objects.filter(
        items__medicine__regulatory_category__in=[
            Medicine.RegulatoryCategory.SCHEDULE_H1,
            Medicine.RegulatoryCategory.SCHEDULE_X,
            Medicine.RegulatoryCategory.NARCOTIC
        ]
    ).distinct().count()
    today_dispensed_count = DispensingRecord.objects.filter(created_at__date=today).count()
    open_discrepancies_count = StockDiscrepancy.objects.filter(status=StockDiscrepancy.Status.OPEN).count()

    context = {
        'medicines': medicines,
        'prescriptions': prescriptions,
        'pending_rx_count': pending_rx_count,
        'controlled_rx_count': controlled_rx_count,
        'today_dispensed_count': today_dispensed_count,
        'open_discrepancies_count': open_discrepancies_count,
    }
    return render(request, 'dispensing/secure_workspace.html', context)


@admin_or_pharmacist_required
def dispensing_history_view(request):
    """
    Searchable audit history of all medication dispensing actions.
    Filters by search keyword (dispensing ID, patient, medicine, batch, Rx, invoice),
    regulatory category, status, date range, and sort order.
    """
    records = DispensingRecord.objects.select_related(
        'medicine', 'pharmacist', 'prescription', 'invoice'
    ).prefetch_related('allocations__batch')

    # Filters
    q = request.GET.get('q', '').strip()
    category = request.GET.get('regulatory_category', '').strip()
    status = request.GET.get('status', '').strip()
    start_date = request.GET.get('start_date', '').strip()
    end_date = request.GET.get('end_date', '').strip()
    sort = request.GET.get('sort', 'date_desc').strip()

    if q:
        records = records.filter(
            Q(dispensing_id__icontains=q) |
            Q(patient_name__icontains=q) |
            Q(patient_id__icontains=q) |
            Q(medicine__medicine_name__icontains=q) |
            Q(pharmacist__username__icontains=q) |
            Q(allocations__batch__batch_number__icontains=q) |
            Q(prescription__prescription_id__icontains=q) |
            Q(prescription__prescription_number__icontains=q) |
            Q(invoice__invoice_number__icontains=q)
        ).distinct()

    if category:
        records = records.filter(regulatory_category=category)
    if status:
        records = records.filter(status=status)
    if start_date:
        records = records.filter(created_at__date__gte=start_date)
    if end_date:
        records = records.filter(created_at__date__lte=end_date)

    # Stable Sorting
    if sort == 'date_asc':
        records = records.order_by('created_at', 'id')
    elif sort == 'qty_desc':
        records = records.order_by('-total_quantity', '-created_at', '-id')
    elif sort == 'qty_asc':
        records = records.order_by('total_quantity', '-created_at', '-id')
    elif sort == 'med_asc':
        records = records.order_by('medicine__medicine_name', '-created_at', '-id')
    else:
        records = records.order_by('-created_at', '-id')

    # All-time vs Filtered aggregates
    all_time_records = DispensingRecord.objects.count()
    all_time_units = DispensingRecord.objects.aggregate(total=Sum('total_quantity'))['total'] or 0

    total_records = records.count()
    total_dispensed_units = records.aggregate(total=Sum('total_quantity'))['total'] or 0

    paginator = Paginator(records, 25)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        'page_obj': page_obj,
        'total_records': total_records,
        'total_dispensed_units': total_dispensed_units,
        'all_time_records': all_time_records,
        'all_time_units': all_time_units,
        'q': q,
        'regulatory_category': category,
        'status': status,
        'start_date': start_date,
        'end_date': end_date,
        'sort': sort,
        'categories': Medicine.RegulatoryCategory.choices,
        'statuses': DispensingRecord.Status.choices,
    }
    return render(request, 'dispensing/dispensing_history.html', context)


@admin_or_pharmacist_required
def dispensing_detail_view(request, dispensing_id):
    """
    Detailed audit view of a single Dispensing Record.
    Shows exact multi-batch breakdown, verification checklist, prescription details, and allows reversal.
    """
    record = get_object_or_404(
        DispensingRecord.objects.select_related('medicine', 'pharmacist', 'prescription', 'invoice').prefetch_related('allocations__batch', 'allocations__stock_transaction'),
        dispensing_id=dispensing_id
    )

    controlled_entries = []
    if record.medicine.is_controlled:
        controlled_entries = list(ControlledMedicineRegister.objects.filter(dispensing_record=record).select_related('batch'))
    controlled_entry = controlled_entries[0] if controlled_entries else None

    context = {
        'record': record,
        'allocations': record.allocations.all(),
        'controlled_entry': controlled_entry,
        'controlled_entries': controlled_entries,
    }
    return render(request, 'dispensing/dispensing_detail.html', context)


@admin_or_pharmacist_required
def controlled_register_view(request):
    """
    Controlled Medicine Running Audit Register (Schedule H1, Schedule X, Narcotics, Controlled Other).
    Maintains statutory audit trail of every entry and dispensing transaction.
    """
    entries = ControlledMedicineRegister.objects.select_related(
        'medicine', 'batch', 'pharmacist', 'dispensing_record'
    ).order_by('-recorded_at', '-id')

    # Filters
    q = request.GET.get('q', '').strip()
    category = request.GET.get('regulatory_category', '').strip()
    tx_type = request.GET.get('transaction_type', '').strip()
    start_date = request.GET.get('start_date', '').strip()
    end_date = request.GET.get('end_date', '').strip()

    if q:
        entries = entries.filter(
            Q(entry_id__icontains=q) |
            Q(medicine__medicine_name__icontains=q) |
            Q(batch__batch_number__icontains=q) |
            Q(patient_name__icontains=q) |
            Q(patient_id__icontains=q) |
            Q(doctor_name__icontains=q) |
            Q(prescription_number__icontains=q) |
            Q(reference_number__icontains=q) |
            Q(pharmacist__username__icontains=q)
        )
    if category:
        entries = entries.filter(medicine__regulatory_category=category)
    if tx_type:
        entries = entries.filter(transaction_type=tx_type)
    if start_date:
        entries = entries.filter(recorded_at__date__gte=start_date)
    if end_date:
        entries = entries.filter(recorded_at__date__lte=end_date)

    # Filtered and All-Time totals
    total_dispensed = entries.aggregate(total=Sum('quantity_out'))['total'] or 0
    total_received = entries.aggregate(total=Sum('quantity_in'))['total'] or 0

    all_time_dispensed = ControlledMedicineRegister.objects.aggregate(total=Sum('quantity_out'))['total'] or 0
    all_time_received = ControlledMedicineRegister.objects.aggregate(total=Sum('quantity_in'))['total'] or 0

    controlled_sku_count = Medicine.objects.filter(
        status='ACTIVE',
        regulatory_category__in=[
            Medicine.RegulatoryCategory.SCHEDULE_H1,
            Medicine.RegulatoryCategory.SCHEDULE_X,
            Medicine.RegulatoryCategory.NARCOTIC,
            Medicine.RegulatoryCategory.CONTROLLED_OTHER,
        ]
    ).count()

    paginator = Paginator(entries, 30)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        'page_obj': page_obj,
        'total_dispensed': total_dispensed,
        'total_received': total_received,
        'all_time_dispensed': all_time_dispensed,
        'all_time_received': all_time_received,
        'controlled_sku_count': controlled_sku_count,
        'q': q,
        'regulatory_category': category,
        'transaction_type': tx_type,
        'start_date': start_date,
        'end_date': end_date,
        'categories': [
            (Medicine.RegulatoryCategory.SCHEDULE_H1, 'Schedule H1'),
            (Medicine.RegulatoryCategory.SCHEDULE_X, 'Schedule X'),
            (Medicine.RegulatoryCategory.NARCOTIC, 'Narcotic / Habit-forming'),
            (Medicine.RegulatoryCategory.CONTROLLED_OTHER, 'Controlled Other'),
        ],
        'transaction_types': ControlledMedicineRegister.TransactionType.choices,
    }
    return render(request, 'dispensing/controlled_register.html', context)


@admin_or_pharmacist_required
def discrepancy_list_view(request):
    """
    Automated Discrepancy Monitoring & Resolution Queue.
    Scans for discrepancies and lists open issues for clinical review.
    """
    # Auto-run discrepancy scanner
    secure_dispensing_service.detect_discrepancies()

    discrepancies = StockDiscrepancy.objects.select_related('medicine', 'batch', 'resolved_by', 'detected_by').order_by('-detected_at', '-id')

    status_filter = request.GET.get('status', 'OPEN').strip()
    if status_filter == 'INVESTIGATING':
        discrepancies = discrepancies.filter(status__in=[StockDiscrepancy.Status.UNDER_REVIEW, 'INVESTIGATING'])
    elif status_filter and status_filter != 'ALL':
        discrepancies = discrepancies.filter(status=status_filter)

    open_count = StockDiscrepancy.objects.filter(status=StockDiscrepancy.Status.OPEN).count()
    investigating_count = StockDiscrepancy.objects.filter(status__in=[StockDiscrepancy.Status.UNDER_REVIEW, 'INVESTIGATING']).count()
    resolved_count = StockDiscrepancy.objects.filter(status=StockDiscrepancy.Status.RESOLVED).count()

    paginator = Paginator(discrepancies, 25)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        'page_obj': page_obj,
        'status_filter': status_filter,
        'open_count': open_count,
        'investigating_count': investigating_count,
        'resolved_count': resolved_count,
    }
    return render(request, 'dispensing/discrepancy_list.html', context)


@admin_or_pharmacist_required
def discrepancy_resolve_view(request, discrepancy_id):
    """
    POST action to resolve a stock discrepancy.
    """
    if request.method == 'POST':
        action_taken = request.POST.get('action_taken', '').strip()
        notes = request.POST.get('notes', '').strip()
        try:
            secure_dispensing_service.resolve_discrepancy(
                discrepancy_id=discrepancy_id,
                pharmacist_user=request.user,
                resolution_notes=notes,
                action_taken=action_taken
            )
            messages.success(request, f"Discrepancy #{discrepancy_id} resolved successfully.")
        except Exception as e:
            messages.error(request, f"Failed to resolve discrepancy: {str(e)}")
    return redirect('discrepancy_list')


@admin_or_pharmacist_required
def stock_reconciliation_view(request):
    """
    Physical Inventory Count Audit & Batch Reconciliation interface.
    Allows pharmacists to enter physical count and adjust system balances with audit trails.
    """
    medicines = Medicine.objects.filter(status='ACTIVE').prefetch_related('batches').order_by('medicine_name')

    if request.method == 'POST':
        medicine_id = request.POST.get('medicine_id')
        batch_id = request.POST.get('batch_id')
        physical_count_str = request.POST.get('physical_count', '').strip()
        reason = request.POST.get('reason', '').strip()

        if not medicine_id or not batch_id:
            messages.error(request, "Please select both a medicine and a specific batch.")
        elif not physical_count_str.isdigit():
            messages.error(request, "Physical count must be a non-negative integer.")
        elif not reason:
            messages.error(request, "A clinical or audit reason is mandatory for reconciliation.")
        else:
            try:
                physical_count = int(physical_count_str)
                rec = secure_dispensing_service.reconcile_batch_stock(
                    medicine_id=medicine_id,
                    batch_id=batch_id,
                    physical_quantity=physical_count,
                    reason=reason,
                    pharmacist_user=request.user
                )
                messages.success(
                    request,
                    f"Reconciliation #{rec.session_id} completed: Batch {rec.batch.batch_number} adjusted to {rec.physical_quantity} units (Variance: {rec.variance:+d})."
                )
                return redirect('stock_reconciliation')
            except (ValidationError, Exception) as e:
                messages.error(request, f"Reconciliation failed: {str(e.message if hasattr(e, 'message') else e)}")

    recent_reconciliations = StockReconciliation.objects.select_related(
        'medicine', 'batch', 'performed_by', 'stock_transaction'
    ).order_by('-created_at')[:40]

    context = {
        'medicines': medicines,
        'recent_reconciliations': recent_reconciliations,
    }
    return render(request, 'dispensing/stock_reconciliation.html', context)


# ==============================================================================
# SECURE DISPENSING REST / JSON APIs
# ==============================================================================

@admin_or_pharmacist_required
def api_secure_barcode_scan(request):
    """
    GET/POST API: Non-deducting Barcode & QR Code Validation.
    Accepts: ?code=... or JSON { code: ... }
    Returns rich metadata for verification modal.
    """
    code = request.GET.get('code') or request.POST.get('code')
    if not code and request.body:
        try:
            data = json.loads(request.body)
            code = data.get('code')
        except Exception:
            pass

    if not code:
        return JsonResponse({'success': False, 'message': 'Barcode/QR parameter required.'}, status=400)

    result = secure_dispensing_service.scan_barcode_or_qr(code)
    return JsonResponse(result)


@pharmacist_required
def api_secure_dispense(request):
    """
    POST API: Atomic Secure Dispensing Execution.
    Accepts JSON payload:
    {
        "medicine_id": 1,
        "requested_qty": 70,
        "prescription_id": "RX-2026-0001",  # optional
        "patient_name": "John Doe",
        "patient_id": "UHID-1234",
        "batch_id": 5,                      # optional specific batch preference
        "verification_checklist": { ... },
        "notes": "..."
    }
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'POST method required.'}, status=405)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        data = request.POST

    medicine_id = data.get('medicine_id')
    requested_qty = data.get('requested_qty')
    prescription_id = data.get('prescription_id')
    patient_name = data.get('patient_name', '')
    patient_id = data.get('patient_id', '')
    batch_id = data.get('batch_id')
    verification_checklist = data.get('verification_checklist')
    notes = data.get('notes', '')

    if not medicine_id or not requested_qty:
        return JsonResponse({'success': False, 'message': 'Medicine ID and requested quantity are required.'}, status=400)

    try:
        result = secure_dispensing_service.execute_secure_dispensing(
            medicine=medicine_id,
            requested_qty=requested_qty,
            pharmacist_user=request.user,
            prescription=prescription_id,
            patient_name=patient_name,
            patient_id=patient_id,
            verification_checklist=verification_checklist,
            specific_batch=batch_id,
            notes=notes
        )
        return JsonResponse({
            'success': True,
            'dispensing_id': result['dispensing_id'],
            'invoice_number': result.get('invoice_number'),
            'invoice_id': result.get('invoice_id'),
            'request_id': result.get('request_id'),
            'message': result['message'],
            'allocations': [
                {
                    'batch_number': a.batch_number,
                    'quantity': a.quantity_allocated,
                    'expiry_date': a.expiry_date.strftime('%Y-%m-%d'),
                    'total_price': str(a.total_price)
                }
                for a in result['allocations']
            ]
        })
    except (ValidationError, Exception) as e:
        return JsonResponse({'success': False, 'message': str(e.message if hasattr(e, 'message') else e)}, status=400)


@admin_or_pharmacist_required
def api_reverse_dispense(request):
    """
    POST API: Reverse a Dispensing Record.
    Accepts JSON: { "dispensing_id": "DISP-2026-00001", "reason": "Patient Return" }
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'POST method required.'}, status=405)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        data = request.POST

    dispensing_id = data.get('dispensing_id')
    reason = data.get('reason', '')

    if not dispensing_id:
        return JsonResponse({'success': False, 'message': 'Dispensing ID is required.'}, status=400)

    try:
        res = secure_dispensing_service.reverse_dispensing(
            dispensing_id=dispensing_id,
            pharmacist_user=request.user,
            reason=reason
        )
        return JsonResponse(res)
    except (ValidationError, Exception) as e:
        return JsonResponse({'success': False, 'message': str(e.message if hasattr(e, 'message') else e)}, status=400)


@admin_or_pharmacist_required
def api_verify_prescription(request):
    """
    POST API: Verifies a prescription.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'POST required.'}, status=405)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        data = request.POST

    prescription_id = data.get('prescription_id')
    if not prescription_id:
        return JsonResponse({'success': False, 'message': 'Prescription ID required.'}, status=400)

    try:
        rx = secure_dispensing_service.verify_prescription(prescription_id, request.user)
        return JsonResponse({
            'success': True,
            'message': f"Prescription {rx.prescription_id} verified successfully.",
            'status': rx.status,
            'status_display': rx.get_status_display()
        })
    except (ValidationError, Exception) as e:
        return JsonResponse({'success': False, 'message': str(e.message if hasattr(e, 'message') else e)}, status=400)


@admin_or_pharmacist_required
def api_reject_prescription(request):
    """
    POST API: Rejects a prescription.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'POST required.'}, status=405)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        data = request.POST

    prescription_id = data.get('prescription_id')
    reason = data.get('reason', '')
    if not prescription_id or not reason:
        return JsonResponse({'success': False, 'message': 'Prescription ID and rejection reason required.'}, status=400)

    try:
        rx = secure_dispensing_service.reject_prescription(prescription_id, request.user, reason)
        return JsonResponse({
            'success': True,
            'message': f"Prescription {rx.prescription_id} rejected.",
            'status': rx.status,
            'status_display': rx.get_status_display()
        })
    except (ValidationError, Exception) as e:
        return JsonResponse({'success': False, 'message': str(e.message if hasattr(e, 'message') else e)}, status=400)


@admin_or_pharmacist_required
def api_medicine_search(request):
    """
    GET API: Search Medicine Master catalogue with real-time stock availability.
    Supports query by: medicine name, generic name, brand name, strength, dosage form, barcode, category.
    """
    q = request.GET.get('q', '').strip()
    otc_only = request.GET.get('otc_only', '').lower() in ['true', '1', 'yes']
    limit = min(int(request.GET.get('limit', 25)), 100)
    today = timezone.now().date()

    query = Medicine.objects.filter(status='ACTIVE').select_related('category').prefetch_related('batches')

    if otc_only:
        query = query.filter(regulatory_category=Medicine.RegulatoryCategory.NORMAL)

    if q:
        query = query.filter(
            Q(medicine_name__icontains=q) |
            Q(generic_name__icontains=q) |
            Q(brand_name__icontains=q) |
            Q(barcode__iexact=q) |
            Q(strength__icontains=q) |
            Q(dosage_form__icontains=q) |
            Q(category__name__icontains=q)
        )

    medicines = query.order_by('medicine_name')[:limit]

    results = []
    for m in medicines:
        active_batches = [
            b for b in m.batches.all()
            if b.expiry_date > today and b.quantity_available > 0 and b.status not in ['EXPIRED', 'QUARANTINED', 'RETURNED']
        ]
        available_stock = sum(b.quantity_available for b in active_batches)
        primary_price = str(active_batches[0].selling_price) if active_batches and active_batches[0].selling_price else '0.00'

        results.append({
            'id': m.id,
            'medicine_name': m.medicine_name,
            'generic_name': m.generic_name or '',
            'brand_name': m.brand_name or '',
            'dosage_form': m.dosage_form or '',
            'strength': m.strength or '',
            'manufacturer': m.manufacturer or 'N/A',
            'category': m.category.name if m.category else 'General',
            'regulatory_category': m.regulatory_category,
            'regulatory_display': m.get_regulatory_category_display(),
            'is_controlled': m.is_controlled,
            'is_prescription_required': m.is_prescription_required,
            'barcode': m.barcode or '',
            'available_stock': available_stock,
            'in_stock': available_stock > 0,
            'selling_price': primary_price,
            'batches_count': len(active_batches)
        })

    return JsonResponse({
        'success': True,
        'count': len(results),
        'medicines': results
    })


@admin_or_pharmacist_required
def api_batch_preview(request):
    """
    GET API: Authoritative FEFO batch allocation preview.
    Accepts: ?medicine_id=123&qty=50&batch_id=45 (optional)
    """
    medicine_id = request.GET.get('medicine_id')
    qty_str = request.GET.get('qty', '1')
    batch_id = request.GET.get('batch_id')

    if not medicine_id:
        return JsonResponse({'success': False, 'message': 'Medicine ID is required.'}, status=400)

    try:
        qty = int(qty_str)
        if qty <= 0:
            return JsonResponse({'success': False, 'message': 'Quantity must be greater than zero.'}, status=400)
    except ValueError:
        return JsonResponse({'success': False, 'message': 'Invalid quantity.'}, status=400)

    try:
        med = Medicine.objects.get(pk=medicine_id)
    except Medicine.DoesNotExist:
        return JsonResponse({'success': False, 'message': 'Medicine not found.'}, status=404)

    preview = secure_dispensing_service.preview_fefo_allocation(
        medicine=med,
        requested_qty=qty,
        specific_batch=batch_id
    )
    return JsonResponse(preview)


@admin_or_pharmacist_required
def api_dispensing_dashboard_stats(request):
    """
    GET API: Real-time dynamic counts for workspace dashboard cards.
    """
    today = timezone.now().date()
    pending_rx_count = Prescription.objects.filter(status=Prescription.Status.PENDING).count()
    controlled_rx_count = Prescription.objects.filter(
        items__medicine__regulatory_category__in=[
            Medicine.RegulatoryCategory.SCHEDULE_H1,
            Medicine.RegulatoryCategory.SCHEDULE_X,
            Medicine.RegulatoryCategory.NARCOTIC
        ]
    ).distinct().count()
    today_dispensed_count = DispensingRecord.objects.filter(created_at__date=today).count()
    open_discrepancies_count = StockDiscrepancy.objects.filter(status=StockDiscrepancy.Status.OPEN).count()

    return JsonResponse({
        'success': True,
        'pending_rx_count': pending_rx_count,
        'controlled_rx_count': controlled_rx_count,
        'today_dispensed_count': today_dispensed_count,
        'open_discrepancies_count': open_discrepancies_count,
    })


@admin_or_pharmacist_required
def api_patient_search(request):
    """
    GET API: Search Patient registry by UHID, name, or phone.
    Returns matching patients with active prescriptions.
    """
    q = request.GET.get('q', '').strip()
    if not q:
        patients = Patient.objects.all().order_by('-created_at')[:20]
    else:
        patients = Patient.objects.filter(
            Q(uhid__icontains=q) |
            Q(name__icontains=q) |
            Q(mobile_number__icontains=q)
        ).order_by('name')[:30]

    results = []
    for p in patients:
        active_rxs = Prescription.objects.filter(
            Q(patient_record=p) | Q(patient_id__iexact=p.uhid),
            status__in=[
                Prescription.Status.VERIFIED,
                Prescription.Status.READY_FOR_DISPENSING,
                Prescription.Status.PARTIALLY_DISPENSED
            ]
        ).order_by('-prescription_date')[:5]

        rx_list = [
            {
                'id': rx.id,
                'prescription_id': rx.prescription_id,
                'doctor_name': rx.doctor_name,
                'date': rx.prescription_date.strftime('%Y-%m-%d'),
                'status': rx.status,
                'status_display': rx.get_status_display(),
                'item_count': rx.items.count()
            }
            for rx in active_rxs
        ]

        results.append({
            'id': p.id,
            'uhid': p.uhid,
            'name': p.name,
            'gender': p.gender,
            'age': p.age,
            'phone': p.phone,
            'email': p.email,
            'address': p.address,
            'blood_group': p.blood_group,
            'allergies': p.allergies,
            'medical_history': p.medical_history,
            'active_prescriptions': rx_list
        })

    return JsonResponse({'success': True, 'count': len(results), 'patients': results})


@admin_or_pharmacist_required
def api_register_patient(request):
    """
    POST API: Registers a new patient with unique server-generated UHID.
    Enforces uniqueness, never silent reuse.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'POST method required.'}, status=405)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        data = request.POST

    name = data.get('name', '').strip()
    gender = data.get('gender', 'MALE').upper()
    age = data.get('age')
    phone = data.get('phone', '').strip()
    email = data.get('email', '').strip()
    address = data.get('address', '').strip()
    blood_group = data.get('blood_group', '').strip()
    allergies = data.get('allergies', '').strip()
    medical_history = data.get('medical_history', '').strip()

    if not name:
        return JsonResponse({'success': False, 'message': 'Patient full name is mandatory.'}, status=400)

    age_int = None
    if age is not None and str(age).strip() != '':
        try:
            age_int = int(age)
            if age_int < 0 or age_int > 150:
                return JsonResponse({'success': False, 'message': 'Please enter a valid age between 0 and 150.'}, status=400)
        except ValueError:
            return JsonResponse({'success': False, 'message': 'Age must be a numeric integer.'}, status=400)

    if gender not in ['MALE', 'FEMALE', 'OTHER']:
        gender = 'OTHER'

    uhid = Patient.generate_uhid()

    patient = Patient.objects.create(
        uhid=uhid,
        name=name,
        gender=gender,
        age=age_int,
        mobile_number=phone,
        email=email,
        address=address,
        blood_group=blood_group,
        allergies=allergies,
        medical_history=medical_history
    )

    log_audit(
        user=request.user,
        action='PATIENT_REGISTERED',
        description=f"Patient {patient.name} registered with UHID {patient.uhid} by {request.user.username}.",
        performed_by=request.user
    )

    return JsonResponse({
        'success': True,
        'message': f"Patient '{patient.name}' registered successfully with UHID {patient.uhid}.",
        'patient': {
            'id': patient.id,
            'uhid': patient.uhid,
            'name': patient.name,
            'gender': patient.gender,
            'age': patient.age,
            'phone': patient.phone,
            'email': patient.email,
            'blood_group': patient.blood_group,
            'allergies': patient.allergies
        }
    })


@admin_or_pharmacist_required
def api_doctor_search(request):
    """
    GET API: Search Doctor registry.
    """
    q = request.GET.get('q', '').strip()
    doctors = Doctor.objects.filter(is_active=True)
    if q:
        doctors = doctors.filter(
            Q(name__icontains=q) |
            Q(doctor_code__icontains=q) |
            Q(specialization__icontains=q) |
            Q(department__icontains=q)
        )
    doctors = doctors.order_by('name')[:30]

    results = [
        {
            'id': d.id,
            'doctor_code': d.doctor_code,
            'name': d.name,
            'specialization': d.specialization,
            'department': d.department,
            'registration_number': d.registration_number,
            'phone': d.phone
        }
        for d in doctors
    ]

    return JsonResponse({'success': True, 'count': len(results), 'doctors': results})


@login_required
def api_create_prescription(request):
    """
    POST API: Authorized Doctor or Clinical Admin issues a new prescription.
    Pharmacists are restricted from issuing prescriptions (they only verify and dispense).
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'POST method required.'}, status=405)

    # Permission check: Doctor or Admin only
    user = request.user
    is_authorized_prescriber = (
        user.is_superuser or
        user.is_staff or
        getattr(user, 'role', '') in ['ADMIN', 'DOCTOR'] or
        getattr(user, 'is_doctor', False)
    )

    if not is_authorized_prescriber:
        return JsonResponse({
            'success': False,
            'message': 'Unauthorized: Only authorized Medical Doctors or Clinical Administrators can issue and sign prescriptions. Pharmacists retrieve and verify prescriptions.'
        }, status=403)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        data = request.POST

    patient_ref = data.get('patient_id')
    doctor_ref = data.get('doctor_id')
    department = data.get('department', 'General Medicine').strip()
    diagnosis = data.get('diagnosis', '').strip()
    notes = data.get('notes', '').strip()
    items_data = data.get('items', [])

    if not patient_ref:
        return JsonResponse({'success': False, 'message': 'Patient reference is required.'}, status=400)
    if not items_data or len(items_data) == 0:
        return JsonResponse({'success': False, 'message': 'At least one prescription item must be added.'}, status=400)

    # Find Patient
    patient = Patient.objects.filter(
        Q(uhid__iexact=str(patient_ref)) | Q(id=int(patient_ref) if str(patient_ref).isdigit() else 0)
    ).first()

    patient_name = patient.name if patient else data.get('patient_name', 'Patient')
    patient_uhid = patient.uhid if patient else str(patient_ref)

    # Find Doctor
    doctor = None
    if doctor_ref:
        doctor = Doctor.objects.filter(
            Q(doctor_code__iexact=str(doctor_ref)) | Q(id=int(doctor_ref) if str(doctor_ref).isdigit() else 0)
        ).first()

    if not doctor and hasattr(user, 'doctor_profile'):
        doctor = user.doctor_profile

    doctor_name = doctor.name if doctor else (user.get_full_name() or user.username)
    doctor_reg = doctor.registration_number if doctor else ''
    doctor_lic = doctor.license_number if doctor else ''

    # Generate Prescription ID (format: RX-YYYY-NNNN)
    year = timezone.now().year
    rx_count = Prescription.objects.filter(created_at__year=year).count() + 1
    rx_id = f"RX-{year}-{rx_count:04d}"
    while Prescription.objects.filter(prescription_id=rx_id).exists():
        rx_count += 1
        rx_id = f"RX-{year}-{rx_count:04d}"

    today = timezone.now().date()
    expiry_date = today + timedelta(days=30)

    with transaction.atomic():
        rx = Prescription.objects.create(
            prescription_id=rx_id,
            prescription_number=f"PHYS-{rx_id}",
            patient_record=patient,
            patient_name=patient_name,
            patient_id=patient_uhid,
            doctor_record=doctor,
            doctor_name=doctor_name,
            doctor_registration=doctor_reg,
            doctor_license=doctor_lic,
            prescription_date=today,
            expiry_date=expiry_date,
            department=department or 'General Medicine',
            diagnosis=diagnosis,
            notes=notes,
            status=Prescription.Status.PENDING,
            created_by=user
        )

        for item in items_data:
            med_id = item.get('medicine_id')
            qty = int(item.get('quantity') or item.get('prescribed_quantity', 1))
            dosage = item.get('dosage', '1 tablet daily')
            frequency = item.get('frequency', '1-0-1')
            duration = item.get('duration', '5 days')
            route = item.get('route', 'Oral')
            instructions = item.get('instructions', '')

            med = Medicine.objects.get(pk=med_id)

            PrescriptionItem.objects.create(
                prescription=rx,
                medicine=med,
                strength=med.strength,
                dosage_form=med.dosage_form,
                dosage=dosage,
                frequency=frequency,
                duration=duration,
                route=route,
                dosage_instructions=instructions,
                prescribed_quantity=qty,
                requested_quantity=qty,
                dispensed_quantity=0,
                status=PrescriptionItem.Status.PENDING
            )

        log_audit(
            user=user,
            action='PRESCRIPTION_CREATED',
            description=f"Prescription {rx.prescription_id} issued for {patient_name} by Dr. {doctor_name}.",
            performed_by=user
        )

    return JsonResponse({
        'success': True,
        'message': f"Prescription {rx.prescription_id} issued successfully and sent to pharmacy queue.",
        'prescription_id': rx.prescription_id,
        'id': rx.id
    })



