"""
ai_engine/intelligence_service.py
Phase 6 + Final Upgrade — Real-time AI Intelligence & Optimization Service Layer

Analyzes MySQL database records (Sales, Dispensing, Inventory, Batches, Suppliers, Price History)
and provides ML demand forecasts, explainable AI reorder recommendations, dead stock detection,
overstock warnings, expiry risk analysis, and composite inventory risk scores.

Upgrades (Final Phase):
  - Multi-horizon demand forecasting (7/14/30/60/90 days)
  - Open Purchase Order awareness in reorder engine (prevents duplicate ordering)
  - Integration with inventory/consumption_service for real consumption data
"""

import os
import joblib
from decimal import Decimal
from datetime import timedelta, datetime

import numpy as np
from django.db.models import Sum, Avg, Count, Q
from django.utils import timezone

from medicines.models import Medicine, MedicineBatch
from inventory.models import Inventory, StockTransaction, StockAlert
from suppliers.models import Supplier, SupplierMedicine, SupplierPriceHistory
from purchases.services import get_best_supplier
from sales.models import Sale, SaleItem


# Load ML Demand Model if available
MODEL_PATH = os.path.join(os.path.dirname(__file__), '..', 'ml', 'models', 'demand_forecast_model.joblib')
_DEMAND_MODEL = None

# Only approved orders are procurement commitments; drafts/submissions are not incoming stock.
OPEN_PO_STATUSES = ('APPROVED', 'ORDERED', 'PARTIAL_RECEIPT')

try:
    if os.path.exists(MODEL_PATH):
        _DEMAND_MODEL = joblib.load(MODEL_PATH)
except Exception:
    _DEMAND_MODEL = None


# ─────────────────────────────────────────────────────────────────────────────
# 1. DEMAND FORECASTING ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def _get_usable_stock(medicine, as_of_date=None) -> int:
    """
    Count only valid, usable, non-expired, non-quarantined batches as usable stock.
    Strictly excludes:
      - Expired batches (expiry_date <= today or status == EXPIRED)
      - Quarantined batches (status == QUARANTINED)
      - Returned batches (status == RETURNED)
      - Depleted batches (quantity_available <= 0)
    Respects existing FEFO rules.
    """
    from ai_engine.auto_reorder_service import get_usable_stock
    return get_usable_stock(medicine, as_of_date=as_of_date)


def get_demand_forecast(medicine, cutoff_date=None):
    """
    Predict 30-day demand forecast for a medicine using ML model or Rule-Based cold-start fallback.
    Uses combined SALE + DISPENSE demand history strictly before cutoff_date to prevent future-data leakage.
    Returns dict: {forecast_30d, daily_rate, model_type, data_origin, confidence_score, explanation, cutoff_date}.
    """
    inv = getattr(medicine, 'inventory', None)
    current_stock = _get_usable_stock(medicine)
    reorder_level = inv.reorder_level if inv else medicine.reorder_level
    minimum_stock = inv.minimum_stock if inv else medicine.minimum_stock
    safety_stock = inv.safety_stock if inv else medicine.safety_stock

    if cutoff_date is not None:
        if isinstance(cutoff_date, datetime):
            cutoff_dt = timezone.make_aware(cutoff_date) if timezone.is_naive(cutoff_date) else cutoff_date
        else:
            cutoff_dt = timezone.make_aware(datetime.combine(cutoff_date, datetime.max.time()))
    else:
        cutoff_dt = timezone.now()

    # Use the shared clinical-consumption source so cancellations, returns, and
    # linked SALE/DISPENSE movements are handled consistently.
    from inventory.consumption_service import (
        get_consumption_for_period,
        get_consumption_transaction_count,
    )

    as_of_date = cutoff_dt.date()
    total_consumed_30d = get_consumption_for_period(medicine, 30, as_of_date=as_of_date)
    txn_count_30d = get_consumption_transaction_count(medicine, 30, as_of_date=as_of_date)
    total_consumed_7d = get_consumption_for_period(medicine, 7, as_of_date=as_of_date)

    daily_rate = round(total_consumed_30d / 30.0, 2) if total_consumed_30d > 0 else 0.0

    # Cold Start Check: If < 5 transactions, use Rule-Based fallback
    if txn_count_30d < 5 or total_consumed_30d == 0:
        rule_forecast = max(reorder_level * 2, safety_stock * 3)
        return {
            'medicine_id': medicine.id,
            'medicine_name': medicine.medicine_name,
            'current_stock': current_stock,
            'forecast_30d': rule_forecast,
            'daily_rate': round(rule_forecast / 30.0, 2),
            'model_type': 'RULE_BASED',
            'data_origin': 'ESTIMATE',
            'confidence_score': None,
            'is_cold_start': True,
            'data_quality': 'INSUFFICIENT_DATA',
            'transaction_count_30d': txn_count_30d,
            'training_cutoff': cutoff_dt.date(),
            'training_window_days': 30,
            'cutoff_date': cutoff_dt.date(),
            'explanation': "Insufficient historical demand data (under 5 transactions). Rule-based estimate used based on reorder level and safety stock buffers."
        }

    # ML Model Prediction
    if _DEMAND_MODEL is not None:
        try:
            import pandas as pd
            features = pd.DataFrame(
                [[current_stock, reorder_level, minimum_stock, safety_stock, total_consumed_7d]],
                columns=['current_stock', 'reorder_level', 'minimum_stock', 'safety_stock', 'demand_7d']
            )
            pred_val = int(round(_DEMAND_MODEL.predict(features)[0]))
            forecast_30d = max(1, pred_val)
            model_type = 'ML_MODEL'
            data_origin = 'FORECAST'
            confidence = None
            explanation = f"ML model prediction based on {txn_count_30d} recent transactions and 7-day consumption velocity ({total_consumed_7d} units)."
        except Exception:
            forecast_30d = max(total_consumed_30d, reorder_level)
            model_type = 'RULE_BASED'
            data_origin = 'ESTIMATE'
            confidence = None
            explanation = f"Calculated baseline forecast from {total_consumed_30d} units consumed in past 30 days."
    else:
        forecast_30d = max(total_consumed_30d, reorder_level)
        model_type = 'RULE_BASED'
        data_origin = 'ESTIMATE'
        confidence = None
        explanation = f"Calculated baseline forecast from {total_consumed_30d} units consumed in past 30 days."

    return {
        'medicine_id': medicine.id,
        'medicine_name': medicine.medicine_name,
        'current_stock': current_stock,
        'forecast_30d': forecast_30d,
        'daily_rate': round(forecast_30d / 30.0, 2),
        'model_type': model_type,
        'data_origin': data_origin,
        'confidence_score': confidence,
        'is_cold_start': False,
        'data_quality': 'GOOD' if txn_count_30d >= 15 else 'WARNING',
        'transaction_count_30d': txn_count_30d,
        'training_cutoff': cutoff_dt.date(),
        'training_window_days': 30,
        'cutoff_date': cutoff_dt.date(),
        'explanation': explanation,
    }


def get_demand_forecast_multi_horizon(medicine, cutoff_date=None) -> dict:
    """
    Returns demand forecasts across multiple time horizons: 7, 14, 30, 60, 90 days.
    Guarantees no future-data leakage beyond cutoff_date.
    """
    from inventory.consumption_service import get_consumption_for_period, get_data_quality_indicator

    base = get_demand_forecast(medicine, cutoff_date=cutoff_date)
    daily_rate = base.get('daily_rate', 0)
    data_quality = get_data_quality_indicator(medicine, as_of_date=base.get('cutoff_date'))

    return {
        'medicine_id': medicine.id,
        'medicine_name': medicine.medicine_name,
        'model_type': base['model_type'],
        'data_origin': base.get('data_origin', 'FORECAST'),
        'confidence_score': base['confidence_score'],
        'is_cold_start': base.get('is_cold_start', False),
        'data_quality': data_quality,
        'cutoff_date': base.get('cutoff_date'),
        'forecast_7d': max(0, round(daily_rate * 7)),
        'forecast_14d': max(0, round(daily_rate * 14)),
        'forecast_30d': base['forecast_30d'],
        'forecast_60d': max(0, round(daily_rate * 60)),
        'forecast_90d': max(0, round(daily_rate * 90)),
        'daily_rate': daily_rate,
        'training_cutoff': base.get('training_cutoff'),
        'training_window_days': base.get('training_window_days'),
        'explanation': base['explanation'],
    }


def evaluate_forecast_accuracy(medicine, test_days: int = 14, train_window_days: int = 60) -> dict:
    """
    Evaluates forecast model accuracy using time-based validation without future-data leakage.
    Splits history into a training period and a test holdout period.
    Computes MAE (Mean Absolute Error) and RMSE (Root Mean Squared Error).
    """
    if test_days <= 0:
        raise ValueError("test_days must be greater than zero.")

    from inventory.consumption_service import (
        get_consumption_for_period,
        get_consumption_transaction_count,
    )

    today = timezone.localdate()
    test_start_date = today - timedelta(days=test_days - 1)
    training_cutoff = test_start_date - timedelta(days=1)

    # Train only on dates before the holdout window.
    forecast = get_demand_forecast(medicine, cutoff_date=training_cutoff)
    predicted_daily_rate = forecast.get('daily_rate') if forecast.get('daily_rate') is not None else round(forecast.get('forecast_30d', 0) / 30.0, 2)

    actual_test_total = get_consumption_for_period(medicine, test_days, as_of_date=today)
    actual_test_count = get_consumption_transaction_count(medicine, test_days, as_of_date=today)
    actual_daily_rate = round(actual_test_total / float(test_days), 2)

    # An empty holdout cannot establish forecast accuracy.
    mae = round(abs(predicted_daily_rate - actual_daily_rate), 2) if actual_test_count else None
    rmse = round(((predicted_daily_rate - actual_daily_rate) ** 2) ** 0.5, 2) if actual_test_count else None

    return {
        'medicine_id': medicine.id,
        'medicine_name': medicine.medicine_name,
        'test_period_days': test_days,
        'cutoff_date': training_cutoff,
        'training_window_days': train_window_days,
        'test_start_date': test_start_date,
        'predicted_daily_rate': predicted_daily_rate,
        'actual_daily_rate': actual_daily_rate,
        'actual_transaction_count': actual_test_count,
        'mae': mae,
        'rmse': rmse,
        'has_sufficient_history': not forecast.get('is_cold_start', False),
        'model_type': forecast['model_type'],
        'data_origin': forecast.get('data_origin', 'ESTIMATE'),
    }


def _get_open_po_qty(medicine) -> int:
    """
    Returns total quantity of `medicine` in open (unfulfilled) Purchase Orders.
    Used by reorder engine to prevent duplicate procurement.
    """
    try:
        from purchases.models import PurchaseOrderItem
        pending = PurchaseOrderItem.objects.filter(
            medicine=medicine,
            purchase_order__status__in=OPEN_PO_STATUSES,
        ).aggregate(s=Sum('quantity_pending'))['s'] or 0
        return pending
    except Exception:
        return 0


def get_ved_classification(medicine) -> str:
    """
    Returns VED (Vital, Essential, Desirable) clinical criticality classification.
    - Vital (V): Life-saving, narcotic, or special controlled items (Schedule X, Narcotic)
    - Essential (E): Prescription items (Schedule H, H1, Controlled Other)
    - Desirable (D): General / OTC items (Normal)
    Note: Regulatory category and VED clinical criticality are distinct attributes.
    """
    reg = getattr(medicine, 'regulatory_category', 'NORMAL')
    if reg in ['SCHEDULE_X', 'NARCOTIC']:
        return 'V'
    elif reg in ['SCHEDULE_H', 'SCHEDULE_H1', 'CONTROLLED_OTHER']:
        return 'E'
    return 'D'


# ─────────────────────────────────────────────────────────────────────────────
# 2. AI REORDER RECOMMENDATIONS (with Open PO Awareness & Lead-Time Gap)
# ─────────────────────────────────────────────────────────────────────────────

def get_reorder_recommendations():
    """
    Generates explainable AI reorder recommendations for active medicines.

    Upgrades:
      - Open PO Awareness: checks pending purchase order quantities before recommending.
        If open_po_qty already covers the shortfall, recommendation is skipped or reduced.
      - Formula:
        Projected stock at lead-time horizon = usable_stock + open_po_qty - (daily_rate * lead_days)
        Reorder Qty = min(remaining_capacity_to_max, max(0, net_shortfall))
      - Priority: CRITICAL / HIGH / MEDIUM / LOW based on stock vs reorder/safety stock and lead-time gap.
      - VED criticality and Schedule H1 regulatory labels are handled as distinct attributes.
    """
    from inventory.consumption_service import get_data_quality_indicator

    medicines = Medicine.objects.filter(status='ACTIVE').select_related('inventory')
    recommendations = []

    for med in medicines:
        inv = getattr(med, 'inventory', None)
        current_stock = _get_usable_stock(med)
        reorder_level = inv.reorder_level if inv else med.reorder_level
        safety_stock = inv.safety_stock if inv else med.safety_stock
        max_stock = inv.maximum_stock if inv else med.maximum_stock

        forecast = get_demand_forecast(med)
        forecast_30d = forecast.get('forecast_30d', 0)
        daily_rate = forecast.get('daily_rate') if forecast.get('daily_rate') is not None else round(forecast_30d / 30.0, 2)

        # Open PO awareness: stock already on order
        open_po_qty = _get_open_po_qty(med)
        effective_available = current_stock + open_po_qty

        # Check if reorder is justified (factoring in open POs)
        needs_reorder = (
            effective_available <= reorder_level
            or (effective_available - forecast_30d) < safety_stock
        )

        if not needs_reorder:
            continue

        # Recommended qty = what's needed after considering current stock AND open POs
        net_shortfall = forecast_30d + safety_stock - effective_available
        remaining_capacity = max(0, max_stock - effective_available)
        rec_qty = min(remaining_capacity, max(0, net_shortfall))
        if rec_qty <= 0:
            continue

        # ── Lead-time-aware clinical urgency ─────────────────────────────
        days_of_stock_remaining = int(current_stock / daily_rate) if daily_rate > 0 else 9999

        # Get best supplier lead time (days)
        best_sm = get_best_supplier(med)
        supplier = best_sm.supplier if best_sm else None
        unit_price = best_sm.unit_price if best_sm else Decimal('0.00')
        supplier_lead_days = supplier.average_delivery_days if supplier else 7
        stockout_risk_days = days_of_stock_remaining - supplier_lead_days  # negative = will stock out before delivery
        projected_stock_lead_time = max(0, round(current_stock + open_po_qty - (daily_rate * supplier_lead_days), 1))

        # VED clinical criticality
        ved_category = get_ved_classification(med)

        # Determine Priority — stockout risk & lead-time gap first, NOT regulatory label alone
        if current_stock == 0:
            priority = 'CRITICAL'
            reason_tag = "OUT OF STOCK"
        elif stockout_risk_days < 0:
            priority = 'CRITICAL'
            reason_tag = f"STOCKOUT BEFORE DELIVERY (+{abs(stockout_risk_days)}d gap)"
        elif current_stock <= int(reorder_level * 0.3):
            priority = 'CRITICAL'
            reason_tag = "CRITICALLY LOW"
        elif current_stock <= reorder_level:
            priority = 'HIGH'
            reason_tag = "BELOW REORDER LEVEL"
        else:
            priority = 'MEDIUM'
            reason_tag = "HIGH FORECAST CONSUMPTION"

        data_quality = get_data_quality_indicator(med)

        # Build explainable reason string
        open_po_note = f" ({open_po_qty} units already on open PO.)" if open_po_qty > 0 else ""
        explanation = (
            f"{reason_tag}: Current usable stock ({current_stock}){open_po_note} is insufficient for "
            f"30-day forecasted demand ({forecast_30d} units) and safety stock ({safety_stock} units). "
            f"Projected stock at lead-time horizon ({supplier_lead_days}d): {projected_stock_lead_time} units. "
            f"Net procurement needed: {rec_qty} units."
        )

        recommendations.append({
            'medicine': med,
            'current_stock': current_stock,
            'usable_stock': current_stock,
            'open_po_qty': open_po_qty,
            'effective_available': effective_available,
            'reorder_level': reorder_level,
            'safety_stock': safety_stock,
            'forecast_30d': forecast_30d,
            'daily_rate': daily_rate,
            'recommended_qty': rec_qty,
            'recommended_reorder_qty': rec_qty,
            'priority': priority,
            'reason_tag': reason_tag,
            'days_of_stock_remaining': days_of_stock_remaining if days_of_stock_remaining != 9999 else None,
            'supplier_lead_days': supplier_lead_days,
            'stockout_risk_days': stockout_risk_days,
            'projected_stock_lead_time': projected_stock_lead_time,
            'ved_category': ved_category,
            'regulatory_category': med.regulatory_category,
            'data_quality': data_quality,
            'recommended_supplier': supplier,
            'estimated_unit_price': unit_price,
            'estimated_total_cost': round(Decimal(str(rec_qty)) * unit_price, 2),
            'explanation': explanation,
            'model_type': forecast['model_type'],
            'confidence_score': forecast['confidence_score'],
        })

    priority_order = {'CRITICAL': 0, 'HIGH': 1, 'MEDIUM': 2, 'LOW': 3}
    recommendations.sort(key=lambda x: priority_order.get(x['priority'], 4))
    return recommendations


def sync_ai_recommendations():
    """
    Syncs live intelligence engine outputs with the AIRecommendation table in database,
    avoiding duplicate active entries for the same medicine and recommendation type.
    """
    from .models import AIRecommendation
    reorders = get_reorder_recommendations()

    for rec in reorders:
        med = rec['medicine']
        existing = AIRecommendation.objects.filter(
            medicine=med,
            recommendation_type=AIRecommendation.RecommendationType.REORDER,
            status=AIRecommendation.Status.PENDING,
        ).first()

        title = f"Reorder {med.medicine_name} ({rec['recommended_qty']} units suggested)"
        p_score = 90.0 if rec['priority'] == 'CRITICAL' else (75.0 if rec['priority'] == 'HIGH' else 50.0)

        source_data = {
            'usable_stock': rec['usable_stock'],
            'open_po_qty': rec['open_po_qty'],
            'effective_available': rec['effective_available'],
            'reorder_level': rec['reorder_level'],
            'safety_stock': rec['safety_stock'],
            'forecast_30d': rec['forecast_30d'],
            'days_of_stock_remaining': rec['days_of_stock_remaining'],
            'supplier_lead_days': rec['supplier_lead_days'],
            'stockout_risk_days': rec['stockout_risk_days'],
            'projected_stock_lead_time': rec['projected_stock_lead_time'],
            'ved_category': rec['ved_category'],
            'regulatory_category': med.regulatory_category,
            'reason_tag': rec['reason_tag'],
            'data_quality': rec['data_quality'],
        }

        if existing:
            existing.title = title
            existing.message = rec['explanation']
            existing.recommended_quantity = rec['recommended_qty']
            existing.recommended_supplier = rec['recommended_supplier']
            existing.priority_score = Decimal(str(p_score))
            existing.forecasted_demand_30d = rec['forecast_30d']
            existing.source_data = source_data
            existing.save()
        else:
            AIRecommendation.objects.create(
                recommendation_type=AIRecommendation.RecommendationType.REORDER,
                engine_type=AIRecommendation.EngineType.HYBRID if rec['model_type'] == 'ML_MODEL' else AIRecommendation.EngineType.RULE_BASED,
                medicine=med,
                supplier=rec['recommended_supplier'],
                recommended_supplier=rec['recommended_supplier'],
                title=title,
                message=rec['explanation'],
                recommended_quantity=rec['recommended_qty'],
                forecasted_demand_30d=rec['forecast_30d'],
                priority_score=Decimal(str(p_score)),
                status=AIRecommendation.Status.PENDING,
                source_data=source_data,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 3. DEAD STOCK & OVERSTOCK DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def get_dead_stock(days_threshold=90):
    """
    Identifies medicines with zero SALE or DISPENSE stock movement for > 90 days.
    Returns list of dead stock entries.
    """
    cutoff_date = timezone.now() - timedelta(days=days_threshold)
    medicines = Medicine.objects.filter(status='ACTIVE').select_related('inventory').prefetch_related('batches')
    dead_stock_list = []

    for med in medicines:
        inv = getattr(med, 'inventory', None)
        current_stock = inv.current_stock if inv else 0
        if current_stock <= 0:
            continue

        # Check if batch was recently received (within last 30 days) — do not flag as dead stock
        recently_received = med.batches.filter(
            received_date__gte=timezone.now().date() - timedelta(days=30),
            quantity_available__gt=0
        ).exists()
        if recently_received:
            continue

        # Check latest sale or dispense transaction
        last_txn = StockTransaction.objects.filter(
            medicine=med,
            transaction_type__in=['SALE', 'DISPENSE']
        ).order_by('-created_at').first()

        if not last_txn:
            days_inactive = (timezone.now() - med.created_at).days
            last_date = med.created_at.date()
        else:
            days_inactive = (timezone.now() - last_txn.created_at).days
            last_date = last_txn.created_at.date()

        if days_inactive >= days_threshold:
            # Estimate inventory value based on latest batch purchase price
            first_batch = med.batches.filter(quantity_available__gt=0).first()
            cost_price = first_batch.purchase_price if first_batch else Decimal('0.00')
            inventory_value = round(Decimal(str(current_stock)) * cost_price, 2)

            dead_stock_list.append({
                'medicine': med,
                'current_stock': current_stock,
                'days_inactive': days_inactive,
                'last_movement_date': last_date,
                'inventory_value': inventory_value,
                'recommendation': "Review procurement and consider promotion or redistribution before reordering."
            })

    dead_stock_list.sort(key=lambda x: x['days_inactive'], reverse=True)
    return dead_stock_list


def get_overstock():
    """
    Identifies medicines where current_stock > maximum_stock.
    """
    medicines = Medicine.objects.filter(status='ACTIVE').select_related('inventory').prefetch_related('batches')
    overstock_list = []

    for med in medicines:
        inv = getattr(med, 'inventory', None)
        current_stock = inv.current_stock if inv else 0
        max_stock = inv.maximum_stock if inv else med.maximum_stock

        if current_stock > max_stock:
            excess_qty = current_stock - max_stock
            first_batch = med.batches.filter(quantity_available__gt=0).first()
            cost_price = first_batch.purchase_price if first_batch else Decimal('0.00')
            excess_value = round(Decimal(str(excess_qty)) * cost_price, 2)

            overstock_list.append({
                'medicine': med,
                'current_stock': current_stock,
                'maximum_stock': max_stock,
                'excess_qty': excess_qty,
                'excess_value': excess_value,
                'recommendation': "Hold further reorders; surplus inventory exceeds configured maximum storage limits."
            })

    return overstock_list


# ─────────────────────────────────────────────────────────────────────────────
# 4. EXPIRY RISK ANALYSIS & INVENTORY RISK SCORE
# ─────────────────────────────────────────────────────────────────────────────

def get_expiry_risk(days_threshold=90):
    """
    Identifies medicine batches expiring within days_threshold where available_stock > forecasted consumption.
    """
    today = timezone.now().date()
    cutoff_date = today + timedelta(days=days_threshold)

    batches = MedicineBatch.objects.filter(
        expiry_date__gt=today,
        expiry_date__lte=cutoff_date,
        quantity_available__gt=0
    ).exclude(status__in=['EXPIRED', 'RETURNED', 'QUARANTINED']).select_related('medicine')

    expiry_risks = []

    for b in batches:
        days_remaining = (b.expiry_date - today).days
        forecast = get_demand_forecast(b.medicine)
        daily_rate = forecast['daily_rate']

        expected_consumed = int(daily_rate * days_remaining)
        unconsumed_risk_qty = max(0, b.quantity_available - expected_consumed)

        if days_remaining <= 30:
            risk_level = 'CRITICAL'
        elif unconsumed_risk_qty > 0 or days_remaining <= 60:
            risk_level = 'HIGH'
        else:
            risk_level = 'MEDIUM'

        cost_price = b.purchase_price
        risk_value = round(Decimal(str(b.quantity_available)) * cost_price, 2)

        expiry_risks.append({
            'batch': b,
            'medicine': b.medicine,
            'current_stock': b.quantity_available,
            'expiry_date': b.expiry_date,
            'days_remaining': days_remaining,
            'expected_consumed': expected_consumed,
            'unconsumed_risk_qty': unconsumed_risk_qty,
            'risk_level': risk_level,
            'risk_value': risk_value,
            'recommendation': "Prioritize FEFO dispensing immediately or arrange supplier return if under agreement."
        })

    expiry_risks.sort(key=lambda x: x['days_remaining'])
    return expiry_risks


def get_inventory_risk_score(medicine):
    """
    Computes composite inventory risk score (0-100) and risk level (LOW, MEDIUM, HIGH, CRITICAL).
    """
    inv = getattr(medicine, 'inventory', None)
    current_stock = inv.current_stock if inv else 0
    reorder_level = inv.reorder_level if inv else medicine.reorder_level

    score = 0

    # Stock Risk (0-40)
    if current_stock == 0:
        score += 40
    elif current_stock <= int(reorder_level * 0.5):
        score += 30
    elif current_stock <= reorder_level:
        score += 20

    # Expiry Risk (0-40)
    today = timezone.now().date()
    near_exp_batch = medicine.batches.filter(
        expiry_date__gt=today,
        expiry_date__lte=today + timedelta(days=60),
        quantity_available__gt=0
    ).first()

    if near_exp_batch:
        days = (near_exp_batch.expiry_date - today).days
        if days <= 30:
            score += 40
        else:
            score += 20

    # Dead Stock Risk (0-20)
    last_txn = StockTransaction.objects.filter(medicine=medicine, transaction_type__in=['SALE', 'DISPENSE']).first()
    if last_txn and (timezone.now() - last_txn.created_at).days > 90:
        score += 20

    if score >= 70:
        level = 'CRITICAL'
    elif score >= 50:
        level = 'HIGH'
    elif score >= 25:
        level = 'MEDIUM'
    else:
        level = 'LOW'

    return {'score': score, 'level': level}


# ─────────────────────────────────────────────────────────────────────────────
# 5. AI INSIGHTS SUMMARY
# ─────────────────────────────────────────────────────────────────────────────

def get_ai_insights_summary():
    """Returns top-level KPI metrics and counts for AI Dashboard."""
    reorders = get_reorder_recommendations()
    dead_stock = get_dead_stock()
    overstock = get_overstock()
    expiry_risk = get_expiry_risk()

    critical_reorders = sum(1 for r in reorders if r['priority'] == 'CRITICAL')
    high_reorders = sum(1 for r in reorders if r['priority'] == 'HIGH')

    return {
        'total_reorders': len(reorders),
        'critical_reorders': critical_reorders,
        'high_reorders': high_reorders,
        'dead_stock_count': len(dead_stock),
        'overstock_count': len(overstock),
        'expiry_risk_count': len(expiry_risk),
        'reorder_list': reorders[:10],
        'dead_stock_list': dead_stock[:10],
        'overstock_list': overstock[:10],
        'expiry_risk_list': expiry_risk[:10],
    }
