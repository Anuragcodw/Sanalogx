# MediVault — AI Stockout Prevention Engine Architecture

## Executive Summary
This document specifies the architecture, mathematical formulas, clinical priority logic, data input boundaries, and explainability mechanisms of MediVault’s AI Stockout Prevention Engine. The engine integrates real-time consumption velocity, usable batch-level stock, open Purchase Order commitments, supplier lead times, and clinical VED criticality to prevent pharmacy stockouts while eliminating duplicate procurement.

---

## 1. Engine Architecture & Source Data Pipeline

```
[Real Outbound Consumption] ◄── inventory/consumption_service.py (Deduplicated SALE + DISPENSE minus RETURN)
             │
             ├──► [Usable On-Hand Stock] (Active & Near-Expiry Non-Expired Batches Only)
             ├──► [Open PO Commitments] (Pending quantities from APPROVED/ORDERED/PARTIAL POs)
             ├──► [Supplier Lead Times] (Average delivery days per supplier)
             └──► [Clinical & Regulatory Metadata] (VED Criticality & Regulatory Category)
             │
             ▼
[AI Intelligence Service Layer] (ai_engine/intelligence_service.py)
             │
             ├──► Multi-Horizon Demand Forecast (7d, 14d, 30d, 60d, 90d)
             ├──► Lead-Time Stockout Risk & Projected Horizon Stock
             ├──► Reorder Quantity Calculation (Open PO & Capacity Aware)
             └──► Configurable Priority Score (CRITICAL / HIGH / MEDIUM / LOW)
             │
             ▼
[Actionable Outputs] ──► AI Dashboard / Recommendation Center / Deduplicated Alerts
```

---

## 2. Core Formulas & Mathematical Definitions

### A. Projected Stock at Supplier Lead-Time Horizon
$$\text{Projected Stock}_{\text{LeadTime}} = \max\Big(0, \text{Stock}_{\text{usable}} + \text{Qty}_{\text{OpenPO}} - (\text{Rate}_{\text{daily}} \times \text{LeadDays}_{\text{supplier}})\Big)$$

- $\text{Stock}_{\text{usable}}$: Non-expired, active/near-expiry batch quantity available.
- $\text{Qty}_{\text{OpenPO}}$: Sum of pending quantities across open POs (`APPROVED`, `ORDERED`, `PARTIAL_RECEIPT`).
- $\text{Rate}_{\text{daily}}$: 30-day deduplicated consumption velocity ($\text{Forecast}_{30\text{d}} / 30.0$).
- $\text{LeadDays}_{\text{supplier}}$: Historical average delivery lead time for the recommended supplier.

### B. Lead-Time Risk Gap
$$\text{StockoutRiskDays} = \text{DaysRemaining} - \text{LeadDays}_{\text{supplier}}$$
- $\text{StockoutRiskDays} < 0$: Stock is projected to run out **before** the supplier can complete delivery (Escalates priority to `CRITICAL`).

### C. Recommended Reorder Quantity
$$\text{ReorderQty} = \min\Big(\text{Stock}_{\text{max}} - (\text{Stock}_{\text{usable}} + \text{Qty}_{\text{OpenPO}}), \max\big(0, \text{Forecast}_{30\text{d}} + \text{Stock}_{\text{safety}} - (\text{Stock}_{\text{usable}} + \text{Qty}_{\text{OpenPO}})\big)\Big)$$
- Open PO Awareness prevents over-ordering when replenishment stock is already in-flight.
- Replenishment is capped at configured `maximum_stock` storage limits.

---

## 3. Priority Classification & Schedule H1 Handling

MediVault explicitly separates **Regulatory Classification** from **Clinical Stockout Priority**.

### Regulatory Classification vs. VED Clinical Criticality

- **Regulatory Classification** (`SCHEDULE_H1`, `SCHEDULE_X`, `NARCOTIC`, `NORMAL`): Governs legal record-keeping (e.g. prescription register requirements), dispensing authorization, and storage restrictions.
- **VED Clinical Criticality** (`V` = Vital, `E` = Essential, `D` = Desirable): Governs medical criticality of the drug in patient care.
- **Priority Rule:** Schedule H1 medicines with ample stock (e.g., 250 units in stock with reorder level 40) are **NOT** marked as `CRITICAL` or `HIGH` restock priority solely because they are Schedule H1. Restock priority is driven strictly by inventory depletion risk, lead-time gaps, and clinical demand.

### Priority Level Matrix

| Priority | Trigger Condition | Reason Tag | Email Alert |
|---|---|---|---|
| **CRITICAL** | `usable_stock == 0` | `OUT OF STOCK` | **YES** |
| **CRITICAL** | `stockout_risk_days < 0` | `STOCKOUT BEFORE DELIVERY (+Xd gap)` | **YES** |
| **CRITICAL** | `usable_stock <= 0.3 * reorder_level` | `CRITICALLY LOW` | **YES** |
| **HIGH** | `usable_stock <= reorder_level` | `BELOW REORDER LEVEL` | No |
| **MEDIUM** | `effective_available - forecast_30d < safety_stock` | `HIGH FORECAST CONSUMPTION` | No |
| **LOW** | Overstock / No immediate risk | `STABLE STOCK` | No |

---

## 4. Cold Start & Forecast Model Fallbacks

When historical consumption data is limited or unavailable, MediVault employs transparent fallback models:

| Data Condition | Model Type Tag | Data Quality Indicator | Method Used |
|---|---|---|---|
| **≥ 15 transactions in 90d** | `ML_MODEL` or `RULE_BASED` | `GOOD` | ML Regression or Weighted Consumption Velocity |
| **5–14 transactions in 90d** | `RULE_BASED` | `WARNING` | 30-day Consumption Moving Average |
| **< 5 transactions in 90d** | `RULE_BASED` | `INSUFFICIENT_DATA` | Cold-start buffer ($\max(2 \times \text{ReorderLevel}, 3 \times \text{SafetyStock})$) |

### Accuracy Evaluation (`evaluate_forecast_accuracy`)
Computes chronological validation without future-data leakage:
- **MAE (Mean Absolute Error):** $\frac{1}{n}\sum |\text{Predicted} - \text{Actual}|$
- **RMSE (Root Mean Squared Error):** $\sqrt{\frac{1}{n}\sum (\text{Predicted} - \text{Actual})^2}$

---

## 5. Notification Fingerprint Deduplication

Alerts generated by the AI Stockout Prevention engine reuse `notifications/service.py` with unique fingerprint keys:
- **Low/Critical Stock Alert:** `LOW_STOCK:{medicine_id}:{year}:{iso_week}`
- **Near-Expiry Alert:** `NEAR_EXPIRY:{batch_id}:{threshold_key}`
- **PO Overdue Alert:** `PO_OVERDUE:{po_id}:{iso_week}`

Duplicate notifications matching an existing active fingerprint within the same period are automatically suppressed.

---

## 6. Verification & Test Execution Results

Empirical verification executed via Django test suite:

```bash
python manage.py test purchases.test_integrated_procurement inventory.test_stock_transaction_accuracy inventory.test_audit_fixes --verbosity=2
```

### Execution Output:
```text
Ran 61 tests in 11.742s

OK
Destroying test database for alias 'default' ('test_medivault')...
```

All 61 unit and integration tests passed cleanly.
