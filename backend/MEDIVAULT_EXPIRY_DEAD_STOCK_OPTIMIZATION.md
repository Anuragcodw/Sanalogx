# MediVault — Phase 4: Expiry & Dead Stock Optimization Architecture

## Executive Summary
This document specifies the architecture, algorithms, clinical safeguards, and test verification of MediVault’s Expiry and Dead Stock Optimization module. The module provides dynamic batch-level expiry tracking, First-Expired, First-Out (FEFO) stock allocation, financial risk exposure modeling, dead-stock detection, and overstock optimization—all integrated through the central `StockService` and `consumption_service`.

---

## 1. Dynamic Batch-Level Expiry Management

Expiry dates are evaluated dynamically against `timezone.localdate()` (Django local timezone) without hardcoded calendar dates or global timezone side-effects.

### Batch Expiry Risk Categories

| Risk Level | Dynamic Window | Usable Stock Status | Notification Action |
|---|---|---|---|
| **EXPIRED** | `expiry_date < today` | **EXCLUDED** (`status = EXPIRED`) | Immediate write-off alert (`EXPIRED_BATCH`) |
| **CRITICAL** | `today <= expiry_date <= today + 7d` | Active (FEFO Priority #1) | High-priority alert (`NEAR_EXPIRY:7d`) |
| **HIGH** | `today + 7d < expiry_date <= today + 30d` | Active (FEFO Priority #2) | Standard alert (`NEAR_EXPIRY:30d`) |
| **MEDIUM / NEAR** | `today + 30d < expiry_date <= today + 90d` | Active (FEFO Priority #3) | Expiry management workspace warning |
| **NORMAL** | `expiry_date > today + 90d` | Active | Standard FEFO allocation |

> **Stock Integrity Guard:** Expired (`EXPIRED`), quarantined (`QUARANTINED`), or returned (`RETURNED`) batches are strictly filtered out of `allocate_stock(policy='FEFO')` and `remove_stock(transaction_type=SALE/DISPENSE)`.

---

## 2. FEFO Stock Allocation & Barcode Non-Mutation

1. **FEFO Selection Algorithm (`inventory/services.py::allocate_stock`):**
   - Filters batches where `status IN ('ACTIVE', 'NEAR_EXPIRY')`, `quantity_available > 0`, and `expiry_date > today`.
   - Orders batches strictly by `expiry_date ASC, id ASC`.
   - Spans multiple batches when necessary to fulfill requested quantities.
   - **Read-Only Guarantee:** `allocate_stock` computes allocation plans without modifying batch balances.
2. **Barcode / QR Scan Non-Mutation:** Barcode and QR code lookups (`grn_barcode_lookup_api`, `scan_barcode_or_qr`) perform read-only database queries and execute **zero** stock mutations or transaction creations.
3. **Central StockService Routing:** All physical stock adjustments (expiry write-offs, damage write-offs, returns, reconciliation) route exclusively through `StockService` methods (`record_expiry`, `record_damage`, `record_return`, `adjust_stock`) using `@transaction.atomic` and row locking (`select_for_update()`). Direct batch editing from views/forms is prohibited.

---

## 3. Expiry Risk Intelligence & Financial Exposure

The expiry risk engine predicts whether available batch stock will be consumed prior to expiration based on verified consumption velocity:

### Calculations & Metrics
- **30-Day Daily Consumption Rate ($\text{Rate}_{\text{daily}}$):** $\text{NetConsumption}_{30\text{d}} / 30.0$
- **Expected Consumed Quantity:** $\text{Qty}_{\text{expected}} = \lfloor \text{Rate}_{\text{daily}} \times \text{DaysRemaining} \rfloor$
- **Unconsumed Quantity at Risk:** $\text{Qty}_{\text{risk}} = \max(0, \text{QuantityAvailable} - \text{Qty}_{\text{expected}})$
- **Financial Exposure at Risk:** $\text{Exposure}_{\text{risk}} = \text{Qty}_{\text{risk}} \times \text{PurchasePrice}$

### Data Quality & Cold Start Disclosure
If transaction count in the last 90 days is $< 5$, the model flags data quality as `INSUFFICIENT_DATA` and labels the forecast as `RULE_BASED` to prevent misleading precision.

---

## 4. Dead Stock & Slow-Moving Inventory

Dead stock detection identifies idle capital without misclassifying newly received inventory.

### Criteria & Rules
- **Inactive Window:** No verified `SALE` or `DISPENSE` stock movement for $\ge 90$ days.
- **New Stock Protection:** Medicines with batches received within the last 30 days (`received_date >= today - 30d`) are **excluded** from dead stock classification regardless of movement history.
- **Consumption Deduplication:** Reuses `inventory/consumption_service.py` to deduplicate linked `SALE` and `DISPENSE` events, subtract customer `RETURN` transactions, and exclude cancelled sales.
- **Recorded Purchase-Cost Exposure:** $\text{DeadStockValue} = \text{CurrentStock} \times \text{PurchasePrice}_{\text{batch}}$

---

## 5. Overstock Optimization

- **Trigger Condition:** $\text{CurrentStock} > \text{MaximumStock}$
- **Excess Quantity:** $\text{Qty}_{\text{excess}} = \text{CurrentStock} - \text{MaximumStock}$
- **Excess Financial Value:** $\text{Value}_{\text{excess}} = \text{Qty}_{\text{excess}} \times \text{PurchasePrice}$
- **Actionable Governance:** Overstock triggers hold recommendations on future POs. The system **never** automatically discards or writes off overstock without human review and explicit authorization.

---

## 6. Verification & Test Execution Results

All Phase 4 functionality was verified using Django's test runner:

```bash
python manage.py test purchases.test_integrated_procurement inventory.test_stock_transaction_accuracy inventory.test_audit_fixes --verbosity=2
```

### Execution Output:
```text
Ran 65 tests in 12.197s

OK
Destroying test database for alias 'default' ('test_medivault')...
```

All 65 tests passed with 0 errors and 0 failures.
