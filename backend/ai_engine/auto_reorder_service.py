"""
ai_engine/auto_reorder_service.py
Phase 6 + Procurement Automation — AI Auto-Reorder Service Layer

Responsibilities:
  1. Calculate accurate usable stock (strictly excluding expired, quarantined, and returned batches).
  2. Compute exact reorder shortfall using 30-day demand forecast, safety stock, and supplier lead time.
  3. Deduct confirmed incoming stock (open approved Purchase Orders) to avoid over-ordering.
  4. Select the best active supplier using existing composite intelligence (price, delivery, rating, availability).
  5. Apply packaging constraints (pack size, minimum order quantity) and maximum stock capacity limits.
  6. Create or update PurchaseRequest in PENDING_APPROVAL status (preserving regulatory & Admin approvals).
  7. Prevent duplicate orders across repeated runs, dashboard refreshes, and background executions.
  8. Never mutate inventory stock on request creation — stock increases ONLY upon physical GRN verification.
"""

import math
import logging
from decimal import Decimal
from datetime import timedelta

from django.db import transaction
from django.db.models import Sum, Q
from django.utils import timezone
from django.core.cache import cache

from medicines.models import Medicine, MedicineBatch
from inventory.models import Inventory, StockAlert
from suppliers.models import Supplier, SupplierMedicine
from purchases.models import (
    PurchaseRequest, PurchaseRequestItem,
    PurchaseOrder, PurchaseOrderItem,
)

logger = logging.getLogger(__name__)

# Distributed lock settings for background automation
AUTO_REORDER_LOCK_KEY = "sanalogx_auto_reorder_running"
AUTO_REORDER_LOCK_TIMEOUT = 300  # 5 minutes safety timeout

# Purchase Order statuses that represent confirmed incoming procurement commitments
CONFIRMED_INCOMING_PO_STATUSES = (
    PurchaseOrder.POStatus.APPROVED,
    PurchaseOrder.POStatus.ORDERED,
    PurchaseOrder.POStatus.PARTIAL_RECEIPT,
)

# Active Purchase Request statuses that represent pending procurement requests
ACTIVE_PR_STATUSES = (
    PurchaseRequest.PRStatus.DRAFT,
    PurchaseRequest.PRStatus.PENDING_APPROVAL,
    PurchaseRequest.PRStatus.APPROVED,
)

# All open PO statuses (including drafts) for duplicate detection
ALL_OPEN_PO_STATUSES = (
    PurchaseOrder.POStatus.DRAFT,
    PurchaseOrder.POStatus.SUBMITTED,
    PurchaseOrder.POStatus.APPROVED,
    PurchaseOrder.POStatus.ORDERED,
    PurchaseOrder.POStatus.PARTIAL_RECEIPT,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. STOCK CALCULATION & ORDER TRACKING HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_usable_stock(medicine, as_of_date=None) -> int:
    """
    Calculate current usable stock for a medicine from valid, available batches.
    Strictly excludes:
      - Expired batches (expiry_date <= today or status == EXPIRED)
      - Quarantined batches (status == QUARANTINED)
      - Returned batches (status == RETURNED)
      - Depleted batches (quantity_available <= 0)
    Respects existing FEFO policy.
    """
    today = as_of_date or timezone.localdate()

    if medicine.batches.exists():
        total = medicine.batches.filter(
            status__in=(MedicineBatch.BatchStatus.ACTIVE, MedicineBatch.BatchStatus.NEAR_EXPIRY),
            expiry_date__gt=today,
            quantity_available__gt=0,
        ).exclude(
            status__in=[
                MedicineBatch.BatchStatus.EXPIRED,
                MedicineBatch.BatchStatus.QUARANTINED,
                MedicineBatch.BatchStatus.RETURNED,
            ]
        ).aggregate(total=Sum('quantity_available'))['total']
        return total or 0

    inv = getattr(medicine, 'inventory', None)
    if inv:
        return inv.current_stock or 0
    return 0


def get_confirmed_incoming_po_qty(medicine) -> int:
    """
    Returns total quantity of `medicine` pending delivery across confirmed
    (approved/ordered/partially received) Purchase Orders.
    """
    try:
        pending = PurchaseOrderItem.objects.filter(
            medicine=medicine,
            purchase_order__status__in=CONFIRMED_INCOMING_PO_STATUSES,
        ).aggregate(s=Sum('quantity_pending'))['s'] or 0
        return pending
    except Exception as e:
        logger.warning(f"Error checking incoming PO qty for {medicine}: {e}")
        return 0


def get_pending_pr_qty(medicine) -> int:
    """
    Returns total quantity of `medicine` requested in active (unconverted)
    Purchase Requests (DRAFT, PENDING_APPROVAL, APPROVED).
    """
    try:
        pending = PurchaseRequestItem.objects.filter(
            medicine=medicine,
            purchase_request__status__in=ACTIVE_PR_STATUSES,
        ).aggregate(s=Sum('requested_qty'))['s'] or 0
        return pending
    except Exception as e:
        logger.warning(f"Error checking pending PR qty for {medicine}: {e}")
        return 0


def get_existing_active_order_for_medicine(medicine):
    """
    Checks whether an equivalent pending or open procurement request/order already
    exists for this medicine to prevent duplicate orders.
    Returns dict with details or None.
    """
    # 1. Check active Purchase Requests
    pr_item = PurchaseRequestItem.objects.filter(
        medicine=medicine,
        purchase_request__status__in=ACTIVE_PR_STATUSES,
    ).select_related('purchase_request').order_by('-purchase_request__created_at').first()

    if pr_item:
        return {
            'order_type': 'PURCHASE_REQUEST',
            'order_id': pr_item.purchase_request.id,
            'reference': pr_item.purchase_request.request_id,
            'status': pr_item.purchase_request.status,
            'status_display': pr_item.purchase_request.get_status_display(),
            'quantity': pr_item.requested_qty,
            'created_at': pr_item.purchase_request.created_at,
            'instance': pr_item.purchase_request,
            'item_instance': pr_item,
        }

    # 2. Check open Purchase Orders with pending delivery
    po_item = PurchaseOrderItem.objects.filter(
        medicine=medicine,
        purchase_order__status__in=ALL_OPEN_PO_STATUSES,
        quantity_pending__gt=0,
    ).select_related('purchase_order', 'purchase_order__supplier').order_by('-purchase_order__created_at').first()

    if po_item:
        return {
            'order_type': 'PURCHASE_ORDER',
            'order_id': po_item.purchase_order.id,
            'reference': po_item.purchase_order.po_number,
            'status': po_item.purchase_order.status,
            'status_display': po_item.purchase_order.get_status_display(),
            'quantity': po_item.quantity_pending,
            'created_at': po_item.purchase_order.created_at,
            'supplier_name': po_item.purchase_order.supplier.company_name if po_item.purchase_order.supplier else '',
            'instance': po_item.purchase_order,
            'item_instance': po_item,
        }

    return None


# ─────────────────────────────────────────────────────────────────────────────
# 2. SUPPLIER MATCHING & EVALUATION
# ─────────────────────────────────────────────────────────────────────────────

def get_best_active_supplier(medicine):
    """
    Selects the single best active supplier for a medicine using existing
    composite scoring (price 40%, delivery 20%, fill rate 20%, rating 20%).
    Returns (Supplier, SupplierMedicine, unit_price) or (None, None, Decimal('0.00')).
    Safely handles inactive suppliers, missing catalog entries, and non-configured suppliers.
    """
    from purchases.services import get_best_supplier

    best_sm = get_best_supplier(medicine)
    if best_sm and best_sm.supplier and best_sm.supplier.status == Supplier.SupplierStatus.ACTIVE:
        return best_sm.supplier, best_sm, best_sm.unit_price

    # Fallback: check any active SupplierMedicine for this medicine
    fallback_sm = SupplierMedicine.objects.filter(
        medicine=medicine,
        supplier__status=Supplier.SupplierStatus.ACTIVE,
        is_available=True,
    ).select_related('supplier').order_by('unit_price').first()

    if fallback_sm:
        return fallback_sm.supplier, fallback_sm, fallback_sm.unit_price

    return None, None, Decimal('0.00')


# ─────────────────────────────────────────────────────────────────────────────
# 3. REORDER QUANTITY CALCULATION
# ─────────────────────────────────────────────────────────────────────────────

def calculate_auto_reorder_details(medicine):
    """
    Calculates complete reorder requirement details for a medicine:
      - Usable stock (excluding expired, quarantined, returned)
      - 30-day forecast demand
      - Safety stock and supplier lead-time demand
      - Existing minimum and maximum stock limits
      - Confirmed incoming stock (open approved POs)
      - Pack-size and minimum order quantity constraints
    Returns structured dict with calculation breakdown.
    """
    from ai_engine.intelligence_service import get_demand_forecast

    inv = getattr(medicine, 'inventory', None)

    # 1. Usable Stock
    usable_stock = get_usable_stock(medicine)

    # 2. Stock Thresholds from Inventory / Medicine configuration
    reorder_level = inv.reorder_level if (inv and inv.reorder_level is not None) else medicine.reorder_level
    if reorder_level is None or reorder_level <= 0:
        reorder_level = medicine.reorder_level or 50

    safety_stock = inv.safety_stock if (inv and inv.safety_stock is not None) else medicine.safety_stock
    if safety_stock is None:
        safety_stock = medicine.safety_stock or 20

    max_stock = inv.maximum_stock if (inv and inv.maximum_stock is not None) else medicine.maximum_stock
    if max_stock is None:
        max_stock = medicine.maximum_stock or 500

    min_stock = inv.minimum_stock if (inv and inv.minimum_stock is not None) else medicine.minimum_stock
    if min_stock is None:
        min_stock = medicine.minimum_stock or 10

    # 3. Forecast Demand
    forecast = get_demand_forecast(medicine)
    forecast_30d = forecast.get('forecast_30d', 0)
    daily_rate = forecast.get('daily_rate') if forecast.get('daily_rate') is not None else round(forecast_30d / 30.0, 2)

    # 4. Confirmed Incoming Stock (open approved Purchase Orders)
    open_po_qty = get_confirmed_incoming_po_qty(medicine)
    effective_available = usable_stock + open_po_qty

    # 5. Pending Purchase Requests (unapproved/draft requests)
    pending_pr_qty = get_pending_pr_qty(medicine)

    # 6. Supplier & Lead Time
    supplier, sm, unit_price = get_best_active_supplier(medicine)
    lead_days = supplier.average_delivery_days if supplier else 7
    lead_time_demand = int(round(daily_rate * lead_days))

    # 7. Low-Stock Detection
    # Triggered when usable stock reaches or falls below the medicine's reorder level,
    # or when effective available stock will not cover 30-day forecast demand + safety stock.
    is_at_or_below_reorder_level = usable_stock <= reorder_level
    needs_reorder = (
        is_at_or_below_reorder_level
        or (effective_available <= reorder_level)
        or ((effective_available - forecast_30d) < safety_stock)
    )

    # 8. Reorder Quantity Calculation
    # Target stock level covers 30-day forecast demand plus safety stock buffer and lead-time demand
    target_stock = max(
        forecast_30d + safety_stock,
        reorder_level + safety_stock,
        lead_time_demand + safety_stock,
    )

    # Net shortfall subtracts both usable stock and confirmed incoming PO stock
    net_shortfall = max(0, target_stock - effective_available)

    # Special handling for zero stock: ensure immediate replenishment
    if usable_stock == 0:
        zero_stock_target = max(target_stock, reorder_level + safety_stock)
        net_shortfall = max(net_shortfall, max(0, zero_stock_target - open_po_qty))

    # Respect maximum stock limit
    if max_stock and max_stock > 0:
        remaining_capacity = max(0, max_stock - effective_available)
        calculated_qty = min(net_shortfall, remaining_capacity)
    else:
        calculated_qty = net_shortfall

    # If stock is genuinely below reorder level and net shortfall > 0, ensure at least 1 unit
    if needs_reorder and net_shortfall > 0 and calculated_qty == 0 and usable_stock <= reorder_level:
        calculated_qty = max(1, min(net_shortfall, reorder_level))

    # 9. Pack-size and Minimum Order Quantity (MOQ) constraints
    pack_size = getattr(sm, 'pack_size', 1) or 1
    moq = getattr(sm, 'minimum_order_quantity', 1) or 1

    if calculated_qty > 0:
        if pack_size > 1:
            calculated_qty = int(math.ceil(calculated_qty / pack_size) * pack_size)
        if moq > 1 and calculated_qty < moq:
            # Respect max stock when applying MOQ where possible
            if max_stock and (effective_available + moq) <= max_stock * 1.2:
                calculated_qty = moq
            else:
                calculated_qty = max(calculated_qty, pack_size)

    # 10. Check Existing Active Orders for Duplicate Prevention
    active_order = get_existing_active_order_for_medicine(medicine)
    has_active_order = active_order is not None

    # Priority determination
    if usable_stock == 0:
        priority = PurchaseRequest.Priority.CRITICAL
        reason_tag = "OUT OF STOCK (ZERO USABLE STOCK)"
    elif usable_stock <= int(reorder_level * 0.3):
        priority = PurchaseRequest.Priority.URGENT
        reason_tag = "CRITICALLY LOW STOCK"
    elif usable_stock <= reorder_level:
        priority = PurchaseRequest.Priority.NORMAL
        reason_tag = "BELOW REORDER LEVEL"
    else:
        priority = PurchaseRequest.Priority.NORMAL
        reason_tag = "FORECAST CONSUMPTION SHORTFALL"

    # Explainable calculation rationale
    open_po_str = f" [Open PO confirmed: {open_po_qty} units]" if open_po_qty > 0 else ""
    pending_pr_str = f" [Pending PR: {pending_pr_qty} units]" if pending_pr_qty > 0 else ""
    supplier_info = f"Supplier: {supplier.company_name} (Lead: {lead_days}d)" if supplier else "No active supplier assigned"

    reason_text = (
        f"AI Auto-Reorder: {reason_tag}. Usable stock: {usable_stock}{open_po_str}{pending_pr_str}, "
        f"Reorder level: {reorder_level}, Safety stock: {safety_stock}, 30d Forecast: {forecast_30d}. "
        f"Suggested replenishment: {calculated_qty} units. {supplier_info}."
    )

    return {
        'medicine': medicine,
        'usable_stock': usable_stock,
        'current_stock': usable_stock,
        'reorder_level': reorder_level,
        'safety_stock': safety_stock,
        'maximum_stock': max_stock,
        'minimum_stock': min_stock,
        'forecast_30d': forecast_30d,
        'daily_rate': daily_rate,
        'open_po_qty': open_po_qty,
        'pending_pr_qty': pending_pr_qty,
        'effective_available': effective_available,
        'supplier': supplier,
        'supplier_medicine': sm,
        'unit_price': unit_price,
        'lead_days': lead_days,
        'pack_size': pack_size,
        'minimum_order_quantity': moq,
        'target_stock': target_stock,
        'net_shortfall': net_shortfall,
        'calculated_qty': calculated_qty,
        'suggested_qty': calculated_qty,
        'needs_reorder': needs_reorder and (calculated_qty > 0),
        'has_active_order': has_active_order,
        'active_order': active_order,
        'priority': priority,
        'reason_tag': reason_tag,
        'reason_text': reason_text,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4. ATOMIC AUTO-REORDER EXECUTION
# ─────────────────────────────────────────────────────────────────────────────

@transaction.atomic
def create_auto_reorder_request(medicine, requested_qty, supplier=None, unit_price=None,
                               reason="", priority=None, triggered_by_user=None):
    """
    Creates or updates an official PurchaseRequest and associated PurchaseRequestItem
    in PENDING_APPROVAL status.
    Guarantees:
      - Preserves Admin governance / approval requirements.
      - Never duplicates an existing active PurchaseRequest.
      - Flags controlled medicines (Schedule H1, Schedule X, Narcotic) for mandatory clinical sign-off.
      - Syncs the AIRecommendation status to AUTO_APPLIED.
      - NEVER modifies inventory stock (stock changes ONLY on verified GRN).
    """
    from ai_engine.models import AIRecommendation

    if requested_qty <= 0:
        return {'created': False, 'updated': False, 'reason': 'Quantity must be greater than zero.'}

    usable_stock = get_usable_stock(medicine)
    inv = getattr(medicine, 'inventory', None)
    reorder_level = inv.reorder_level if (inv and inv.reorder_level is not None) else medicine.reorder_level

    # 1. Duplicate check: if an active PurchaseRequest already exists, update it rather than duplicating
    existing_item = PurchaseRequestItem.objects.filter(
        medicine=medicine,
        purchase_request__status__in=ACTIVE_PR_STATUSES,
    ).select_related('purchase_request').first()

    if existing_item:
        pr = existing_item.purchase_request
        # If shortfall increased, update requested and proposed approved qty
        if requested_qty > existing_item.requested_qty:
            existing_item.requested_qty = requested_qty
            existing_item.approved_qty = requested_qty
            existing_item.save(update_fields=['requested_qty', 'approved_qty'])
            pr.notes = (pr.notes + f"\n[AI Auto-Reorder] Updated quantity to {requested_qty} units on {timezone.now().strftime('%Y-%m-%d %H:%M')}.").strip()
            pr.save(update_fields=['notes', 'updated_at'])

        # Sync AI recommendation record
        _sync_ai_recommendation_status(medicine, pr, requested_qty, supplier, status=AIRecommendation.Status.AUTO_APPLIED)

        return {
            'created': False,
            'updated': True,
            'purchase_request': pr,
            'item': existing_item,
            'action': 'UPDATED_EXISTING_PR',
            'message': f"Active Purchase Request {pr.request_id} already exists for {medicine.medicine_name}; updated quantity to {existing_item.requested_qty} units."
        }

    # 2. Check if open PO already covers it
    open_po_item = PurchaseOrderItem.objects.filter(
        medicine=medicine,
        purchase_order__status__in=CONFIRMED_INCOMING_PO_STATUSES,
        quantity_pending__gt=0,
    ).select_related('purchase_order').first()

    if open_po_item and open_po_item.quantity_pending >= requested_qty:
        return {
            'created': False,
            'updated': False,
            'purchase_order': open_po_item.purchase_order,
            'action': 'COVERED_BY_OPEN_PO',
            'message': f"Open Purchase Order {open_po_item.purchase_order.po_number} already covers {medicine.medicine_name} ({open_po_item.quantity_pending} units pending)."
        }

    # 3. Resolve supplier pricing
    if supplier is None or unit_price is None:
        matched_supplier, sm, resolved_price = get_best_active_supplier(medicine)
        supplier = supplier or matched_supplier
        unit_price = unit_price or resolved_price

    # 4. Determine priority and controlled-medicine notes
    if priority is None:
        priority = (
            PurchaseRequest.Priority.CRITICAL if usable_stock == 0
            else (PurchaseRequest.Priority.URGENT if usable_stock <= int(reorder_level * 0.3)
                  else PurchaseRequest.Priority.NORMAL)
        )

    is_controlled = (
        getattr(medicine, 'regulatory_category', 'NORMAL') in ['SCHEDULE_X', 'NARCOTIC', 'SCHEDULE_H1', 'CONTROLLED_OTHER']
        or medicine.batches.filter(is_controlled=True).exists()
    )

    notes = "AI Auto-Reorder generated replenishment request."
    if is_controlled:
        notes += f" [MANDATORY APPROVAL: {medicine.regulatory_category} controlled substance - clinical pharmacist/admin review required before PO issuance.]"
    if not supplier:
        notes += " [NO ACTIVE SUPPLIER CONFIGURED: Admin must assign supplier during review.]"

    # 5. Create new PurchaseRequest
    req_id = PurchaseRequest.generate_request_id()
    pr = PurchaseRequest.objects.create(
        request_id=req_id,
        priority=priority,
        status=PurchaseRequest.PRStatus.PENDING_APPROVAL,
        reason=reason or f"AI Auto-Reorder: Low usable stock ({usable_stock} units vs reorder level {reorder_level}).",
        notes=notes,
        recommended_supplier=supplier,
        created_by=triggered_by_user,
    )

    # 6. Create PurchaseRequestItem line
    pr_item = PurchaseRequestItem.objects.create(
        purchase_request=pr,
        medicine=medicine,
        current_stock=usable_stock,
        reorder_level=reorder_level,
        ai_recommended_qty=requested_qty,
        requested_qty=requested_qty,
        approved_qty=requested_qty,
        suggested_supplier=supplier,
        suggested_unit_price=unit_price or Decimal('0.00'),
        notes=f"Auto-generated for {medicine.medicine_name} ({requested_qty} units).",
    )

    # 7. Sync AI recommendation record
    _sync_ai_recommendation_status(medicine, pr, requested_qty, supplier, status=AIRecommendation.Status.AUTO_APPLIED)

    logger.info(f"[Auto-Reorder] Created Purchase Request {pr.request_id} for {medicine.medicine_name} ({requested_qty} units, Supplier: {supplier}).")

    return {
        'created': True,
        'updated': False,
        'purchase_request': pr,
        'item': pr_item,
        'action': 'CREATED_PR',
        'message': f"Purchase Request {pr.request_id} created successfully for {medicine.medicine_name} ({requested_qty} units)."
    }


def _sync_ai_recommendation_status(medicine, pr, quantity, supplier, status):
    """Syncs or creates the AIRecommendation database record for audit and dashboard display."""
    from ai_engine.models import AIRecommendation

    try:
        rec = AIRecommendation.objects.filter(
            medicine=medicine,
            recommendation_type=AIRecommendation.RecommendationType.REORDER,
            status__in=[AIRecommendation.Status.PENDING, AIRecommendation.Status.AUTO_APPLIED],
        ).first()

        action_data = {
            'purchase_request_id': pr.id,
            'request_id': pr.request_id,
            'requested_qty': quantity,
            'supplier_id': supplier.id if supplier else None,
            'supplier_name': supplier.company_name if supplier else 'None',
            'auto_ordered': True,
            'processed_at': timezone.now().isoformat(),
        }

        if rec:
            rec.status = status
            rec.recommended_quantity = quantity
            rec.recommended_supplier = supplier
            rec.supplier = supplier
            rec.action_data = action_data
            rec.save(update_fields=['status', 'recommended_quantity', 'recommended_supplier', 'supplier', 'action_data'])
        else:
            AIRecommendation.objects.create(
                recommendation_type=AIRecommendation.RecommendationType.REORDER,
                engine_type=AIRecommendation.EngineType.HYBRID,
                medicine=medicine,
                supplier=supplier,
                recommended_supplier=supplier,
                title=f"AI Auto-Reorder: {medicine.medicine_name} ({quantity} units)",
                message=pr.reason,
                recommended_quantity=quantity,
                status=status,
                action_data=action_data,
                priority_score=Decimal('90.00') if pr.priority == PurchaseRequest.Priority.CRITICAL else Decimal('75.00'),
            )
    except Exception as e:
        logger.warning(f"Error syncing AIRecommendation status for {medicine}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# 5. SINGLE MEDICINE & BULK AUTOMATION ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def process_medicine_auto_reorder(medicine, dry_run=False, triggered_by_user=None):
    """
    Evaluates a single medicine for auto-reordering.
    Returns:
      {
        'status': 'CREATED' | 'UPDATED' | 'SKIPPED_NORMAL_STOCK' | 'SKIPPED_EXISTING_ORDER' | 'SKIPPED_ZERO_QTY' | 'DRY_RUN',
        'medicine': medicine,
        'details': details_dict,
        'result': create_result_dict,
        'message': str,
      }
    """
    details = calculate_auto_reorder_details(medicine)

    # 1. Normal stock check
    if not details['needs_reorder']:
        return {
            'status': 'SKIPPED_NORMAL_STOCK',
            'medicine': medicine,
            'details': details,
            'message': f"{medicine.medicine_name}: Stock is healthy (usable: {details['usable_stock']}, reorder level: {details['reorder_level']}). No reorder needed."
        }

    # 2. Existing order / duplicate check
    if details['has_active_order']:
        order_info = details['active_order']
        ref = order_info.get('reference', '')
        otype = order_info.get('order_type', '')
        return {
            'status': 'SKIPPED_EXISTING_ORDER',
            'medicine': medicine,
            'details': details,
            'message': f"{medicine.medicine_name}: Existing active {otype} ({ref}) already in progress. Skipped duplicate creation."
        }

    # 3. Quantity check
    if details['calculated_qty'] <= 0:
        return {
            'status': 'SKIPPED_ZERO_QTY',
            'medicine': medicine,
            'details': details,
            'message': f"{medicine.medicine_name}: Calculated replenishment quantity is 0 units. Skipped."
        }

    # 4. Dry-run simulation
    if dry_run:
        return {
            'status': 'DRY_RUN',
            'medicine': medicine,
            'details': details,
            'message': f"[DRY RUN] Would create Purchase Request for {medicine.medicine_name} ({details['calculated_qty']} units, Supplier: {details['supplier']})."
        }

    # 5. Execute creation
    res = create_auto_reorder_request(
        medicine=medicine,
        requested_qty=details['calculated_qty'],
        supplier=details['supplier'],
        unit_price=details['unit_price'],
        reason=details['reason_text'],
        priority=details['priority'],
        triggered_by_user=triggered_by_user,
    )

    status_key = 'CREATED' if res.get('created') else ('UPDATED' if res.get('updated') else 'SKIPPED')
    return {
        'status': status_key,
        'medicine': medicine,
        'details': details,
        'result': res,
        'message': res.get('message', ''),
    }


def run_auto_reorder(dry_run=False, triggered_by_user=None, quiet=False, medicine_ids=None):
    """
    Main periodic engine runner.
    Processes all active medicines safely:
      - Uses distributed cache locking to prevent overlapping executions.
      - Isolates per-medicine failures so bad data in one item never halts the loop.
      - Safe to run repeatedly (fully idempotent).
      - Returns comprehensive execution statistics.
    """
    stats = {
        'total_evaluated': 0,
        'reorder_needed': 0,
        'requests_created': 0,
        'requests_updated': 0,
        'skipped_normal_stock': 0,
        'skipped_existing_order': 0,
        'skipped_zero_qty': 0,
        'dry_run_count': 0,
        'failed': 0,
        'errors': [],
        'orders': [],
    }

    # 1. Acquire distributed lock (unless running in dry-run mode)
    if not dry_run:
        acquired = cache.add(AUTO_REORDER_LOCK_KEY, timezone.now().isoformat(), AUTO_REORDER_LOCK_TIMEOUT)
        if not acquired:
            msg = "Another auto-reorder task is currently running. Skipping this cycle to prevent duplicate execution."
            if not quiet:
                logger.warning(msg)
            return {'locked': True, 'message': msg, **stats}

    try:
        # 2. Query target medicines
        medicines = Medicine.objects.filter(status=Medicine.Status.ACTIVE).select_related('inventory').prefetch_related('batches')
        if medicine_ids:
            medicines = medicines.filter(id__in=medicine_ids)

        stats['total_evaluated'] = medicines.count()

        for med in medicines:
            try:
                outcome = process_medicine_auto_reorder(med, dry_run=dry_run, triggered_by_user=triggered_by_user)
                st = outcome['status']

                if st == 'CREATED':
                    stats['reorder_needed'] += 1
                    stats['requests_created'] += 1
                    pr = outcome['result'].get('purchase_request')
                    stats['orders'].append({
                        'medicine': med.medicine_name,
                        'request_id': pr.request_id if pr else '',
                        'quantity': outcome['details']['calculated_qty'],
                        'supplier': str(outcome['details']['supplier']),
                    })
                elif st == 'UPDATED':
                    stats['reorder_needed'] += 1
                    stats['requests_updated'] += 1
                elif st == 'SKIPPED_NORMAL_STOCK':
                    stats['skipped_normal_stock'] += 1
                elif st == 'SKIPPED_EXISTING_ORDER':
                    stats['skipped_existing_order'] += 1
                elif st == 'SKIPPED_ZERO_QTY':
                    stats['skipped_zero_qty'] += 1
                elif st == 'DRY_RUN':
                    stats['reorder_needed'] += 1
                    stats['dry_run_count'] += 1

            except Exception as e:
                logger.error(f"[Auto-Reorder] Error processing medicine {med.id} ({med.medicine_name}): {e}", exc_info=True)
                stats['failed'] += 1
                stats['errors'].append({'medicine': med.medicine_name, 'error': str(e)})

    finally:
        if not dry_run:
            cache.delete(AUTO_REORDER_LOCK_KEY)

    return stats
