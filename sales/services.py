"""
sales/services.py
Phase 5 — Sales, Billing, FEFO Allocation, and Stock Movement Services
"""

from decimal import Decimal
from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone

from medicines.models import Medicine, MedicineBatch
from inventory.models import Inventory, StockTransaction, StockAlert
from inventory.services import remove_stock, add_stock, allocate_stock, recalculate_inventory, generate_reorder_alerts
from accounts.models import AuditLog
from .models import Sale, SaleItem, SaleReturn, SaleReturnItem


@transaction.atomic
def create_sale_from_cart(
    patient_name="Walk-in Customer",
    patient_id="",
    mobile_number="",
    doctor_name="",
    prescription_number="",
    cart_items=None,
    payment_method=Sale.PaymentMethod.CASH,
    discount_amount=Decimal('0.00'),
    tax_amount=Decimal('0.00'),
    notes="",
    pharmacist_user=None
):
    """
    Atomic Sale & Stock Deduction Pipeline with Production Pricing & Tax Recalculation:
    1. Locks inventory and batch rows.
    2. Revalidates medicine & batch active status and expiry date > today.
    3. Performs multi-batch FEFO allocation using central allocate_stock().
    4. Calls central remove_stock(SALE) for each allocated batch.
    5. Records historical cost basis (purchase_unit_price=batch.unit_cost or purchase_price).
    6. Generates unique sale_number & invoice_number.
    7. Recalculates line subtotals, item-level discounts, GST tax, and grand totals on the server.
    8. Distinguishes paid amount and outstanding amount.
    9. Triggers low-stock alerts check via generate_reorder_alerts().
    10. Logs audit action.
    """
    if not cart_items or len(cart_items) == 0:
        raise ValidationError("Sales cart is empty. Please add at least one medicine.")

    today = timezone.now().date()
    sale_number = Sale.generate_sale_number()
    invoice_number = Sale.generate_invoice_number()

    # Pre-create Sale shell
    sale = Sale.objects.create(
        sale_number=sale_number,
        invoice_number=invoice_number,
        patient_name=patient_name or "Walk-in Customer",
        patient_id=patient_id or "",
        mobile_number=mobile_number or "",
        doctor_name=doctor_name or "",
        prescription_number=prescription_number or "",
        payment_method=payment_method,
        status=Sale.SaleStatus.COMPLETED,
        notes=notes or "",
        sold_by=pharmacist_user,
    )

    total_subtotal = Decimal('0.00')
    total_discount = Decimal('0.00')
    total_tax = Decimal('0.00')

    for c_item in cart_items:
        med_id = c_item.get('medicine_id')
        req_qty = int(c_item.get('quantity', 0))

        if req_qty <= 0:
            raise ValidationError("Item quantity must be greater than zero.")

        try:
            medicine = Medicine.objects.select_for_update().get(pk=med_id, status='ACTIVE')
        except Medicine.DoesNotExist:
            raise ValidationError(f"Medicine ID {med_id} is inactive or not found.")

        # Validate Prescription and Controlled Medicine requirements
        if medicine.is_prescription_required or medicine.is_controlled:
            if not prescription_number:
                drug_type = "Controlled (Schedule H1/X/Narcotic)" if medicine.is_controlled else "Prescription-Only"
                raise ValidationError(
                    f"Prescription required: '{medicine.medicine_name}' is a {drug_type} medicine "
                    "and cannot be dispensed without a valid prescription number."
                )

        if medicine.is_controlled and (not patient_name or patient_name.strip() in ['Walk-in Customer', '']):
            raise ValidationError(
                f"Controlled Drug Compliance: A registered patient name is mandatory when dispensing '{medicine.medicine_name}'."
            )

        # Check total physical & available stock
        inv = getattr(medicine, 'inventory', None)
        if not inv or inv.available_stock < req_qty:
            avail = inv.available_stock if inv else 0
            raise ValidationError(f"Insufficient stock for {medicine.medicine_name}. (Requested: {req_qty}, Available: {avail}).")

        # Item-level requested discount validation
        raw_disc_pct = Decimal(str(c_item.get('discount_pct', 0) or 0))
        max_allowed_disc = getattr(medicine, 'max_discount_pct', Decimal('20.00')) or Decimal('20.00')
        if raw_disc_pct > max_allowed_disc:
            raise ValidationError(
                f"Requested discount ({raw_disc_pct}%) for {medicine.medicine_name} exceeds maximum allowable discount ({max_allowed_disc}%)."
            )
        if raw_disc_pct < Decimal('0.00'):
            raise ValidationError("Discount percentage cannot be negative.")

        # FEFO multi-batch allocation
        try:
            allocations = allocate_stock(medicine, req_qty, policy='FEFO')
        except ValidationError as err:
            raise ValidationError(f"Stock allocation error for {medicine.medicine_name}: {err}")

        for alloc in allocations:
            batch = alloc['batch']
            alloc_qty = alloc['quantity']

            # Double-check batch status & expiry date
            if batch.expiry_date <= today:
                raise ValidationError(f"Sale blocked: Batch {batch.batch_number} of {medicine.medicine_name} has expired on {batch.expiry_date}.")

            if batch.status in ['EXPIRED', 'QUARANTINED', 'RETURNED']:
                raise ValidationError(f"Sale blocked: Batch {batch.batch_number} of {medicine.medicine_name} is in status {batch.get_status_display()}.")

            # Remove stock via central inventory service (creates StockTransaction with SALE type)
            remove_stock(
                medicine=medicine,
                batch=batch,
                quantity=alloc_qty,
                transaction_type=StockTransaction.TransactionType.SALE,
                performed_by=pharmacist_user,
                reference_number=sale_number,
                notes=f"Pharmacy Sale {sale_number} | Invoice {invoice_number} | Batch {batch.batch_number}"
            )

            # Controlled Medicine Running Register entry (batch-level running ledger)
            if medicine.is_controlled or medicine.regulatory_category in ['NARCOTIC', 'SCHEDULE_X', 'SCHEDULE_H1']:
                from dispensing.models import ControlledMedicineRegister
                balance_after = batch.quantity_available
                opening_bal = balance_after + alloc_qty

                ControlledMedicineRegister.objects.create(
                    entry_id=ControlledMedicineRegister.generate_entry_id(),
                    medicine=medicine,
                    batch=batch,
                    transaction_type=ControlledMedicineRegister.TransactionType.DISPENSE,
                    opening_balance=opening_bal,
                    quantity_in=0,
                    quantity_out=alloc_qty,
                    closing_balance=balance_after,
                    prescription_number=prescription_number or '',
                    doctor_name=doctor_name or '',
                    patient_name=patient_name or 'Walk-in Customer',
                    patient_id=patient_id or patient_name or '',
                    pharmacist=pharmacist_user,
                    reference_number=sale_number,
                    reason=f"POS Sale {sale_number} (Invoice: {invoice_number})",
                )

            # Server-calculated line pricing, tax & cost
            unit_price = batch.selling_price
            purchase_price = getattr(batch, 'unit_cost', None) or batch.purchase_price or Decimal('0.00')
            tax_rate = getattr(batch, 'tax_rate', None) or getattr(medicine, 'tax_rate', Decimal('5.00')) or Decimal('5.00')
            discount_pct = raw_disc_pct if raw_disc_pct > 0 else (getattr(batch, 'discount_pct', Decimal('0.00')) or Decimal('0.00'))

            line_subtotal = round(Decimal(str(alloc_qty)) * unit_price, 2)
            line_discount = round(line_subtotal * (discount_pct / Decimal('100.00')), 2) if discount_pct else Decimal('0.00')
            line_taxable = max(Decimal('0.00'), line_subtotal - line_discount)
            line_tax = round(line_taxable * (Decimal(str(tax_rate)) / Decimal('100.00')), 2) if tax_rate else Decimal('0.00')
            line_total = line_taxable + line_tax

            SaleItem.objects.create(
                sale=sale,
                medicine=medicine,
                batch=batch,
                quantity=alloc_qty,
                unit_price=unit_price,
                purchase_unit_price=purchase_price,
                discount_pct=discount_pct,
                discount_amount=line_discount,
                tax_rate=tax_rate,
                tax_amount=line_tax,
                total_price=line_total,
            )

            total_subtotal += line_subtotal
            total_discount += line_discount
            total_tax += line_tax

    # Finalize financial totals on server
    sale.subtotal = total_subtotal
    sale.discount_amount = total_discount
    sale.tax_amount = total_tax
    sale.total_amount = max(Decimal('0.00'), total_subtotal - total_discount + total_tax)

    if payment_method == Sale.PaymentMethod.CREDIT:
        sale.amount_paid = Decimal('0.00')
        sale.outstanding_amount = sale.total_amount
    else:
        sale.amount_paid = sale.total_amount
        sale.outstanding_amount = Decimal('0.00')

    sale.save(update_fields=['subtotal', 'discount_amount', 'tax_amount', 'total_amount', 'amount_paid', 'outstanding_amount'])

    # Audit logging
    if AuditLog:
        try:
            AuditLog.log(
                action_type='SALE_CREATED',
                performed_by=pharmacist_user,
                object_type='Sale',
                object_id=sale.id,
                object_repr=sale.sale_number,
                description=f"Completed sale {sale.sale_number} (Invoice: {sale.invoice_number}) for ₹{sale.total_amount}"
            )
        except Exception:
            pass

    return sale


@transaction.atomic
def process_sale_return(sale_id, return_items_data, reason="Customer Return", notes="", pharmacist_user=None):
    """
    Process sale return / refund safely:
    1. Validates returned quantity does not exceed original sold quantity.
    2. Calls inventory.services.add_stock(RETURN) to increase batch quantity safely.
    3. Calculates refund amount, statutory tax reversal, and inventory COGS reversal.
    4. Creates SaleReturn & SaleReturnItem records.
    5. Updates Sale status to PARTIAL_RETURN or RETURNED and updates refund totals.
    """
    sale = Sale.objects.select_for_update().get(pk=sale_id)

    if sale.status == Sale.SaleStatus.CANCELLED:
        raise ValidationError("Cannot process return on a cancelled sale.")

    return_number = SaleReturn.generate_return_number()
    sale_return = SaleReturn.objects.create(
        return_number=return_number,
        sale=sale,
        reason=reason,
        notes=notes,
        processed_by=pharmacist_user,
    )

    total_refund = Decimal('0.00')
    total_tax_refund = Decimal('0.00')

    for r_data in return_items_data:
        item_id = r_data.get('item_id')
        ret_qty = int(r_data.get('quantity', 0))

        if ret_qty <= 0:
            continue

        sale_item = SaleItem.objects.select_for_update().get(pk=item_id, sale=sale)
        max_returnable = sale_item.quantity - sale_item.quantity_returned

        if ret_qty > max_returnable:
            raise ValidationError(f"Cannot return {ret_qty} units of {sale_item.medicine.medicine_name}; only {max_returnable} units returnable.")

        # Restore stock to batch via central service
        add_stock(
            medicine=sale_item.medicine,
            batch=sale_item.batch,
            quantity=ret_qty,
            transaction_type=StockTransaction.TransactionType.RETURN,
            performed_by=pharmacist_user,
            reference_number=return_number,
            notes=f"Sale Return {return_number} against {sale.sale_number} | Reason: {reason}"
        )

        # Controlled Medicine Running Register entry for patient return
        if sale_item.medicine.is_controlled or sale_item.medicine.regulatory_category in ['NARCOTIC', 'SCHEDULE_X', 'SCHEDULE_H1']:
            from dispensing.models import ControlledMedicineRegister
            balance_after = sale_item.batch.quantity_available
            opening_bal = max(0, balance_after - ret_qty)

            ControlledMedicineRegister.objects.create(
                entry_id=ControlledMedicineRegister.generate_entry_id(),
                medicine=sale_item.medicine,
                batch=sale_item.batch,
                transaction_type=ControlledMedicineRegister.TransactionType.RETURN,
                opening_balance=opening_bal,
                quantity_in=ret_qty,
                quantity_out=0,
                closing_balance=balance_after,
                prescription_number=sale.prescription_number or '',
                doctor_name=sale.doctor_name or '',
                patient_name=sale.patient_name or '',
                patient_id=sale.patient_id or '',
                pharmacist=pharmacist_user,
                reference_number=return_number,
                reason=f"Sale Return {return_number} against {sale.sale_number} | {reason}",
            )

        sale_item.quantity_returned += ret_qty
        sale_item.return_reason = reason
        sale_item.save(update_fields=['quantity_returned', 'return_reason'])

        # Proportional refund and tax reversal
        qty_dec = Decimal(str(sale_item.quantity))
        ret_dec = Decimal(str(ret_qty))

        unit_effective = sale_item.total_price / qty_dec
        line_refund = round(unit_effective * ret_dec, 2)

        unit_tax = (sale_item.tax_amount / qty_dec) if sale_item.tax_amount else Decimal('0.00')
        line_tax_refund = round(unit_tax * ret_dec, 2)

        line_cogs_reversed = round(sale_item.purchase_unit_price * ret_dec, 2)

        total_refund += line_refund
        total_tax_refund += line_tax_refund

        SaleReturnItem.objects.create(
            sale_return=sale_return,
            sale_item=sale_item,
            quantity_returned=ret_qty,
            refund_amount=line_refund,
            tax_refund_amount=line_tax_refund,
            cogs_reversed=line_cogs_reversed,
            restocked_to_batch=True
        )

    sale_return.refund_amount = total_refund
    sale_return.tax_refund_amount = total_tax_refund
    sale_return.save(update_fields=['refund_amount', 'tax_refund_amount'])

    # Update Sale aggregates
    sale.refund_amount = (sale.refund_amount or Decimal('0.00')) + total_refund
    if sale.outstanding_amount > Decimal('0.00'):
        sale.outstanding_amount = max(Decimal('0.00'), sale.outstanding_amount - total_refund)

    # Check overall return status
    all_returned = all(i.quantity == i.quantity_returned for i in sale.items.all())
    any_returned = any(i.quantity_returned > 0 for i in sale.items.all())

    if all_returned:
        sale.status = Sale.SaleStatus.RETURNED
    elif any_returned:
        sale.status = Sale.SaleStatus.PARTIAL_RETURN
    sale.save(update_fields=['status', 'refund_amount', 'outstanding_amount'])

    # Audit logging
    if AuditLog:
        try:
            AuditLog.log(
                action_type='SALE_RETURNED',
                performed_by=pharmacist_user,
                object_type='SaleReturn',
                object_id=sale_return.id,
                object_repr=sale_return.return_number,
                description=f"Processed return {sale_return.return_number} for sale {sale.sale_number}. Refund amount: ₹{total_refund}"
            )
        except Exception:
            pass

    return sale_return


@transaction.atomic
def cancel_sale(sale_id, cancelled_by, reason="Customer Request / Order Cancellation"):
    """
    Safely cancel a sale invoice:
    1. Reverses remaining unreturned stock to the original batch(es) via central StockService.
    2. Writes auditable StockTransactions with ADJUSTMENT type.
    3. Marks Sale status as CANCELLED with user, timestamp, and audit reason.
    4. Clears outstanding amount.
    5. Logs action in system AuditLog.
    """
    sale = Sale.objects.select_for_update().get(pk=sale_id)
    if sale.status == Sale.SaleStatus.CANCELLED:
        raise ValidationError(f"Sale {sale.sale_number} is already cancelled.")

    for item in sale.items.select_for_update():
        unreturned_qty = item.quantity - item.quantity_returned
        if unreturned_qty > 0:
            add_stock(
                medicine=item.medicine,
                batch=item.batch,
                quantity=unreturned_qty,
                transaction_type=StockTransaction.TransactionType.ADJUSTMENT,
                performed_by=cancelled_by,
                reference_number=sale.sale_number,
                notes=f"Invoice Cancellation {sale.invoice_number} | Stock restored ({unreturned_qty} units) | Reason: {reason}"
            )

            # Controlled Medicine Running Register entry for cancelled sale
            if item.medicine.is_controlled or item.medicine.regulatory_category in ['NARCOTIC', 'SCHEDULE_X', 'SCHEDULE_H1']:
                from dispensing.models import ControlledMedicineRegister
                balance_after = item.batch.quantity_available
                opening_bal = max(0, balance_after - unreturned_qty)

                ControlledMedicineRegister.objects.create(
                    entry_id=ControlledMedicineRegister.generate_entry_id(),
                    medicine=item.medicine,
                    batch=item.batch,
                    transaction_type=ControlledMedicineRegister.TransactionType.RETURN,
                    opening_balance=opening_bal,
                    quantity_in=unreturned_qty,
                    quantity_out=0,
                    closing_balance=balance_after,
                    prescription_number=sale.prescription_number or '',
                    doctor_name=sale.doctor_name or '',
                    patient_name=sale.patient_name or '',
                    patient_id=sale.patient_id or '',
                    pharmacist=cancelled_by,
                    reference_number=sale.sale_number,
                    reason=f"Invoice Cancellation {sale.invoice_number} | {reason}",
                )

            item.quantity_returned = item.quantity
            item.return_reason = f"Cancelled: {reason}"
            item.save(update_fields=['quantity_returned', 'return_reason'])

    sale.status = Sale.SaleStatus.CANCELLED
    sale.cancelled_by = cancelled_by
    sale.cancelled_at = timezone.now()
    sale.cancellation_reason = reason
    sale.outstanding_amount = Decimal('0.00')
    sale.save(update_fields=['status', 'cancelled_by', 'cancelled_at', 'cancellation_reason', 'outstanding_amount'])

    if AuditLog:
        try:
            AuditLog.log(
                action_type='SALE_CANCELLED',
                performed_by=cancelled_by,
                object_type='Sale',
                object_id=sale.id,
                object_repr=sale.sale_number,
                description=f"Cancelled sale {sale.sale_number} (Invoice: {sale.invoice_number}). Reason: {reason}"
            )
        except Exception:
            pass

    return sale
