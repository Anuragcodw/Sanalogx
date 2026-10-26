# MEDIVAULT — INVENTORY & PROCUREMENT WORKFLOW AUDIT REPORT

**Document Version:** 1.0.0  
**Project:** MediVault — Hospital Pharmacy Supply Chain & Stock Optimization  
**System Environment:** Django 5.x, MySQL / SQLite (Test Mode), Python 3.12+  
**Audit Scope:** End-to-end Inventory, Dispensing, Consumption Analytics, Smart Alerts, AI Restocking, Procurement, Goods Receiving (GRN), and Supplier Tracking  
**Test Suite Status:** 136 / 136 Tests Passing (100% Pass Rate)

---

## 1. Executive Summary

This audit and remediation were conducted to inspect, validate, harden, and complete the hospital pharmacy supply chain in **MediVault** without rebuilding the system or breaking existing architecture. 

### Key Findings & Architectural Remediation Summary

| Area | Issue Identified During Audit | Resolution & Architectural Fix | Status |
|---|---|---|---|
| **Dispensing Stock Bypass** | Direct quantity decrement in dispensing bypassed audit logging | Routed through `inventory.services.remove_stock()` using transaction type `DISPENSE` | **RESOLVED** |
| **Manual Batch Add Bypass** | `medicines/views.py::batch_add_view` set `batch.quantity_available = batch.quantity_received` directly, skipping `StockTransaction` creation | Refactored to initialize available stock to 0 and invoke `inventory_services.add_stock(..., transaction_type=PURCHASE)` with audit reference `MANUAL-BATCH-...` | **RESOLVED** |
| **Race Conditions in Stock Mutations** | Concurrent stock transactions could read stale inventory and cause overselling or negative stock | Wrapped `add_stock()` and `remove_stock()` in `MedicineBatch.objects.select_for_update()` row locks within `@transaction.atomic` | **RESOLVED** |
| **Consumption Calculation Accuracy** | Cancelled sales (`Sale.SaleStatus.CANCELLED`) were counted as valid clinical consumption | Updated `calculate_net_consumption_in_range()` to query cancelled sale identifiers and filter out cancelled sale transactions | **RESOLVED** |
| **Alert Overdue PO Filter Bug** | `check_and_notify_overdue_orders` queried non-existent status `'SENT'` and invalid URL `/purchases/po/{id}/` | Fixed status filtering to `['APPROVED', 'ORDERED', 'PARTIAL_RECEIPT']` and corrected URL to `/purchases/{id}/` | **RESOLVED** |
| **Notification Email Tracking** | `_send_email_alert_for_notifications` marked `email_sent=True` even when `send_mail` failed or raised an exception | Removed `fail_silently=True`, verified `sent_count > 0`, and set `email_sent=False` upon failure | **RESOLVED** |
| **Lead-Time-Aware Restock Alerting** | Low-stock checks relied only on static thresholds, causing false alarms even when replacement stock was in transit | Enhanced `check_and_notify_low_stock` with Days of Stock Remaining, supplier delivery lead times, and open PO quantity awareness | **RESOLVED** |
| **Supplier PO Dispatch** | No real or simulated supplier API existed; risking fake webhook illusions | Implemented clean, verifiable email-based PO dispatch (`send_po_email_view`) using standard Django email backend and audit notes | **RESOLVED** |
| **PO Delivery Tracking** | Admins had no UI to log carrier updates or revised ETAs | Added tracking update POST handler to `po_detail_view` and created interactive Tracking & Audit Log card in `po_detail.html` | **RESOLVED** |

---

## 2. End-to-End Architecture & Data Flow

MediVault enforces a unidirectional, auditable supply chain flow where every physical stock movement is backed by a verified business event:

```
[Supplier Catalog / Medicine Need]
               │
               ▼
   [AI Reorder Recommendation]
               │
               ▼
      [Purchase Request] (Pharmacist)
               │
               ▼
       [Admin Approval] ────► [Purchase Order Created]
                                       │
                                       ▼
                       [Order Placed (Email to Supplier)]
                                       │
                                       ▼
                       [Delivery Tracking / ETA Updates]
                                       │
                                       ▼
                      [Physical Delivery at Pharmacy]
                                       │
                                       ▼
                       [Barcode Scan & Invoice Match]
                                       │
                                       ▼
                     [Goods Receipt Note (GRN) Draft]
                                       │
                    ┌──────────────────┴──────────────────┐
                    ▼                                     ▼
        [Inspection Accepted]                 [Damaged / Expired Rejected]
                    │                                     │
                    ▼                                     ▼
        [Pharmacist Verification]              [Recorded in GRN Only;]
                    │                          [Never Added to Stock]
                    ▼
       [StockService.add_stock()]
         - select_for_update()
         - StockTransaction (PURCHASE)
         - Inventory Totals Updated
                    │
                    ▼
     [FEFO Dispensing & Sales] ────► [Central StockService.remove_stock()]
                    │                 - StockTransaction (DISPENSE/SALE)
                    │
                    ▼
    [Consumption & Health Analytics]
         - Net = Gross - Returns (Excludes Cancelled Sales)
         - Lead-Time & Stockout Suppression Detection
                    │
                    ▼
     [Lead-Time-Aware Alerts & AI] ─── (Closes Supply Chain Loop)
```

---

## 3. Inventory & Receiving Audit

### 3.1 Strict GRN Verification Gate
- **No Early Stock Addition:** Creating a PO or drafting a GRN does not increment physical or available inventory. Stock is added strictly when an authorized pharmacist or admin executes `process_grn_receipt(grn_id, verified_by)`.
- **Duplicate Prevention:** GRN verification checks `grn.status == PENDING_VERIFICATION`. A GRN cannot be verified twice.
- **Rejection Isolation:** Only accepted quantities (`quantity_accepted`) are routed to `StockService.add_stock()`. Damaged, missing, or rejected items remain logged on the GRN item record with structured rejection reasons (`DAMAGED_PACKAGING`, `NEAR_EXPIRY`, `WRONG_ITEM`, etc.) and are never added to active inventory.
- **Atomic Expiry Protection:** Batches with past expiry dates are rejected before any database commit, guaranteeing expired products never enter active hospital stock.

### 3.2 Barcode Scanning Safety
- The endpoint `/purchases/grn/barcode-lookup/` operates as a read-only metadata helper (`@require_GET`). It matches barcode strings against `Medicine.barcode` or active `MedicineBatch.batch_number` without performing any database mutations.

### 3.3 Concurrency & Row-Level Locking
- Both `inventory.services.add_stock()` and `inventory.services.remove_stock()` are decorated with `@transaction.atomic` and invoke `MedicineBatch.objects.select_for_update().get(pk=batch.pk)`.
- Prevents race conditions during concurrent dispensing, sales, or receipt operations.
- Direct quantity decrement bypasses in `dispensing/services.py` and `medicines/views.py` were eliminated.

---

## 4. Dispensing & Consumption Audit

### 4.1 Centralized FEFO Stock Dispensing
- All dispensing workflows utilize `inventory.services.allocate_stock(medicine, quantity, policy='FEFO')` followed by `inventory.services.remove_stock(..., transaction_type=StockTransaction.TransactionType.DISPENSE)`.
- Batches nearest to expiration are allocated and consumed first.
- Every dispensing event records prescription number, doctor, patient identifier, and pharmacist user.

### 4.2 Accurate Net Consumption Engine
The net clinical consumption calculation in `inventory/consumption_service.py` adheres to strict mathematical integrity:
1. **Deduplication:** When a dispensing record and a sale record share the same clinical reference number, it is counted once to prevent double-counting.
2. **Cancelled Sales Exclusion:** The query inspects `Sale.objects.filter(status=Sale.SaleStatus.CANCELLED)` and explicitly skips voided sale numbers.
3. **Return Offsets:** Customer returns (`RETURN` transactions) offset outbound consumption for the period.
4. **Non-Demand Segregation:** Expiry, damage write-offs, and administrative adjustments are recorded separately and excluded from clinical demand forecasts.

---

## 5. Smart Low-Stock & AI Restocking Audit

### 5.1 Dynamic Lead-Time-Aware Alerting
Static minimum thresholds alone are insufficient in hospital environments. `notifications/service.py::check_and_notify_low_stock` evaluates:
- **Days of Stock Remaining (DoSR):** $\text{DoSR} = \frac{\text{Current Stock}}{\text{Average Daily Demand}}$.
- **Lead-Time Buffer:** Compares DoSR against the primary supplier's delivery lead time ($\text{Lead Days}$).
- **Stockout Gap:** If $\text{DoSR} \le \text{Lead Days}$, stockout will occur before a newly placed order could arrive.
- **Open PO Awareness:** Queries pending purchase orders in `APPROVED`, `ORDERED`, or `PARTIAL_RECEIPT` status. If incoming supply is already in transit to cover the gap, alert severity is adjusted to prevent panic ordering.

### 5.2 Clinical Urgency vs. Regulatory Classification
- **Regulatory Category $\ne$ Clinical Criticality:** MediVault does not treat every Schedule H or H1 drug as clinically critical. Regulatory categories define legal dispensing safeguards (prescription required, register logging). Clinical priority is determined by patient impact and stockout risk:
  - `CRITICAL`: Days of stock remaining $\le$ supplier lead time, or stockout gap $> 0$ with no incoming PO.
  - `HIGH`: Days of stock remaining within $1.5 \times$ lead time.
  - `NORMAL`: Standard replenishment buffer.

---

## 6. Supplier Delivery Tracking & PO Workflow

### 6.1 PO Lifecycle States
MediVault enforces a rigorous purchase order state machine:
$$\text{DRAFT} \longrightarrow \text{SUBMITTED} \longrightarrow \text{APPROVED} \longrightarrow \text{ORDERED} \longrightarrow \text{PARTIAL\_RECEIPT} \longrightarrow \text{COMPLETED}$$
*(or $\text{CANCELLED}$ at any pre-receipt stage without modifying stock)*

### 6.2 Realistic Supplier Integration (No Fake APIs)
- MediVault uses an email-based supplier communication pipeline:
  - Generates official Purchase Order documents with PO number, vendor contact, itemized line items, pack sizes, and agreed prices.
  - Dispatched via standard Django `send_mail` with delivery tracking confirmation.
  - Email failures are handled gracefully without aborting the procurement transaction.

### 6.3 Delivery Tracking & Audit Trail
- **Admin Tracking View:** Accessible under `Procurement` $\rightarrow$ `Delivery Tracking` (`/purchases/tracking/`), tracking all in-flight orders, supplier performance, and delay indicators.
- **Audit Logging in `po.notes`:** Admins can record dispatch notes, carrier references (AWB numbers), and revised delivery dates via the updated `po_detail.html` tracking interface.

---

## 7. Role-Based Access Control & Security

| Operation | Pharmacist | Admin | System / AI |
|---|:---:|:---:|:---:|
| Create Purchase Request (PR) | Yes | Yes | Suggest Only |
| Approve Purchase Request | No | Yes | No |
| Create Purchase Order (PO) | Yes | Yes | No |
| Approve PO & Mark ORDERED | No | Yes | No |
| Email PO to Supplier | No | Yes | No |
| Receive Goods & Verify GRN | Yes | Yes | No |
| Update PO Tracking Notes | No | Yes | No |
| Dispense Medication (FEFO) | Yes | Yes | No |
| Cancel Purchase Order | No | Yes | No |

- All POST endpoints are protected by Django CSRF tokens.
- Strict role decorators (`@admin_required`, `@pharmacist_or_admin_required`) protect views from unauthorized access.

---

## 8. Test Verification Matrix (136 / 136 Tests Passing)

All test suites were executed on the test database (`test_medivault`) with 0 failures and 0 errors:

| Test Module | Coverage Area | Tests Run | Result |
|---|---|:---:|:---:|
| `purchases/test_supply_chain_workflow.py` | End-to-end PO lifecycle, GRN verification, FEFO allocation, non-mutating barcode lookup, partial receipts, rejection handling, cancellation stock safety, lead-time AI urgency | 17 | **PASSED** |
| `inventory/test_audit_fixes.py` | Cancelled sales exclusion, return consumption offsets, notification fingerprint dedup, email failure logging, stock validation, and batch_add StockService integration | 8 | **PASSED** |
| `accounts` | User authentication, registration approval, password resets, role guards | 26 | **PASSED** |
| `ai_engine` | Demand forecasting, cold-start fallbacks, supplier intelligence, voice/chat intent matching | 24 | **PASSED** |
| `dispensing` | Prescription validation, pharmacist verification, FEFO batch allocation | 15 | **PASSED** |
| `integrations` | FHIR medication request mock APIs and serialization | 6 | **PASSED** |
| `medicines` | Medicine catalog, dosage forms, batch management, validation | 12 | **PASSED** |
| `notifications` | Notification dispatch, fingerprint deduplication, status transitions | 10 | **PASSED** |
| `reports` | Consumption, expiry, financial, and inventory health reporting | 10 | **PASSED** |
| `sales` | Billing, payment processing, invoice generation, returns | 8 | **PASSED** |
| **Total** | **Full System Regression Suite** | **136** | **100% PASS** |

---

## 9. Conclusion

The MediVault hospital pharmacy supply chain and stock optimization system is robust, auditable, and resilient. All stock movements flow through the centralized `StockService` with row-level locks, ensuring full data consistency, clinical safety, and regulatory compliance.
