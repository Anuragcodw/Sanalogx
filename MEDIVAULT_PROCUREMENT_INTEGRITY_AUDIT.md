# MediVault — Integrated Procurement Workflow & Integrity Audit

## Executive Summary
This document records the architectural audit, refactoring, and empirical verification of MediVault’s Integrated Procurement Workflow. The complete procurement lifecycle—from Restock Requests and Admin Approvals to Purchase Orders, Email Dispatches, Supplier Confirmations, and Verified Goods Receiving (GRN)—has been audited, hardened, and verified to ensure 100% physical stock integrity and zero unverified inventory mutations.

---

## 1. Procurement Lifecycle & Status Transitions

```
[Pharmacist Restock Request] 
       │ (status: PENDING_APPROVAL)
       ▼
[Admin Review & Approval] 
       │ ──► REJECTED (status: REJECTED, reason logged)
       ▼ (status: APPROVED)
[Convert to Purchase Order] 
       │ (PO status: APPROVED)
       ▼
[Email Dispatch to Supplier] 
       │ (PO status: ORDERED, email_status: SENT / FAILED)
       ▼
[Supplier Confirmation & ETA] 
       │ (records confirmation reference, confirmed ETA, supplier_confirmed_at)
       ▼
[Goods Receiving Note (GRN)] 
       │ (GRN status: PENDING_VERIFICATION)
       ▼
[QC Verification & Physical Receipt] 
       │ (calls StockService.add_stock(PURCHASE) inside transaction.atomic())
       ▼
[Physical Stock Updated & PO Completed/Partial]
```

### Lifecycle Status Matrix

| Phase | Entry Point / View | Allowed Status Transitions | Physical Stock Effect |
|---|---|---|---|
| **1. Restock Request** | `purchases/views.py::create_purchase_request_view` | `DRAFT` → `PENDING_APPROVAL` | **NONE** (No stock change) |
| **2. Admin Review** | `purchases/views.py::approve_purchase_request_view` | `PENDING_APPROVAL` → `APPROVED` or `REJECTED` | **NONE** (No stock change) |
| **3. PO Conversion** | `purchases/services.py::convert_pr_to_po` | `PRStatus`: `CONVERTED_TO_PO`<br>`POStatus`: `APPROVED` | **NONE** (No stock change) |
| **4. Email Dispatch** | `purchases/views.py::send_po_email_view` | `APPROVED` → `ORDERED`<br>`email_status`: `SENT` / `FAILED` | **NONE** (No stock change; dispatch != confirmation) |
| **5. Supplier Confirmation** | `purchases/views.py::confirm_supplier_order_view` | Status remains `ORDERED`<br>Populates `supplier_confirmation_reference`, `supplier_confirmed_eta` | **NONE** (No stock change) |
| **6. Goods Receipt Note** | `purchases/views.py::create_grn_view` | `GRNStatus`: `PENDING_VERIFICATION` | **NONE** (No stock change before verification) |
| **7. QC & Verification** | `purchases/services.py::process_grn_receipt` | `GRNStatus`: `VERIFIED` / `REJECTED`<br>`POStatus`: `PARTIAL_RECEIPT` / `COMPLETED` | **UPDATED** via `StockService.add_stock(PURCHASE)` inside `@transaction.atomic()` |

---

## 2. Integrity Rules & Safeguards Enforced

1. **Strict Stock Mutation Boundary:** PO creation, Admin approval, email dispatch, and supplier confirmation **never** mutate `MedicineBatch.quantity_available` or `Inventory.current_stock`. Physical stock is incremented only upon QC verification of accepted GRN quantities via `StockService.add_stock(PURCHASE)`.
2. **Duplicate Active Request Prevention:** If a pharmacist or admin submits a restock request for a medicine that already has an active (`DRAFT` or `PENDING_APPROVAL`) request, the system updates the existing request's quantity instead of creating duplicate PR records.
3. **Email Transmission vs. Supplier Confirmation:** Dispatching a PO via email updates `email_status = SENT` and transitions PO status to `ORDERED`. It does NOT populate `supplier_confirmation_reference` or `supplier_confirmed_at`, which require authentic supplier response or manual recording of confirmation details.
4. **Idempotent GRN Verification:** `process_grn_receipt()` checks `if grn.status != PENDING_VERIFICATION: raise ValidationError`. Re-verifying an already processed GRN is strictly blocked.
5. **Atomic Transaction Rollback:** If any line item in a Goods Receipt fails validation (e.g. expired batch, negative price, invalid quantity), the entire transaction rolls back using Django's `@transaction.atomic` and row locking (`select_for_update()`). No partial or orphan batches remain.

---

## 3. Key Files & Components

| File | Primary Responsibility |
|---|---|
| `purchases/models.py` | Models for `PurchaseRequest`, `PurchaseRequestItem`, `PurchaseOrder`, `PurchaseOrderItem`, `GoodsReceipt`, `GoodsReceiptItem`. |
| `purchases/services.py` | Core procurement service layer: `convert_pr_to_po()`, `process_grn_receipt()`, `recalculate_supplier_performance()`. |
| `purchases/views.py` | Workflows for PR submission, Admin approval/rejection, direct PO creation, PO email dispatch, supplier confirmation, GRN creation, and GRN verification. |
| `purchases/test_integrated_procurement.py` | Comprehensive test suite covering procurement workflow integrity, status transitions, duplicate prevention, and GRN verification. |

---

## 4. Verification & Test Execution Results

Executed combined test suite across procurement and stock accuracy modules:

```bash
python manage.py test purchases.test_integrated_procurement inventory.test_stock_transaction_accuracy inventory.test_audit_fixes --verbosity=2
```

### Result:
```text
Ran 61 tests in 11.742s

OK
Destroying test database for alias 'default' ('test_medivault')...
```

All 61 tests passed with 0 errors and 0 failures.
