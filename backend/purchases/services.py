"""
purchases/services.py
Phase 4 — Procurement Service Layer

Responsibilities:
- AI reorder quantity recommendation
- Best supplier scoring & recommendation
- Atomic GRN verification: batch creation → inventory increase → stock transaction
- Purchase Request → Purchase Order conversion
- Supplier performance recalculation
"""

from decimal import Decimal
from datetime import timedelta

from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone

from medicines.models import Medicine, MedicineBatch
from inventory.models import Inventory, StockTransaction, StockAlert
from inventory.services import add_stock, recalculate_inventory, generate_reorder_alerts
from suppliers.models import Supplier, SupplierMedicine, SupplierPriceHistory
from .models import (
    PurchaseRequest, PurchaseRequestItem,
    PurchaseOrder, PurchaseOrderItem,
    GoodsReceipt, GoodsReceiptItem,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. AI REORDER RECOMMENDATION
# ─────────────────────────────────────────────────────────────────────────────

def get_ai_reorder_recommendation(medicine):
    """
    Calculate recommended reorder quantity for a medicine using:
      - Current stock
      - Maximum stock (as replenishment ceiling)
      - Reorder level
      - Safety stock
      - 30-day consumption velocity (reuses existing StockTransaction data)

    Returns a dict with recommended_qty, daily_rate, days_remaining.
    """
    inv = getattr(medicine, 'inventory', None)
    if inv is None:
        return {
            'recommended_qty': medicine.maximum_stock,
            'daily_rate': 0,
            'days_remaining': 0,
            'current_stock': 0,
            'reorder_level': medicine.reorder_level,
            'maximum_stock': medicine.maximum_stock,
        }

    thirty_days_ago = timezone.now() - timedelta(days=30)
    sales_qty = StockTransaction.objects.filter(
        medicine=medicine,
        transaction_type=StockTransaction.TransactionType.SALE,
        created_at__gte=thirty_days_ago
    ).values_list('quantity', flat=True)
    total_sold_30d = sum(sales_qty) if sales_qty else 0

    daily_rate = total_sold_30d / 30.0
    current = inv.current_stock

    if daily_rate > 0:
        days_remaining = int(current / daily_rate)
        # Recommend enough to cover 60 days + safety stock buffer
        recommended = max(
            int(daily_rate * 60) + inv.safety_stock - current,
            inv.maximum_stock - current,
        )
    else:
        days_remaining = 999
        # Default: fill up to maximum stock
        recommended = max(inv.maximum_stock - current, inv.reorder_level * 2)

    return {
        'recommended_qty': max(1, recommended),
        'daily_rate': round(daily_rate, 2),
        'days_remaining': days_remaining,
        'current_stock': current,
        'reorder_level': inv.reorder_level,
        'maximum_stock': inv.maximum_stock,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2. BEST SUPPLIER RECOMMENDATION
# ─────────────────────────────────────────────────────────────────────────────

def get_supplier_comparison(medicine):
    """
    Return ranked supplier list for a medicine with composite scores.
    Composite score weights: Price 40%, Delivery 20%, Fill Rate 20%, Rating 20%.
    """
    sm_qs = SupplierMedicine.objects.filter(
        medicine=medicine,
        supplier__status=Supplier.SupplierStatus.ACTIVE,
        is_available=True
    ).select_related('supplier')

    if not sm_qs.exists():
        return []

    all_prices = [float(sm.unit_price) for sm in sm_qs]
    results = []

    for sm in sm_qs:
        score = sm.calculate_composite_score(all_prices)
        sm.composite_score = Decimal(str(score))
        sm.save(update_fields=['composite_score'])
        results.append({
            'supplier_medicine': sm,
            'supplier': sm.supplier,
            'unit_price': sm.unit_price,
            'delivery_days': sm.delivery_days,
            'rating': sm.supplier.rating,
            'composite_score': score,
            'is_available': sm.is_available,
        })

    results.sort(key=lambda x: x['composite_score'], reverse=True)
    if results:
        results[0]['is_recommended'] = True
    return results


def get_best_supplier(medicine):
    """Return the single best SupplierMedicine entry for a medicine."""
    ranked = get_supplier_comparison(medicine)
    if ranked:
        return ranked[0]['supplier_medicine']
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 3. PURCHASE REQUEST → PURCHASE ORDER CONVERSION
# ─────────────────────────────────────────────────────────────────────────────

@transaction.atomic
def convert_pr_to_po(purchase_request, admin_user, supplier_override=None,
                     expected_delivery_date=None):
    """
    Convert an APPROVED PurchaseRequest into a PurchaseOrder.
    - Uses approved_qty from each PR item (admin may have modified these)
    - Creates PO with status APPROVED
    - Updates PR status → CONVERTED_TO_PO
    - Returns the created PurchaseOrder
    """
    if purchase_request.status != PurchaseRequest.PRStatus.APPROVED:
        raise ValidationError("Only APPROVED purchase requests can be converted to Purchase Orders.")

    items = purchase_request.items.select_related('medicine', 'suggested_supplier')
    if not items.exists():
        raise ValidationError("Purchase request has no items.")

    # Determine supplier: use override if given, else use recommended_supplier
    supplier = supplier_override or purchase_request.recommended_supplier
    if not supplier:
        # Try to find one from items
        first_item = items.first()
        if first_item and first_item.suggested_supplier:
            supplier = first_item.suggested_supplier
    if not supplier:
        raise ValidationError("No supplier assigned. Please specify a supplier.")

    po_number = PurchaseOrder.generate_po_number()
    po = PurchaseOrder.objects.create(
        po_number=po_number,
        supplier=supplier,
        status=PurchaseOrder.POStatus.APPROVED,
        priority=purchase_request.priority,
        order_date=timezone.now().date(),
        expected_delivery_date=expected_delivery_date or (
            timezone.now().date() + timedelta(days=supplier.average_delivery_days)
        ),
        notes=purchase_request.notes,
        created_by=admin_user,
        approved_by=admin_user,
        approved_at=timezone.now(),
        triggered_by_alert=purchase_request.triggered_by_alert,
    )

    for item in items:
        qty = item.approved_qty if item.approved_qty > 0 else item.requested_qty
        # Get current price from SupplierMedicine catalogue, fall back to suggested price
        try:
            sm = SupplierMedicine.objects.get(supplier=supplier, medicine=item.medicine)
            unit_price = sm.unit_price
        except SupplierMedicine.DoesNotExist:
            unit_price = item.suggested_unit_price or Decimal('0.00')

        PurchaseOrderItem.objects.create(
            purchase_order=po,
            medicine=item.medicine,
            quantity_ordered=qty,
            quantity_pending=qty,
            unit_price=unit_price,
        )

    po.calculate_totals()

    # Mark PR as converted
    purchase_request.status = PurchaseRequest.PRStatus.CONVERTED_TO_PO
    purchase_request.converted_to_po = po
    purchase_request.save(update_fields=['status', 'converted_to_po'])

    return po


# ─────────────────────────────────────────────────────────────────────────────
# 4. ATOMIC GRN RECEIPT PROCESSING
# ─────────────────────────────────────────────────────────────────────────────

@transaction.atomic
def process_grn_receipt(grn_id, verified_by):
    """
    Full atomic Goods Receiving pipeline.

    For each GRNItem that is PENDING_VERIFICATION with quantity_accepted > 0:
      1. Validate expiry date is in future
      2. Create or update MedicineBatch
      3. Call inventory.services.add_stock(PURCHASE) to increase stock
      4. Update PurchaseOrderItem.quantity_received
      5. Update GRN status
      6. Update PO status (PARTIAL_RECEIPT or COMPLETED)
      7. Resolve low-stock alerts if stock now above reorder level
      8. Record supplier price history if price changed

    Raises ValidationError on any failure — entire transaction rolls back.
    """
    grn = GoodsReceipt.objects.select_for_update().get(pk=grn_id)
    po = PurchaseOrder.objects.select_for_update().get(pk=grn.purchase_order_id)

    if grn.status != GoodsReceipt.GRNStatus.PENDING_VERIFICATION:
        raise ValidationError(f"{grn.grn_number} is not pending verification.")
    if grn.supplier_id != po.supplier_id:
        raise ValidationError("Goods receipt supplier does not match the purchase order.")

    today = timezone.now().date()
    items = list(grn.items.select_for_update().select_related('medicine', 'po_item'))
    any_accepted = False

    if not items:
        raise ValidationError("Goods receipt has no line items.")

    for grn_item in items:
        if grn_item.quantity_accepted < 0 or grn_item.quantity_accepted > grn_item.quantity_received:
            raise ValidationError(
                f"Accepted quantity for {grn_item.medicine.medicine_name} must be between 0 and the delivered quantity."
            )
        if grn_item.quantity_rejected != grn_item.quantity_received - grn_item.quantity_accepted:
            raise ValidationError(
                f"Rejected quantity for {grn_item.medicine.medicine_name} does not match the delivery discrepancy."
            )
        if not grn_item.po_item_id:
            raise ValidationError("Every goods receipt line must reference a purchase order item.")

        poi = PurchaseOrderItem.objects.select_for_update().get(pk=grn_item.po_item_id)
        if poi.purchase_order_id != po.pk or poi.medicine_id != grn_item.medicine_id:
            raise ValidationError("Goods receipt line does not match its purchase order item.")

        if grn_item.quantity_accepted <= 0:
            continue  # Skip items that pharmacist chose not to accept
        if grn_item.quantity_accepted > poi.quantity_pending:
            raise ValidationError(
                f"Accepted quantity for {grn_item.medicine.medicine_name} exceeds the outstanding PO quantity."
            )

        if grn_item.manufactured_date and grn_item.manufactured_date > today:
            raise ValidationError(f"Batch {grn_item.batch_number} has a future manufacturing date.")
        if grn_item.manufactured_date and grn_item.manufactured_date > grn_item.expiry_date:
            raise ValidationError(f"Batch {grn_item.batch_number} expires before its manufacturing date.")
        if grn_item.unit_price < 0:
            raise ValidationError(f"Batch {grn_item.batch_number} has an invalid purchase price.")

        # ── Expiry validation ──────────────────────────────────────────────
        if grn_item.expiry_date <= today:
            raise ValidationError(
                f"Cannot accept batch {grn_item.batch_number} for "
                f"{grn_item.medicine.medicine_name}: batch is expired "
                f"(Expiry: {grn_item.expiry_date})."
            )

        near_expiry_threshold = today + timedelta(days=90)
        is_near_expiry = grn_item.expiry_date <= near_expiry_threshold

        # ── Batch creation / update ────────────────────────────────────────
        existing_batch = MedicineBatch.objects.select_for_update().filter(
            medicine=grn_item.medicine,
            batch_number=grn_item.batch_number
        ).first()

        if existing_batch:
            # Safety: only reuse batch if expiry matches exactly
            if existing_batch.expiry_date != grn_item.expiry_date:
                raise ValidationError(
                    f"Batch {grn_item.batch_number} already exists with a different "
                    f"expiry date ({existing_batch.expiry_date}). "
                    f"Use a unique batch number for this shipment."
                )
            if existing_batch.status in (
                MedicineBatch.BatchStatus.EXPIRED,
                MedicineBatch.BatchStatus.QUARANTINED,
                MedicineBatch.BatchStatus.RETURNED,
            ):
                raise ValidationError(
                    f"Batch {grn_item.batch_number} is not eligible to receive usable stock."
                )
            batch = existing_batch
        else:
            batch_status = (
                MedicineBatch.BatchStatus.NEAR_EXPIRY if is_near_expiry
                else MedicineBatch.BatchStatus.ACTIVE
            )
            batch = MedicineBatch.objects.create(
                medicine=grn_item.medicine,
                batch_number=grn_item.batch_number,
                manufacturing_date=grn_item.manufactured_date,
                expiry_date=grn_item.expiry_date,
                purchase_price=grn_item.unit_price,
                selling_price=grn_item.unit_price,  # Pharmacist can update later
                quantity_received=0,
                quantity_available=0,  # add_stock will increment
                received_date=today,
                status=batch_status,
            )

        # Link batch back to GRN item for audit
        grn_item.medicine_batch = batch
        grn_item.save(update_fields=['medicine_batch'])

        # ── Stock increase via inventory service ──────────────────────────
        add_stock(
            medicine=grn_item.medicine,
            batch=batch,
            quantity=grn_item.quantity_accepted,
            transaction_type=StockTransaction.TransactionType.PURCHASE,
            performed_by=verified_by,
            reference_number=grn.grn_number,
            notes=f"GRN {grn.grn_number} | PO {po.po_number} | Batch {batch.batch_number}",
        )

        # ── Update PO item received quantity ──────────────────────────────
        if grn_item.po_item:
            poi.quantity_received = (poi.quantity_received or 0) + grn_item.quantity_accepted
            poi.quantity_pending = max(0, poi.quantity_ordered - poi.quantity_received)
            poi.save(update_fields=['quantity_received', 'quantity_pending'])

        # ── Supplier price history if price changed ───────────────────────
        try:
            sm = SupplierMedicine.objects.get(
                supplier=po.supplier, medicine=grn_item.medicine
            )
            if sm.unit_price != grn_item.unit_price:
                SupplierPriceHistory.objects.create(
                    supplier=po.supplier,
                    medicine=grn_item.medicine,
                    old_price=sm.unit_price,
                    new_price=grn_item.unit_price,
                    changed_by=verified_by,
                    reason=f"Updated from GRN {grn.grn_number}",
                )
                sm.unit_price = grn_item.unit_price
                sm.save(update_fields=['unit_price'])
        except SupplierMedicine.DoesNotExist:
            pass

        any_accepted = True

    if not any_accepted:
        if not any(item.quantity_rejected > 0 for item in items):
            raise ValidationError("No delivered quantities were recorded for verification.")
        grn.status = GoodsReceipt.GRNStatus.REJECTED
        grn.verified_by = verified_by
        grn.verified_at = timezone.now()
        grn.save(update_fields=['status', 'verified_by', 'verified_at'])
        return grn

    # ── Update GRN status ─────────────────────────────────────────────────
    grn.status = GoodsReceipt.GRNStatus.VERIFIED
    grn.verified_by = verified_by
    grn.verified_at = timezone.now()
    grn.save(update_fields=['status', 'verified_by', 'verified_at'])

    # ── Update PO status ──────────────────────────────────────────────────
    po_items = po.items.all()
    all_received = all(i.quantity_pending == 0 for i in po_items)
    any_received = any(i.quantity_received > 0 for i in po_items)

    if all_received:
        po.status = PurchaseOrder.POStatus.COMPLETED
        po.actual_delivery_date = today
    elif any_received:
        po.status = PurchaseOrder.POStatus.PARTIAL_RECEIPT
    po.save(update_fields=['status', 'actual_delivery_date'])

    return grn


# ─────────────────────────────────────────────────────────────────────────────
# 5. SUPPLIER PERFORMANCE RECALCULATION
# ─────────────────────────────────────────────────────────────────────────────

def recalculate_supplier_performance(supplier):
    """
    Recalculate on_time_delivery_rate and fill_rate for a supplier
    from actual completed PurchaseOrder records.
    Returns the updated supplier instance.
    """
    completed_pos = PurchaseOrder.objects.filter(
        supplier=supplier,
        status=PurchaseOrder.POStatus.COMPLETED,
        actual_delivery_date__isnull=False,
        expected_delivery_date__isnull=False,
    )

    total = completed_pos.count()
    if total == 0:
        return supplier  # Insufficient data — leave defaults

    on_time = sum(
        1 for po in completed_pos
        if po.actual_delivery_date <= po.expected_delivery_date
    )
    on_time_rate = Decimal(str(round((on_time / total) * 100, 2)))

    # Fill rate: sum(quantity_received) / sum(quantity_ordered) across all PO items
    ordered_total = 0
    received_total = 0
    for po in completed_pos:
        for item in po.items.all():
            ordered_total += item.quantity_ordered
            received_total += item.quantity_received

    fill_rate = Decimal(str(round((received_total / ordered_total) * 100, 2))) if ordered_total else Decimal('100.00')

    supplier.on_time_delivery_rate = min(on_time_rate, Decimal('100.00'))
    supplier.fill_rate = min(fill_rate, Decimal('100.00'))
    supplier.save(update_fields=['on_time_delivery_rate', 'fill_rate'])
    return supplier
