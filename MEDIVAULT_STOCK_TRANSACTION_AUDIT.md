# MediVault — Stock Transaction Accuracy & Audit Architecture

## Executive Summary
This document records the comprehensive audit, refactoring, hardening, and empirical verification of MediVault’s inventory and physical stock transaction ecosystem. All physical stock modifications across receiving, dispensing, sales, returns, damage write-offs, expiry write-offs, and stock reconciliation have been audited and strictly centralized through the core `StockService` (`inventory/services.py`).

---

## 1. Scope & Audited Pathways

The audit inspected every module, view, model, and service method interacting with `MedicineBatch.quantity_available`, `MedicineBatch.quantity_received`, and `Inventory.current_stock`:

| Pathway / Module | Entry Points Audited | Centralization Status & Resolution |
|---|---|---|
| **Goods Receiving** | `purchases/services.py::process_grn_receipt` | Routes through `inventory_services.add_stock(PURCHASE)`. Increment of `quantity_received` strictly scoped to `PURCHASE` transactions. Idempotent GRN check prevents double receiving. |
| **Sales Billing & Checkout** | `sales/services.py::create_sale_from_cart` | Allocates batches using `allocate_stock(FEFO)` and deducts physical stock using `remove_stock(SALE)`. |
| **Sales Returns** | `sales/services.py::process_sale_return` | Restores physical stock using `add_stock(RETURN)`. `quantity_received` is NOT incremented on return. |
| **Dispensing (Standard)** | `dispensing/services.py::dispense_request_item` | Deducts physical stock using `inventory_services.remove_stock(DISPENSE)`. |
| **Dispensing (Secure / QR)** | `dispensing/secure_dispensing_service.py::execute_secure_dispensing` | **Fixed direct bypass.** Refactored from direct batch field mutations to calling `inventory_services.remove_stock(DISPENSE, transaction_number=tx_ref)`. |
| **Dispensing Reversals & Returns** | `dispensing/secure_dispensing_service.py::reverse_dispensing`, `dispensing/services.py::record_medicine_return` | **Fixed direct bypass.** Refactored from raw batch updates to calling `inventory_services.record_return(..., transaction_number=tx_ref)`. |
| **Batch Stock Reconciliation** | `dispensing/secure_dispensing_service.py::reconcile_batch_stock` | **Fixed direct bypass.** Refactored from raw set to calling `inventory_services.adjust_stock(..., transaction_number=tx_ref)`. |
| **Batch Management UI** | `medicines/views.py::batch_edit_view` | **Hardened against direct stock editing.** Created `BatchEditForm` which excludes `quantity_available` and `quantity_received` from form input fields. Adjustments must use Stock Adjustment workflows. |
| **Damage Write-off** | `inventory/services.py::record_damage` | Centralized method with `select_for_update()` row locking, validating bounds before deducting. |
| **Expiry Write-off** | `inventory/services.py::record_expiry` | Centralized method with `select_for_update()` row locking, setting batch status to `EXPIRED` and deducting stock. |
| **Stock Adjustments** | `inventory/services.py::adjust_stock` | Centralized method supporting Increase/Decrease adjustments with reason logging and row-level locks. |
| **Read-only Verification Scans** | `dispensing/secure_dispensing_service.py::scan_barcode_or_qr`, `sales/views.py::barcode_scan_sale_api` | Confirmed 100% read-only. No stock mutations or transaction creation occur during scans. |

---

## 2. Shared Service Signatures & Rules

All physical stock changes occur within atomic transactions (`@transaction.atomic`) with database row locks (`select_for_update()`) on `MedicineBatch`.

### `inventory/services.py` Core API:

```python
add_stock(medicine, batch, quantity, transaction_type, performed_by, reference_number="", notes="", transaction_number="")
remove_stock(medicine, batch, quantity, transaction_type, performed_by, reference_number="", notes="", transaction_number="")
adjust_stock(medicine, batch, adjustment_type, quantity, performed_by, reason, notes="", transaction_number="")
record_damage(medicine, batch, quantity, performed_by, reason, notes="", transaction_number="")
record_expiry(medicine, batch, quantity, performed_by, notes="", transaction_number="")
record_return(medicine, batch, quantity, performed_by, reason="", reference_number="", notes="", transaction_number="")
allocate_stock(medicine, quantity_needed, policy='FEFO')  # Read-only calculation
```

### Business Rules Enforced:

1. **Strict Non-Negative Bounds:** `remove_stock`, `record_damage`, and `adjust_stock(Decrease)` raise `ValidationError` if requested quantity exceeds `batch.quantity_available`.
2. **Clinical Batch Status Guard:** `remove_stock` blocks `SALE` and `DISPENSE` operations on batches where `expiry_date <= today` or status is `EXPIRED`, `QUARANTINED`, or `RETURNED`.
3. **Scoped `quantity_received` Increments:** `batch.quantity_received` is incremented **only** when `transaction_type == StockTransaction.TransactionType.PURCHASE`. `RETURN` transactions do NOT increment `quantity_received`.
4. **Audit Traceability:** Every transaction records a unique `transaction_number` (`TX-ADD-...`, `TX-REM-...`, `TX-ADJ-...`, etc.), along with `previous_stock`, `new_stock`, `performed_by`, and `reference_number`.
5. **Record Deletion Protection:** Completed `Sale` records (`COMPLETED`, `PARTIAL_RETURN`) and `DispensingRecord` entries (`CONFIRMED`, `COMPLETED`) override `.delete()` to raise a `ValidationError`, preventing silent record erasure.

---

## 3. Modified & Created Files Summary

| File Path | Description of Changes |
|---|---|
| `inventory/services.py` | Hardened StockService: unique `transaction_number` generation, `select_for_update()` locks on all write methods, clinical status guards in `remove_stock`, `quantity_received` scoped to `PURCHASE`, added canonical `record_return` wrapper. |
| `dispensing/services.py` | Refactored `record_medicine_return` to route through `inventory_services.record_return`. |
| `dispensing/secure_dispensing_service.py` | Refactored `execute_secure_dispensing`, `reverse_dispensing`, and `reconcile_batch_stock` to route through centralized `StockService` methods (`remove_stock`, `record_return`, `adjust_stock`). |
| `sales/models.py` | Added `Sale.delete()` override preventing silent deletion of completed/partially returned sales. |
| `dispensing/models.py` | Added `DispensingRecord.delete()` override preventing silent deletion of confirmed/completed dispensing records. |
| `medicines/forms.py` | Created `BatchEditForm` excluding `quantity_available` and `quantity_received` from direct editing. |
| `medicines/views.py` | Updated `batch_edit_view` to use `BatchEditForm`, preventing direct manual batch quantity overwrites. |
| `inventory/test_stock_transaction_accuracy.py` | Created comprehensive 27-scenario acceptance test suite validating stock transaction accuracy. |
| `MEDIVAULT_STOCK_TRANSACTION_AUDIT.md` | Audit deliverable documenting findings, signatures, rules, and test results. |

---

## 4. Test Suite Execution & Results

The test suite (`inventory.test_stock_transaction_accuracy` and `inventory.test_audit_fixes`) was executed using Django's test runner.

### Command Executed:
```bash
python manage.py test inventory.test_stock_transaction_accuracy inventory.test_audit_fixes --verbosity=2
```

### Results:
```text
Ran 44 tests in 7.788s

OK
Destroying test database for alias 'default' ('test_medivault')...
```

### Detailed Coverage Matrix:

| Test ID | Test Name | Target Behavior | Result |
|---|---|---|---|
| **A1** | `test_receiving_adds_stock_and_creates_purchase_transaction` | GRN receipt creates `PURCHASE` txn & updates stock | **PASS** |
| **A2** | `test_sale_deducts_stock_with_sale_type` | Sale deducts batch stock via `remove_stock(SALE)` | **PASS** |
| **A3** | `test_dispense_deducts_stock_with_dispense_type` | Dispense deducts stock via `remove_stock(DISPENSE)` | **PASS** |
| **A4** | `test_return_restores_stock_with_return_type` | Return restores stock via `record_return` | **PASS** |
| **A5** | `test_damage_deducts_stock_with_damage_type` | Damage write-off deducts via `record_damage(DAMAGE)` | **PASS** |
| **A6** | `test_expiry_deducts_stock_marks_batch_expired` | Expiry write-off deducts & sets status `EXPIRED` | **PASS** |
| **A7** | `test_increase_adjustment_creates_adjustment_transaction` | Increase adjustment creates `ADJUSTMENT` txn | **PASS** |
| **A8** | `test_decrease_adjustment_creates_adjustment_transaction` | Decrease adjustment creates `ADJUSTMENT` txn | **PASS** |
| **A9** | `test_fefo_picks_earliest_expiry_first` | FEFO allocation prioritizes earliest expiry batch | **PASS** |
| **A10** | `test_allocation_spans_two_batches_in_fefo_order` | Multi-batch FEFO allocation spans batches in order | **PASS** |
| **A11** | `test_A11_remove_stock_raises_on_over_deduction` | `remove_stock` blocks deduction > available | **PASS** |
| **A12** | `test_A12_record_damage_raises_on_over_deduction` | `record_damage` blocks write-off > available | **PASS** |
| **A13** | `test_A13_adjust_stock_decrease_raises_on_over_deduction` | `adjust_stock(Decrease)` blocks drop > available | **PASS** |
| **A14** | `test_expired_batch_blocked_from_sale` | Expired batch blocked from `SALE` and `DISPENSE` | **PASS** |
| **A15** | `test_quarantined_batch_blocked_from_dispense` | Quarantined batch blocked from `DISPENSE` | **PASS** |
| **A16** | `test_all_transaction_numbers_are_unique` | All generated `transaction_number`s are unique | **PASS** |
| **A17** | `test_return_does_not_increment_quantity_received` | `RETURN` transaction does NOT change `quantity_received` | **PASS** |
| **A18** | `test_duplicate_grn_raises_validation_error` | Re-processing same GRN raises `ValidationError` | **PASS** |
| **A19** | `test_completed_sale_cannot_be_deleted` | `.delete()` on completed sale raises error | **PASS** |
| **A20** | `test_completed_dispensing_record_cannot_be_deleted` | `.delete()` on completed dispensing raises error | **PASS** |
| **A21** | `test_cancelled_sale_not_counted_in_net_consumption` | Cancelled sale excluded from consumption query | **PASS** |
| **A22** | `test_return_reduces_net_consumption` | Patient return correctly subtracted from net consumption | **PASS** |
| **A23** | `test_batch_edit_form_excludes_quantity_available` | `quantity_available` excluded from edit form fields | **PASS** |
| **A24** | `test_batch_edit_form_excludes_quantity_received` | `quantity_received` excluded from edit form fields | **PASS** |
| **A25** | `test_record_return_creates_return_transaction_not_purchase` | `record_return` creates `RETURN` txn (no `PURCHASE`) | **PASS** |
| **A26** | `test_failed_remove_leaves_no_orphan_transaction` | Atomic rollback leaves no orphan `StockTransaction` | **PASS** |
| **A27** | `test_inventory_accurate_after_complex_workflow` | 5-step workflow inventory & batch balance accurate | **PASS** |

---

## 5. Verification & Conclusion

1. **System Check:** `python manage.py check` passed with 0 issues.
2. **Transaction Integrity:** Every physical inventory mutation in MediVault is guaranteed to go through the central `StockService`, ensuring row-level lock safety, FEFO allocation order, clinical batch status enforcement, and non-delete audit history.
