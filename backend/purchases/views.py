"""
purchases/views.py
Phase 4 — Procurement, Purchase Orders, Goods Receiving, and Restocking Workflow
"""
from decimal import Decimal
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum, Count, Q, F
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.core.mail import send_mail
from django.conf import settings as django_settings
from django.utils import timezone
from django.views.decorators.http import require_POST


from inventory.views import admin_required, admin_or_pharmacist_required
from medicines.models import Medicine, MedicineBatch
from suppliers.models import Supplier, SupplierMedicine
from inventory.models import StockAlert, Inventory
from .models import (
    PurchaseRequest, PurchaseRequestItem,
    PurchaseOrder, PurchaseOrderItem,
    GoodsReceipt, GoodsReceiptItem
)
from . import services


# ─────────────────────────────────────────────────────────────────────────────
# 1. PURCHASE REQUESTS
# ─────────────────────────────────────────────────────────────────────────────

@admin_or_pharmacist_required
def purchase_request_list_view(request):
    """View purchase requests with status & priority filters."""
    requests_qs = PurchaseRequest.objects.select_related(
        'recommended_supplier', 'created_by', 'reviewed_by', 'converted_to_po'
    ).prefetch_related('items__medicine').order_by('-created_at')

    status_filter = request.GET.get('status', '')
    priority_filter = request.GET.get('priority', '')
    search_q = request.GET.get('search', '').strip()

    if status_filter:
        requests_qs = requests_qs.filter(status=status_filter)
    if priority_filter:
        requests_qs = requests_qs.filter(priority=priority_filter)
    if search_q:
        requests_qs = requests_qs.filter(
            Q(request_id__icontains=search_q) |
            Q(reason__icontains=search_q)
        )

    context = {
        'requests': requests_qs,
        'total_requests': PurchaseRequest.objects.count(),
        'pending_approval': PurchaseRequest.objects.filter(status='PENDING_APPROVAL').count(),
        'approved_count': PurchaseRequest.objects.filter(status='APPROVED').count(),
        'status_filter': status_filter,
        'priority_filter': priority_filter,
        'search_q': search_q,
    }
    return render(request, 'purchases/purchase_request_list.html', context)


@admin_or_pharmacist_required
def create_purchase_request_view(request):
    """Create a new Purchase Request with AI recommendation calculations."""
    medicines = Medicine.objects.filter(status='ACTIVE').order_by('medicine_name')
    suppliers = Supplier.objects.filter(status=Supplier.SupplierStatus.ACTIVE).order_by('company_name')

    alert_id = request.GET.get('alert_id')
    medicine_id_param = request.GET.get('medicine_id')
    initial_med = None
    initial_ai_rec = None
    initial_best_supplier = None

    if alert_id:
        try:
            alert = StockAlert.objects.get(pk=alert_id)
            initial_med = alert.medicine
            initial_ai_rec = services.get_ai_reorder_recommendation(initial_med)
            best_sm = services.get_best_supplier(initial_med)
            if best_sm:
                initial_best_supplier = best_sm.supplier
        except StockAlert.DoesNotExist:
            pass
    elif medicine_id_param:
        try:
            initial_med = Medicine.objects.get(pk=medicine_id_param, status='ACTIVE')
            initial_ai_rec = services.get_ai_reorder_recommendation(initial_med)
            best_sm = services.get_best_supplier(initial_med)
            if best_sm:
                initial_best_supplier = best_sm.supplier
        except Medicine.DoesNotExist:
            pass


    if request.method == 'POST':
        medicine_id = request.POST.get('medicine')
        requested_qty_str = request.POST.get('requested_qty', '0')
        priority = request.POST.get('priority', 'NORMAL')
        reason = request.POST.get('reason', '').strip()
        supplier_id = request.POST.get('supplier')

        try:
            medicine = Medicine.objects.get(pk=medicine_id)
            requested_qty = int(requested_qty_str)
            if requested_qty <= 0:
                raise ValidationError("Requested quantity must be greater than zero.")

            # Check if an active (DRAFT / PENDING_APPROVAL) request already exists for this medicine
            existing_item = PurchaseRequestItem.objects.filter(
                medicine=medicine,
                purchase_request__status__in=[
                    PurchaseRequest.PRStatus.DRAFT,
                    PurchaseRequest.PRStatus.PENDING_APPROVAL,
                ]
            ).select_related('purchase_request').first()

            if existing_item:
                pr = existing_item.purchase_request
                existing_item.requested_qty = requested_qty
                existing_item.approved_qty = requested_qty
                existing_item.save(update_fields=['requested_qty', 'approved_qty'])
                messages.info(
                    request,
                    f"Updated existing active Purchase Request {pr.request_id} for {medicine.medicine_name} "
                    f"to {requested_qty} units (prevented duplicate request)."
                )
                return redirect('purchase_request_list')

            ai_rec = services.get_ai_reorder_recommendation(medicine)
            best_sm = services.get_best_supplier(medicine)
            rec_supplier = Supplier.objects.get(pk=supplier_id) if supplier_id else (best_sm.supplier if best_sm else None)

            req_id = PurchaseRequest.generate_request_id()
            pr = PurchaseRequest.objects.create(
                request_id=req_id,
                priority=priority,
                status=PurchaseRequest.PRStatus.PENDING_APPROVAL,
                reason=reason or f"Restocking request for {medicine.medicine_name}",
                recommended_supplier=rec_supplier,
                created_by=request.user
            )

            inv = getattr(medicine, 'inventory', None)
            PurchaseRequestItem.objects.create(
                purchase_request=pr,
                medicine=medicine,
                current_stock=inv.current_stock if inv else 0,
                reorder_level=inv.reorder_level if inv else 0,
                ai_recommended_qty=ai_rec['recommended_qty'],
                requested_qty=requested_qty,
                approved_qty=requested_qty,
                suggested_supplier=best_sm.supplier if best_sm else None,
                suggested_unit_price=best_sm.unit_price if best_sm else Decimal('0.00')
            )

            messages.success(request, f"Purchase Request {pr.request_id} created successfully and submitted for Admin review.")
            return redirect('purchase_request_list')
        except (Medicine.DoesNotExist, ValidationError, ValueError) as e:
            messages.error(request, str(e))

    context = {
        'medicines': medicines,
        'suppliers': suppliers,
        'initial_med': initial_med,
        'initial_ai_rec': initial_ai_rec,
        'initial_best_supplier': initial_best_supplier,
    }
    return render(request, 'purchases/create_purchase_request.html', context)


@admin_required
def approve_purchase_request_view(request, pk):
    """Admin view: Review and Approve/Modify Purchase Request and convert into PO."""
    pr = get_object_or_404(PurchaseRequest, pk=pk)
    suppliers = Supplier.objects.filter(status=Supplier.SupplierStatus.ACTIVE).order_by('company_name')

    if request.method == 'POST':
        action = request.POST.get('action')
        supplier_id = request.POST.get('supplier')
        approved_qty_str = request.POST.get('approved_qty')
        rejection_reason = request.POST.get('rejection_reason', '').strip()

        try:
            if action == 'REJECT':
                pr.status = PurchaseRequest.PRStatus.REJECTED
                pr.reviewed_by = request.user
                pr.reviewed_at = timezone.now()
                pr.rejection_reason = rejection_reason or "Request rejected by Administrator."
                pr.save()
                messages.warning(request, f"Purchase Request {pr.request_id} has been rejected.")
                return redirect('purchase_request_list')

            # Approval action
            if approved_qty_str:
                approved_qty = int(approved_qty_str)
                for item in pr.items.all():
                    item.approved_qty = approved_qty
                    item.save()

            supplier_override = Supplier.objects.get(pk=supplier_id) if supplier_id else None

            pr.status = PurchaseRequest.PRStatus.APPROVED
            pr.reviewed_by = request.user
            pr.reviewed_at = timezone.now()
            pr.save()

            # Convert to PO
            po = services.convert_pr_to_po(
                purchase_request=pr,
                admin_user=request.user,
                supplier_override=supplier_override
            )

            messages.success(request, f"Purchase Request {pr.request_id} APPROVED and converted to Purchase Order {po.po_number}.")
            return redirect('po_detail', pk=po.pk)

        except (ValidationError, Exception) as e:
            messages.error(request, str(e))

    context = {
        'pr': pr,
        'suppliers': suppliers,
    }
    return render(request, 'purchases/approve_purchase_request.html', context)


# ─────────────────────────────────────────────────────────────────────────────
# 2. PURCHASE ORDERS
# ─────────────────────────────────────────────────────────────────────────────

@admin_or_pharmacist_required
def purchase_orders_list_view(request):
    """List all Purchase Orders with status & priority filters."""
    orders_qs = PurchaseOrder.objects.select_related('supplier', 'created_by', 'approved_by').order_by('-created_at')

    status_filter = request.GET.get('status', '')
    priority_filter = request.GET.get('priority', '')
    search_q = request.GET.get('search', '').strip()

    if status_filter:
        orders_qs = orders_qs.filter(status=status_filter)
    if priority_filter:
        orders_qs = orders_qs.filter(priority=priority_filter)
    if search_q:
        orders_qs = orders_qs.filter(
            Q(po_number__icontains=search_q) |
            Q(supplier__company_name__icontains=search_q)
        )

    context = {
        'orders': orders_qs[:50],
        'total_orders': PurchaseOrder.objects.count(),
        'pending_orders': PurchaseOrder.objects.filter(status__in=['DRAFT', 'SUBMITTED', 'APPROVED']).count(),
        'ordered_count': PurchaseOrder.objects.filter(status='ORDERED').count(),
        'delivered_count': PurchaseOrder.objects.filter(status='COMPLETED').count(),
        'status_filter': status_filter,
        'priority_filter': priority_filter,
        'search_q': search_q,
    }
    return render(request, 'purchases/po_list.html', context)


@admin_required
def create_po_view(request):
    """Admin: Direct Purchase Order Creation form."""
    suppliers = Supplier.objects.filter(status=Supplier.SupplierStatus.ACTIVE).order_by('company_name')
    medicines = Medicine.objects.filter(status='ACTIVE').order_by('medicine_name')

    if request.method == 'POST':
        supplier_id = request.POST.get('supplier')
        priority = request.POST.get('priority', 'NORMAL')
        notes = request.POST.get('notes', '').strip()

        medicine_id = request.POST.get('medicine_id')
        quantity_str = request.POST.get('quantity', '1')

        try:
            supplier = Supplier.objects.get(pk=supplier_id)
            medicine = Medicine.objects.get(pk=medicine_id)
            quantity = int(quantity_str)
            if quantity <= 0:
                raise ValidationError("Quantity must be greater than zero.")

            # Get price from SupplierMedicine or fallback
            try:
                sm = SupplierMedicine.objects.get(supplier=supplier, medicine=medicine)
                unit_price = sm.unit_price
            except SupplierMedicine.DoesNotExist:
                unit_price = Decimal('10.00')

            po_num = PurchaseOrder.generate_po_number()
            po = PurchaseOrder.objects.create(
                po_number=po_num,
                supplier=supplier,
                priority=priority,
                status=PurchaseOrder.POStatus.APPROVED,
                order_date=timezone.now().date(),
                expected_delivery_date=timezone.now().date() + timezone.timedelta(days=supplier.average_delivery_days),
                notes=notes,
                created_by=request.user,
                approved_by=request.user,
                approved_at=timezone.now()
            )

            PurchaseOrderItem.objects.create(
                purchase_order=po,
                medicine=medicine,
                quantity_ordered=quantity,
                quantity_pending=quantity,
                unit_price=unit_price
            )
            po.calculate_totals()

            messages.success(request, f"Purchase Order {po.po_number} created and approved successfully.")
            return redirect('po_detail', pk=po.pk)

        except (Supplier.DoesNotExist, Medicine.DoesNotExist, ValidationError, Exception) as e:
            messages.error(request, str(e))

    context = {
        'suppliers': suppliers,
        'medicines': medicines,
        'priority_choices': PurchaseOrder.Priority.choices,
    }
    return render(request, 'purchases/create_po.html', context)


@admin_or_pharmacist_required
def po_detail_view(request, pk):
    """View details of a Purchase Order, line items, and delivery status timeline."""
    po = get_object_or_404(PurchaseOrder.objects.select_related('supplier', 'created_by', 'approved_by'), pk=pk)

    if request.method == 'POST' and request.user.role == 'ADMIN':
        tracking_note = request.POST.get('tracking_note', '').strip()
        confirmed_date_str = request.POST.get('confirmed_date', '').strip()
        if tracking_note or confirmed_date_str:
            timestamp = timezone.now().strftime('%Y-%m-%d %H:%M UTC')
            parts = []
            if tracking_note:
                parts.append(tracking_note)
            if confirmed_date_str:
                parts.append(f"Expected Delivery revised to: {confirmed_date_str}")
                try:
                    po.expected_delivery_date = timezone.datetime.strptime(confirmed_date_str, '%Y-%m-%d').date()
                except ValueError:
                    pass
            entry = f"\n[TRACKING UPDATE {timestamp} by {request.user.username}]: " + " | ".join(parts)
            po.notes = (po.notes or '') + entry
            po.save(update_fields=['notes', 'expected_delivery_date'])
            messages.success(request, "Delivery tracking information updated.")
            return redirect('po_detail', pk=po.pk)

    items = po.items.select_related('medicine').all()
    grns = po.goods_receipts.select_related('received_by', 'verified_by').all()

    context = {
        'po': po,
        'items': items,
        'grns': grns,
    }
    return render(request, 'purchases/po_detail.html', context)


@admin_required
@require_POST
def approve_po_view(request, pk):
    """Admin: approve a draft PO without implying supplier confirmation."""
    po = get_object_or_404(PurchaseOrder, pk=pk)
    if po.status in (PurchaseOrder.POStatus.DRAFT, PurchaseOrder.POStatus.SUBMITTED):
        po.status = PurchaseOrder.POStatus.APPROVED
        po.approved_by = request.user
        po.approved_at = timezone.now()
        po.save(update_fields=['status', 'approved_by', 'approved_at'])
        messages.success(request, f"Purchase Order {po.po_number} approved. Supplier confirmation is still pending.")
    else:
        messages.info(request, f"Purchase Order {po.po_number} is not awaiting approval.")
    return redirect('po_detail', pk=po.pk)


@admin_required
@require_POST
def confirm_supplier_order_view(request, pk):
    """Record a supplier response entered by an administrator; no live supplier API is implied."""
    confirmation_reference = request.POST.get('confirmation_reference', '').strip()
    eta_value = request.POST.get('supplier_eta', '').strip()
    if not confirmation_reference:
        messages.error(request, "Enter a supplier response reference before recording confirmation.")
        return redirect('po_detail', pk=pk)

    supplier_eta = None
    if eta_value:
        try:
            supplier_eta = timezone.datetime.strptime(eta_value, '%Y-%m-%d').date()
        except ValueError:
            messages.error(request, "Enter a valid supplier ETA.")
            return redirect('po_detail', pk=pk)
        if supplier_eta < timezone.localdate():
            messages.error(request, "Supplier ETA cannot be in the past.")
            return redirect('po_detail', pk=pk)

    with transaction.atomic():
        po = get_object_or_404(PurchaseOrder.objects.select_for_update(), pk=pk)
        if po.status not in (PurchaseOrder.POStatus.APPROVED, PurchaseOrder.POStatus.ORDERED):
            messages.error(request, "Only approved, non-cancelled purchase orders can be confirmed.")
            return redirect('po_detail', pk=po.pk)

        now = timezone.now()
        po.supplier_confirmation_reference = confirmation_reference
        po.supplier_confirmed_at = now
        po.supplier_confirmed_by = request.user
        po.supplier_confirmed_eta = supplier_eta
        if supplier_eta:
            po.expected_delivery_date = supplier_eta
        po.status = PurchaseOrder.POStatus.ORDERED
        po.save(update_fields=[
            'supplier_confirmation_reference', 'supplier_confirmed_at',
            'supplier_confirmed_by', 'supplier_confirmed_eta',
            'expected_delivery_date', 'status',
        ])

    messages.success(request, f"Supplier confirmation recorded for {po.po_number}.")
    return redirect('po_detail', pk=po.pk)


@admin_required
def cancel_po_view(request, pk):
    """Admin: Cancel Purchase Order with reason without affecting stock."""
    po = get_object_or_404(PurchaseOrder, pk=pk)
    if request.method == 'POST':
        reason = request.POST.get('cancellation_reason', '').strip()
        po.status = PurchaseOrder.POStatus.CANCELLED
        po.cancelled_by = request.user
        po.cancellation_reason = reason or "Cancelled by Administrator."
        po.save()
        messages.warning(request, f"Purchase Order {po.po_number} has been cancelled.")
    return redirect('po_detail', pk=po.pk)


# ─────────────────────────────────────────────────────────────────────────────
# 3. GOODS RECEIVING & VERIFICATION
# ─────────────────────────────────────────────────────────────────────────────

@admin_or_pharmacist_required
def grn_list_view(request):
    """List Goods Receipt Notes."""
    grns = GoodsReceipt.objects.select_related('purchase_order', 'supplier', 'received_by', 'verified_by').order_by('-created_at')

    context = {
        'grns': grns,
        'pending_count': grns.filter(status='PENDING_VERIFICATION').count(),
        'verified_count': grns.filter(status='VERIFIED').count(),
    }
    return render(request, 'purchases/grn_list.html', context)


@admin_or_pharmacist_required
def create_grn_view(request, po_pk):
    """Pharmacist view: Receive goods against a Purchase Order."""
    po = get_object_or_404(PurchaseOrder.objects.select_related('supplier'), pk=po_pk)

    if not po.is_receivable:
        messages.error(request, f"Purchase Order {po.po_number} is not in a receivable state.")
        return redirect('po_detail', pk=po.pk)

    items = po.items.select_related('medicine').filter(quantity_pending__gt=0)

    if request.method == 'POST':
        invoice_number = request.POST.get('invoice_number', '').strip()
        invoice_amount_str = request.POST.get('invoice_amount', '0')
        notes = request.POST.get('notes', '').strip()

        try:
            grn_num = GoodsReceipt.generate_grn_number()
            grn = GoodsReceipt.objects.create(
                grn_number=grn_num,
                purchase_order=po,
                supplier=po.supplier,
                receipt_date=timezone.now().date(),
                invoice_number=invoice_number,
                invoice_amount=Decimal(invoice_amount_str),
                status=GoodsReceipt.GRNStatus.PENDING_VERIFICATION,
                notes=notes,
                received_by=request.user
            )

            # Process line items from POST form
            for item in items:
                rec_qty_str = request.POST.get(f'qty_{item.id}', '0')
                batch_no = request.POST.get(f'batch_{item.id}', '').strip()
                mfg_date = request.POST.get(f'mfg_{item.id}') or None
                exp_date = request.POST.get(f'exp_{item.id}')

                rec_qty = int(rec_qty_str)
                if rec_qty > 0 and batch_no and exp_date:
                    GoodsReceiptItem.objects.create(
                        goods_receipt=grn,
                        po_item=item,
                        medicine=item.medicine,
                        quantity_received=rec_qty,
                        quantity_accepted=rec_qty,
                        batch_number=batch_no,
                        manufactured_date=mfg_date,
                        expiry_date=exp_date,
                        unit_price=item.unit_price
                    )

            messages.success(request, f"Goods Receipt Note {grn.grn_number} recorded. Please proceed to physical verification.")
            return redirect('grn_detail', pk=grn.pk)

        except Exception as e:
            messages.error(request, f"Error receiving goods: {e}")

    context = {
        'po': po,
        'items': items,
    }
    return render(request, 'purchases/create_grn.html', context)


@admin_or_pharmacist_required
def grn_detail_view(request, pk):
    """View Goods Receipt Note details and physical verification workspace."""
    grn = get_object_or_404(GoodsReceipt.objects.select_related('purchase_order', 'supplier', 'received_by', 'verified_by'), pk=pk)
    items = grn.items.select_related('medicine', 'po_item').all()

    context = {
        'grn': grn,
        'items': items,
    }
    return render(request, 'purchases/grn_detail.html', context)


@admin_or_pharmacist_required
def verify_grn_view(request, pk):
    """
    Pharmacist view: Perform physical & barcode verification of Goods Receipt.
    Executes atomic transaction via services.process_grn_receipt().
    Increases batch stock, records PURCHASE stock transaction, resolves alerts.
    """
    grn = get_object_or_404(GoodsReceipt, pk=pk)

    if request.method == 'POST':
        try:
            # Update item quantities accepted/rejected if modified in verification form
            for grn_item in grn.items.all():
                acc_str = request.POST.get(f'accepted_{grn_item.id}', str(grn_item.quantity_accepted))
                rej_reason = request.POST.get(f'reason_{grn_item.id}', '').strip()
                grn_item.quantity_accepted = int(acc_str)
                grn_item.quantity_rejected = grn_item.quantity_received - grn_item.quantity_accepted
                grn_item.rejection_reason = rej_reason
                grn_item.save()

            services.process_grn_receipt(grn_id=grn.pk, verified_by=request.user)
            messages.success(request, f"Goods Receipt {grn.grn_number} VERIFIED successfully. Inventory updated and batches created.")
            return redirect('grn_detail', pk=grn.pk)

        except (ValidationError, Exception) as e:
            messages.error(request, str(e))
            return redirect('grn_detail', pk=grn.pk)

    return redirect('grn_detail', pk=grn.pk)


# ─────────────────────────────────────────────────────────────────────────────
# 4. BARCODE / QR LOOKUP  (identification only — zero stock mutation)
# ─────────────────────────────────────────────────────────────────────────────

@admin_or_pharmacist_required
def grn_barcode_lookup_api(request):
    """
    AJAX GET endpoint for GRN barcode / medicine-name lookup.
    Returns JSON with medicine details. NEVER modifies stock.

    Query params:
      ?barcode=<value>        — exact match on Medicine.barcode
      ?medicine_name=<value>  — partial case-insensitive match on medicine_name
    """
    barcode = request.GET.get('barcode', '').strip()
    name_q = request.GET.get('medicine_name', '').strip()

    if not barcode and not name_q:
        return JsonResponse({'error': 'Provide barcode or medicine_name'}, status=400)

    try:
        if barcode:
            med = Medicine.objects.select_related('inventory').get(
                barcode=barcode, status='ACTIVE'
            )
        else:
            med = Medicine.objects.select_related('inventory').filter(
                medicine_name__icontains=name_q, status='ACTIVE'
            ).first()
            if not med:
                return JsonResponse({'error': 'No matching active medicine found'}, status=404)

        inv = getattr(med, 'inventory', None)
        return JsonResponse({
            'id': med.pk,
            'medicine_name': med.medicine_name,
            'generic_name': med.generic_name,
            'strength': med.strength,
            'dosage_form': med.dosage_form,
            'manufacturer': med.manufacturer,
            'regulatory_category': med.regulatory_category,
            'current_stock': inv.current_stock if inv else 0,
            'reorder_level': inv.reorder_level if inv else med.reorder_level,
        })
    except Medicine.DoesNotExist:
        return JsonResponse({'error': 'Medicine not found or inactive'}, status=404)


# ─────────────────────────────────────────────────────────────────────────────
# 5. PO EMAIL DISPATCH  (Admin only — records outcome in PO.notes)
# ─────────────────────────────────────────────────────────────────────────────

@admin_required
def send_po_email_view(request, pk):
    """
    Admin view: Dispatch a Purchase Order to the supplier via email.
    POST only. Tracks email transmission separately from supplier confirmation.
    """
    po = get_object_or_404(PurchaseOrder, pk=pk)

    if request.method != 'POST':
        return redirect('po_detail', pk=po.pk)

    if po.status not in (PurchaseOrder.POStatus.APPROVED, PurchaseOrder.POStatus.ORDERED):
        messages.error(request, "Purchase Order must be APPROVED before dispatching to supplier.")
        return redirect('po_detail', pk=po.pk)

    supplier = po.supplier
    if not supplier.email:
        messages.error(request, f"Supplier {supplier.company_name} has no email address on record.")
        return redirect('po_detail', pk=po.pk)

    # Build email body from PO items
    items = po.items.select_related('medicine').all()
    item_lines = "\n".join(
        f"  • {item.medicine.medicine_name} ({item.medicine.strength})"
        f" — Qty: {item.quantity_ordered} × ₹{item.unit_price} = ₹{item.total_price}"
        for item in items
    )
    subject = f"[Sanalogx] Purchase Order {po.po_number} from {request.user.get_full_name() or request.user.username}"
    body = (
        f"Dear {supplier.contact_person or supplier.company_name},\n\n"
        f"Please find the details of Purchase Order {po.po_number} below.\n\n"
        f"PO Number   : {po.po_number}\n"
        f"Order Date  : {po.order_date or timezone.now().date()}\n"
        f"Expected By : {po.expected_delivery_date or 'TBD'}\n"
        f"Priority    : {po.get_priority_display()}\n\n"
        f"Items Ordered:\n{item_lines}\n\n"
        f"Total Amount: ₹{po.total_amount}\n\n"
        f"Please confirm receipt of this order and advise expected dispatch date.\n\n"
        f"Regards,\nSanalogx Pharmacy System\n{request.user.get_full_name() or request.user.username}"
    )

    from accounts.email_service import get_from_email
    from_email = get_from_email()

    try:
        sent_count = send_mail(
            subject=subject,
            message=body,
            from_email=from_email,
            recipient_list=[supplier.email],
            fail_silently=False,
        )
        if sent_count < 1:
            raise RuntimeError("Email backend did not accept the message.")

        sent_at = timezone.now()
        transmission_note = (
            f"\n[EMAIL SENT {sent_at.strftime('%Y-%m-%d %H:%M')} UTC] "
            f"PO dispatched to {supplier.email} by {request.user.username}."
        )
        po.notes = (po.notes or '') + transmission_note
        po.email_status = PurchaseOrder.EmailStatus.SENT
        po.email_sent_at = sent_at
        po.status = PurchaseOrder.POStatus.ORDERED
        po.save(update_fields=['notes', 'email_status', 'email_sent_at', 'status'])
        messages.success(request, f"Purchase Order {po.po_number} emailed to {supplier.email}. Supplier confirmation remains pending.")

    except Exception:
        failure_note = (
            f"\n[EMAIL FAILED {timezone.now().strftime('%Y-%m-%d %H:%M')} UTC] "
            f"Attempt by {request.user.username} to {supplier.email}."
        )
        po.notes = (po.notes or '') + failure_note
        po.email_status = PurchaseOrder.EmailStatus.FAILED
        po.save(update_fields=['notes', 'email_status'])
        messages.error(request, f"Email delivery to {supplier.email} failed. Failure recorded in PO history.")

    return redirect('po_detail', pk=po.pk)


# ─────────────────────────────────────────────────────────────────────────────
# 6. DELIVERY TRACKING  (in-flight PO status dashboard)
# ─────────────────────────────────────────────────────────────────────────────

@admin_or_pharmacist_required
def delivery_tracking_view(request):
    """
    Dashboard view: in-flight Purchase Orders with delivery metrics.
    Displays PO number, supplier, dates, ordered/received/outstanding quantities,
    delay indicators, discrepancy counts, and direct 'Receive Goods' links.
    """
    today = timezone.now().date()

    in_flight_pos = PurchaseOrder.objects.filter(
        status__in=[
            PurchaseOrder.POStatus.APPROVED,
            PurchaseOrder.POStatus.ORDERED,
            PurchaseOrder.POStatus.PARTIAL_RECEIPT,
        ]
    ).select_related('supplier', 'created_by').prefetch_related(
        'items__medicine', 'goods_receipts__items'
    ).order_by('expected_delivery_date')

    tracking_data = []
    for po in in_flight_pos:
        total_ordered = sum(item.quantity_ordered for item in po.items.all())
        total_received = sum(
            grn_item.quantity_accepted
            for grn in po.goods_receipts.all()
            for grn_item in grn.items.all()
        )
        total_rejected = sum(
            grn_item.quantity_rejected
            for grn in po.goods_receipts.all()
            for grn_item in grn.items.all()
        )
        outstanding = max(total_ordered - total_received, 0)

        # Delay calculation
        is_overdue = False
        days_overdue = 0
        if po.expected_delivery_date and outstanding > 0:
            if today > po.expected_delivery_date:
                is_overdue = True
                days_overdue = (today - po.expected_delivery_date).days

        # Discrepancy count (any GRN item with rejections)
        discrepancy_count = sum(
            1 for grn in po.goods_receipts.all()
            for grn_item in grn.items.all()
            if grn_item.quantity_rejected > 0
        )

        tracking_data.append({
            'po': po,
            'total_ordered': total_ordered,
            'total_received': total_received,
            'total_rejected': total_rejected,
            'outstanding': outstanding,
            'is_overdue': is_overdue,
            'days_overdue': days_overdue,
            'discrepancy_count': discrepancy_count,
            'grn_count': po.goods_receipts.count(),
        })

    context = {
        'tracking_data': tracking_data,
        'today': today,
        'total_in_flight': len(tracking_data),
        'overdue_count': sum(1 for d in tracking_data if d['is_overdue']),
    }
    return render(request, 'purchases/delivery_tracking.html', context)

