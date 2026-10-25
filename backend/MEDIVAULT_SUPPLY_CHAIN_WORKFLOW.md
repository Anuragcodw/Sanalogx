# MediVault — End-to-End Hospital Pharmacy Supply Chain & Stock Optimization

## Architectural Overview

MediVault connects hospital pharmacy consumption, AI demand forecasting, low-stock notifications, restock requests, administrative approvals, supplier purchase orders, delivery tracking, physical goods receiving (GRN), and FEFO-driven dispensing into one integrated enterprise workflow on Django and MySQL.

The system enforces strict operational integrity:
- **Centralized Stock Engine**: No module (sales, dispensing, purchases, adjustments) directly decrements or increments `batch.quantity_available` or `Inventory.current_stock`. All stock mutations route through `inventory.services.add_stock()` and `inventory.services.remove_stock()` inside `@transaction.atomic` blocks.
- **Dispensing Stock Fix**: Dispensing transactions use `StockTransaction.TransactionType.DISPENSE`. Net consumption calculations deduplicate linked sales and dispensing transactions using reference IDs.
- **Zero Premature Stock Mutation**: Neither barcode scanning, prescription verification, purchase request creation, PO creation, nor PO email transmission mutates stock. Stock is only incremented when a pharmacist formally verifies an accepted Goods Receipt Note (GRN).
- **Accepted Quantity Isolation**: Rejected quantities (`DAMAGED_PACKAGING`, `EXPIRED_AT_ARRIVAL`, `BROKEN_COLD_CHAIN`, `SHORT_DELIVERY`, `WRONG_ITEM`) are logged for supplier dispute but strictly excluded from sellable inventory.

---

## 9-Stage Supply Chain Workflow

```
[1. Dispensing / Sale] ──(DISPENSE txn)──> [Inventory Depleted]
                                                  │
                                                  ▼
[3. Low Stock Alert] <──(Fingerprint Dedup)── [2. AI Demand Forecast & Urgency]
         │                                                │
         ▼                                                ▼
[4. Purchase Request] <─────────────────────── (Pre-filled from AI Rec)
         │
         ▼
[5. Admin Approval & Conversion] ──> [Purchase Order Created]
                                                │
                                                ▼
[6. PO Email Dispatch & Live Tracking] ──> [Supplier Dispatches Goods]
                                                │
                                                ▼
[7. Goods Receiving (GRN)] ──(Barcode Lookup / Rejection Logging)
                                                │
                                                ▼
[8. Atomic Verification] ──(add_stock: accepted only)──> [Sellable Batches Created]
                                                                │
                                                                ▼
                                                [9. FEFO Dispensing Queue]
```

### Stage 1: Consumption & Stock Depletion
- **POS Sales & Bedside Dispensing**: When medications are sold or dispensed to patients, stock is deducted batch-by-batch using First-Expiry-First-Out (FEFO) logic.
- **Transaction Types**: Sales log `StockTransaction.TransactionType.SALE`; Dispensing logs `StockTransaction.TransactionType.DISPENSE`.
- **Atomic Execution**: If any batch within a multi-item order has insufficient stock or is expired, the entire transaction rolls back cleanly.

### Stage 2: AI Forecasting & Reorder Intelligence
- **Demand Forecasting**: `ai_engine/intelligence_service.py` evaluates 30-day historical consumption using the Random Forest regressor (`demand_forecast_model.joblib`), falling back to moving averages when historical data points are limited.
- **Open PO Awareness**: The engine queries pending quantities across open POs (`APPROVED`, `ORDERED`, `PARTIAL_RECEIPT`). If open POs already satisfy forecasted demand and safety stock, duplicate reorder recommendations are prevented.
- **Lead-Time-Aware Clinical Urgency**: Priority is determined not just by static stock thresholds, but by comparing **Days of Stock Remaining** against supplier delivery lead times:
  $$\text{Days of Stock} = \frac{\text{Current Stock}}{\text{Forecast 30d} / 30}$$
  $$\text{Stockout Risk Gap} = \text{Days of Stock} - \text{Supplier Average Delivery Days}$$
  If the risk gap is negative (stock will deplete before the supplier can deliver), priority is escalated to `CRITICAL` with reason tag `STOCKOUT BEFORE DELIVERY (+Nd gap)`.

### Stage 3: Low-Stock Alerts & Notification Pipeline
- **Automated Alert Generation**: `inventory.services.generate_reorder_alerts()` triggers when stock crosses reorder level or safety stock thresholds.
- **Fingerprint Deduplication**: `notifications/service.py` computes SHA-256 fingerprints across recipient, title, and weekly period to prevent alert flooding.

### Stage 4: Restock Request Creation (Pharmacist)
- **Role**: Pharmacists initiate restock requests at `/purchases/request/new/`.
- **AI Recommendation Integration**: The endpoint accepts `?medicine_id=<id>` (in addition to `?alert_id=<id>`), automatically pre-filling recommended quantity, priority, and preferred supplier.

### Stage 5: Administrative Review & Conversion (Admin)
- **Role**: Administrators review pending requests at `/purchases/request/<pk>/approve/`.
- **Order Modification**: Admins can adjust quantities, select alternative suppliers based on price comparison matrices, or reject requests with audit notes.
- **Conversion to PO**: Approved requests are converted to formal Purchase Orders (`PurchaseOrder`) with status `APPROVED`.

### Stage 6: Purchase Order Dispatch & Live Delivery Tracking
- **PO Email Dispatch**: Admins can email POs directly to suppliers at `/purchases/<pk>/send-email/`. The transmission outcome is timestamped and recorded in `po.notes`, and status advances to `ORDERED`.
- **Live Delivery Tracking Dashboard**: Accessible under Procurement -> Delivery Tracking (`/purchases/tracking/`). Displays in-flight orders, supplier details, expected delivery dates, ordered vs. received quantities, delay flags (`Nd LATE`), and discrepancy counts.

### Stage 7: Physical Goods Receiving (GRN)
- **Receiving Interface**: Pharmacists record incoming shipments at `/purchases/po/<pk>/grn/new/`.
- **Barcode & QR Lookup**: An AJAX lookup endpoint (`/purchases/grn/barcode-lookup/`) identifies medicines and matches line items visually without touching stock.
- **Structured Rejections**: Pharmacists specify rejected quantities and select standardized rejection reasons (`DAMAGED_PACKAGING`, `EXPIRED_AT_ARRIVAL`, `BROKEN_COLD_CHAIN`, `SHORT_DELIVERY`, `WRONG_ITEM`).

### Stage 8: Atomic Verification & Stock Increase
- **Verification Engine**: `purchases.services.process_grn_receipt()` executes inside `@transaction.atomic`.
- **Rejection Expiry Validation**: If a batch has `expiry_date <= today`, verification aborts with `ValidationError`.
- **Stock Increment**: Only `quantity_accepted` is passed to `inventory.services.add_stock()`. A `PURCHASE` stock transaction is logged, `MedicineBatch` records are updated, and pending PO line quantities are recalculated.
- **Partial Receiving**: If received quantity is less than ordered quantity, the PO enters `PARTIAL_RECEIPT`. When all lines are fulfilled, status transitions to `COMPLETED`.

### Stage 9: Inventory Audit, Traceability & FEFO Allocation
- **Traceability**: Every batch links back to its originating GRN and PO for full regulatory and recall auditability.
- **FEFO Allocation**: Outgoing dispensing and sales automatically query active, non-expired batches ordered by `expiry_date ASC`.

---

## Test Suite & Verification Results

All 128 automated tests across the MediVault repository pass with zero errors:

| App / Test Suite | Tests | Result | Coverage Highlights |
|---|---|---|---|
| `purchases.test_supply_chain_workflow` | 17 | **PASSED** | Verified receiving, non-mutating PO/email, partial receipt, rejection exclusions, duplicate verification rejection, expired batch rejection, non-mutating barcode lookup, delivery tracking metrics, lead-time AI urgency, open PO awareness, PR to PO conversion, FEFO allocation. |
| `dispensing.tests` | 19 | **PASSED** | Centralized StockService integration, DISPENSE transaction types, multi-batch FEFO dispensing, prescription verification, controlled drug registers. |
| `inventory.tests` | 26 | **PASSED** | Stock adjustments, batch additions/deductions, alert generation, consumption calculation. |
| `sales.tests` | 12 | **PASSED** | Point-of-sale transactions, invoice generation, profit analytics. |
| `accounts.tests` | 17 | **PASSED** | Role-based authentication (Admin vs. Pharmacist), email verification, password reset. |
| `ai_engine.tests` | 11 | **PASSED** | Demand forecasting, accuracy evaluation, dead stock detection. |
| `integrations.tests` | 15 | **PASSED** | FHIR MedicationRequest endpoints, EHR simulator. |
| `reports.tests` | 11 | **PASSED** | Date range resolution, PDF/CSV export, consumption reports. |
| **Total** | **128** | **ALL PASSED** | `python manage.py test` — 0 errors, 0 failures. |

---

## Summary of Files Modified and Added

### Procurement & Goods Receiving
- `purchases/views.py`: Added `grn_barcode_lookup_api`, `send_po_email_view`, `delivery_tracking_view`, and `medicine_id` support in `create_purchase_request_view`.
- `purchases/urls.py`: Added routes for barcode lookup, delivery tracking, and PO email dispatch.
- `purchases/test_supply_chain_workflow.py`: Comprehensive 17-test suite for all supply chain acceptance criteria.
- `purchases/tests.py`: Exported workflow tests for runner discovery.

### Templates & UI
- `templates/purchases/delivery_tracking.html`: In-flight PO tracking dashboard with metrics, delay badges, and receive actions.
- `templates/purchases/create_grn.html`: Added non-mutating barcode/medicine scanner with AJAX lookup and item highlighting.
- `templates/purchases/grn_detail.html`: Added structured rejection categories dropdown for physical verification.
- `templates/purchases/po_detail.html`: Added "Email PO to Supplier" action button for administrators.
- `templates/navbar.html`: Added "Delivery Tracking" link in the Procurement dropdown.

### AI Engine
- `ai_engine/intelligence_service.py`: Refactored `get_reorder_recommendations()` to calculate clinical urgency using Days of Stock Remaining vs. Supplier Lead Time, with `stockout_risk_days` and `days_of_stock_remaining` metrics.

### Dispensing & Stock Core
- `dispensing/services.py`: Fixed direct batch quantity modification; now routes through `inventory_services.remove_stock(type=DISPENSE)`.
- `inventory/services.py`: `add_stock` and `remove_stock` return created `txn` instance.
