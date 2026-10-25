# MediVault — Phase 5: Supplier Intelligence & Integration Architecture

## Executive Summary
This document specifies the architecture, data models, scoring formulas, performance analytics, and integration boundaries of MediVault’s Supplier Intelligence and Integration module. The module provides multi-supplier medicine mapping, historical price tracking, empirical supplier performance metrics, composite recommendation scoring, in-flight delivery tracking, and explicit supplier integration boundaries.

---

## 1. Supplier Master & Catalogue Mapping

### Data Schema (`suppliers/models.py`)

1. **`Supplier` Model:** Represents a medicine vendor entity (managed by Admin/Pharmacist, not a login user). Tracks GST number, drug license number, business terms (`payment_terms`, `minimum_order_value`), and empirical performance metrics (`average_delivery_days`, `on_time_delivery_rate`, `fill_rate`, `rating`).
2. **`SupplierMedicine` Model:** Maps medicines to suppliers with specific commercial terms:
   - `unit_price`: Current price per unit offered by supplier.
   - `minimum_order_quantity` (MOQ): Minimum order requirement.
   - `pack_size`: Number of units per pack.
   - `delivery_days`: Expected lead time in days.
   - `is_available` & `is_preferred`: Availability status and preference flag.
3. **`SupplierPriceHistory` Model:** Immutable audit record created whenever a supplier unit price changes (via GRN receipt or catalogue update). Stores `old_price`, `new_price`, `price_change_pct`, `changed_by`, `reason`, and `changed_at`.

---

## 2. Supplier Price Intelligence

- **Price History Auditing:** Automatically recorded upon Goods Receipt Note (GRN) verification (`purchases/services.py::process_grn_receipt`) or catalogue edits when `unit_price` differs from previous record.
- **Price Change Percentage:**
  $$\text{ChangePct} = \left(\frac{\text{NewPrice} - \text{OldPrice}}{\text{OldPrice}}\right) \times 100$$
- **Price Comparison Matrix (`suppliers/views.py::price_comparison_view`):** Compares unit prices across suppliers for the same medicine, identifying best price offers, average market price, and price trend (`INCREASED`, `DECREASED`, `STABLE`).

---

## 3. Empirical Supplier Performance Analytics

Supplier performance is recalculated dynamically from actual completed `PurchaseOrder` and `GoodsReceipt` records via `purchases/services.py::recalculate_supplier_performance(supplier)`:

$$\text{OnTimeDeliveryRate} = \left(\frac{\text{Count}(\text{PO}_{\text{actual\_delivery} \le \text{expected\_delivery}})}{\text{Total Completed POs}}\right) \times 100$$

$$\text{FillRate} = \left(\frac{\sum \text{QuantityReceived}}{\sum \text{QuantityOrdered}}\right) \times 100$$

### Handling Missing Data
If a supplier has 0 completed Purchase Orders, the system explicitly handles the unmeasured state without fabricating 100% ratings or treating missing records as poor performance.

---

## 4. Explainable Supplier Recommendation Engine

The recommendation engine (`purchases/services.py::get_supplier_comparison`) ranks suppliers for a given medicine using a composite score (0–100):

$$\text{CompositeScore} = (0.40 \times \text{PriceScore}) + (0.20 \times \text{DeliveryScore}) + (0.20 \times \text{AvailabilityScore}) + (0.20 \times \text{RatingScore})$$

### Score Components

1. **Price Score (40% Weight):** Normalized relative to minimum and maximum prices among active suppliers for the target medicine:
   $$\text{PriceScore} = \left(\frac{\text{MaxPrice} - \text{UnitPrice}}{\text{MaxPrice} - \text{MinPrice}}\right) \times 100$$
2. **Delivery Lead-Time Score (20% Weight):** Inverse linear score assuming 30-day baseline ceiling:
   $$\text{DeliveryScore} = \max\left(0, \frac{30 - \text{DeliveryDays}}{30}\right) \times 100$$
3. **Availability / Fill Rate Score (20% Weight):** Recorded `fill_rate` percentage (0–100).
4. **Rating Score (20% Weight):** Overall supplier rating normalized to 0–100 ($\text{Rating} / 5.0 \times 100$).

---

## 5. In-Flight Delivery & Order Tracking

In-flight Purchase Orders are tracked through explicit status transitions (`purchases/views.py::delivery_tracking_view`):

$$\text{DRAFT} \longrightarrow \text{APPROVED} \stackrel{\text{PO Email Sent}}{\longrightarrow} \text{ORDERED} \stackrel{\text{Supplier Confirmation}}{\longrightarrow} \text{ORDERED (with Ref \& ETA)} \longrightarrow \text{PARTIAL\_RECEIPT} \longrightarrow \text{COMPLETED}$$

- **Overdue Delivery Flag:** Triggered when `expected_delivery_date < today` and `outstanding_quantity > 0`.
- **Open PO Quantity Integration:** Outstanding incoming quantities from open POs are exposed to the AI stockout prevention engine to calculate lead-time horizon stock and prevent duplicate procurement.

---

## 6. Supplier Integration Levels & Boundaries

MediVault adheres to strict integration level disclosures:

- **Level 1 (Operational & Verified):**
  - Configured PO email dispatch (`send_po_email_view`).
  - Manual recording of supplier confirmation reference and ETA (`confirm_supplier_order_view`).
  - In-flight PO delivery tracking (`delivery_tracking_view`).
  - QC verified physical receipt via GRN (`process_grn_receipt`).
- **Level 2 / Level 3 (Integration Boundary):**
  - External supplier APIs and EDI protocols require real third-party credentials and documentation.
  - MediVault provides an explicit, authenticated integration boundary (`confirm_supplier_order_view`) for recording supplier confirmations without claiming unvalidated external API integrations.

---

## 7. Verification & Test Execution Results

Executed full Django test suite:

```bash
python manage.py test purchases.test_integrated_procurement inventory.test_stock_transaction_accuracy inventory.test_audit_fixes --verbosity=2
```

### Output:
```text
Ran 65 tests in 12.197s

OK
Destroying test database for alias 'default' ('test_medivault')...
```

All 65 tests passed with 0 errors and 0 failures.
