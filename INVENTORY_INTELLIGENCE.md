# MediVault — Intelligent Pharmacy Inventory Automation & Optimization Platform
## Architecture & Technical Reference Guide

---

### Executive Overview

MediVault's **Inventory Automation & Optimization Platform** represents an enterprise-grade intelligence layer seamlessly integrated into the core Django/MySQL hospital pharmacy architecture.

The platform provides mathematically sound, database-connected predictive analytics and automated replenishment workflows without breaking or altering any existing Phase 1–7 operational pipelines (Dispensing, Sales, Purchase Orders, FEFO, Barcode/QR, or User Authentication).

---

## 1. Core Platform Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           PRESENTATION LAYER                                │
│  Inventory Dashboard │ Consumption Analytics │ AI Recommendation Center     │
│  Medicine Details (Tabs) │ Reports Center │ AI Chatbot & MediVoice Widget   │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
┌──────────────────────────────────────▼──────────────────────────────────────┐
│                            INTELLIGENCE LAYER                               │
│  ┌────────────────────────┐ ┌────────────────────────┐ ┌──────────────────┐ │
│  │   Consumption Engine   │ │ Inventory Health Engine│ │ Demand Forecast  │ │
│  │ (SALE+DISPENSE filter) │ │ (Days of Stock / Flags)│ │ (Multi-Horizon ML│ │
│  └───────────┬────────────┘ └───────────┬────────────┘ └────────┬─────────┘ │
│              │                          │                       │           │
│  ┌───────────▼────────────┐ ┌───────────▼────────────┐ ┌────────▼─────────┐ │
│  │ Historical Data Import │ │ Supplier Intelligence  │ │ Reorder Engine   │ │
│  │ (CSV/XLSX Preview/Sync)│ │ (Rankings & Pricing)   │ │ (Open PO Aware)  │ │
│  └────────────────────────┘ └────────────────────────┘ └──────────────────┘ │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
┌──────────────────────────────────────▼──────────────────────────────────────┐
│                             DATA STORE LAYER                                │
│   MedicineBatch │ Inventory │ StockTransaction │ PurchaseOrder │ Supplier   │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Component Specifications

### 2.1. Automatic Consumption Engine
- **File:** `inventory/consumption_service.py`
- **Rule:** Consumption is calculated strictly from `transaction_type IN ('SALE', 'DISPENSE')` records in `StockTransaction`.
- **Anti-Double-Counting:** Purchase receipts, stock adjustments, opening balances, and damage write-offs are mathematically excluded from consumption.
- **Horizons:**
  - `consumed_7d`: Last 7 calendar days
  - `consumed_14d`: Last 14 calendar days
  - `consumed_30d`: Last 30 calendar days
  - `consumed_90d`: Last 90 calendar days
- **Velocity Metrics:**
  - `daily_avg_30d = consumed_30d / 30.0`
  - `weekly_avg_30d = consumed_30d / 4.0`
  - `monthly_avg_90d = consumed_90d / 3.0`
- **Data Quality Indicator:**
  - `GOOD`: $\ge 15$ transactions in past 90 days.
  - `WARNING`: $5 - 14$ transactions in past 90 days.
  - `INSUFFICIENT_DATA`: $< 5$ transactions (triggers cold-start fallback in forecasting).

---

### 2.2. Historical Data Import Engine
- **File:** `inventory/historical_import.py`
- **Formats Supported:** CSV (`.csv` with UTF-8 / UTF-8-BOM / Latin-1) and Excel (`.xlsx` via `openpyxl`).
- **Validation Pipeline:**
  1. Header normalization (case-insensitive fuzzy matching for `medicine`, `quantity`, `date`, `type`, `reference`).
  2. Entity resolution: matches medicine by `id`, exact name, or barcode.
  3. Positive integer validation for quantity.
  4. In-file duplicate detection (same medicine, date, and quantity).
- **Preview & Commit Workflow:**
  - `validate_and_preview_import()`: Returns valid row count, error row list, and sample preview.
  - `execute_historical_import()`: Uses `@transaction.atomic` to write records and synchronize `ConsumptionCache`.

---

### 2.3. Inventory Health Engine & Days of Stock (DoS)
- **File:** `inventory/health_service.py`
- **Days of Stock Remaining Formula:**
  $$\text{DoS} = \frac{\text{Current Stock}}{\text{Daily Consumption Rate (30d)}}$$
  *(Returns 0 when out of stock; $\infty$ when zero consumption recorded).*
- **Multi-Flag Priority Hierarchy:**
  1. `OUT_OF_STOCK`: $\text{Current Stock} = 0$.
  2. `CRITICAL`: $\text{Current Stock} \le \text{Safety Stock}$.
  3. `EXPIRY_RISK`: Batch expiring within 60 days with available quantity exceeding forecasted consumption.
  4. `LOW`: $\text{Current Stock} \le \text{Reorder Level}$.
  5. `DEAD_STOCK`: No `SALE` or `DISPENSE` activity for $\ge 90$ days with $\text{Stock} > 0$.
  6. `OVERSTOCK`: $\text{Current Stock} > \text{Maximum Stock}$.
  7. `NORMAL`: Healthy inventory within configured operating limits.

---

### 2.4. Multi-Horizon Demand Forecasting
- **File:** `ai_engine/intelligence_service.py` (`get_demand_forecast_multi_horizon`)
- **Supported Horizons:** 7, 14, 30, 60, and 90 days.
- **Engine Logic:**
  - Primary: Random Forest ML Regression model (`demand_forecast_model.joblib`) utilizing velocity, stock buffers, and 7-day trend.
  - Fallback: Moving average consumption baseline.
  - Cold-Start: Rule-based buffer calculation ($\max(\text{reorder\_level} \times 2, \text{safety\_stock} \times 3)$) when data quality is insufficient.

---

### 2.5. Open Purchase Order Aware Reorder Engine
- **File:** `ai_engine/intelligence_service.py` (`get_reorder_recommendations`)
- **Open PO Deduction Logic:**
  - Queries pending units in purchase orders with status in `('DRAFT', 'SUBMITTED', 'APPROVED', 'ORDERED', 'PARTIAL_RECEIPT')`.
  - Calculates $\text{Effective Available} = \text{Current Stock} + \text{Open PO Quantity}$.
  - Skips redundant reorders if $\text{Effective Available} > \text{Reorder Level}$.
  - Reorder quantity formula:
    $$\text{Recommended Qty} = \max(\text{Forecast}_{30d} + \text{Safety Stock} - \text{Effective Available}, 1)$$
- **Urgency Priority:**
  - `CRITICAL`: Current stock is 0 or $\le 30\%$ of reorder level.
  - `HIGH`: Current stock $\le$ reorder level.
  - `MEDIUM`: High consumption velocity eroding safety buffer.

---

### 2.6. Supplier & Price Intelligence
- **File:** `suppliers/intelligence_service.py`
- **Composite Score Formula (0–100):**
  $$\text{Score} = (0.40 \times \text{Price Score}) + (0.20 \times \text{Delivery Score}) + (0.20 \times \text{Fill Rate}) + (0.20 \times \text{Rating Score})$$
- **Price Trend Diagnostics:** Compares average price changes over 30/90 days to detect inflation or discount opportunities.

---

### 2.7. AI Recommendation Center
- **File:** `ai_engine/views.py` (`recommendation_center_view`, `handle_recommendation_action`, `convert_recommendation_to_pr`)
- **Lifecycle:** `PENDING` $\rightarrow$ `REVIEWED` $\rightarrow$ `ACCEPTED` / `REJECTED`.
- **Actionability:** Approved reorder recommendations can be directly converted into official `PurchaseRequest` records with pre-assigned best suppliers and estimated unit costs with one click.

---

## 3. Database Models & Schema Extensions

| Model | App | Description |
|---|---|---|
| `ConsumptionCache` | `inventory` | Precomputed 7/14/30/90d consumption velocity for sub-millisecond dashboard queries |
| `Inventory.StockStatus.CRITICAL` | `inventory` | Added to distinguish critical safety-stock breaches from general low stock |
| `StockTransaction.TransactionType.DISPENSE` | `inventory` | Distinguishes counter prescription dispensations from POS retail sales |
| `StockTransaction.TransactionType.HISTORICAL_IMPORT` | `inventory` | Distinct transaction audit tag for imported past data |
| `AIRecommendation.source_data` | `ai_engine` | JSON explainability metadata storing underlying mathematical parameters |
| `AIRecommendation.Status.REVIEWED` | `ai_engine` | Intermediate workflow status for acknowledged proposals |
| `ReportRecord.ReportType.CONSUMPTION` | `reports` | Consumption Analytics PDF/Excel/CSV export type |
| `ReportRecord.ReportType.OPTIMIZATION` | `reports` | Inventory Optimization & Health Diagnostic export type |

---

## 4. Verification & Testing

The platform includes automated tests covering all modules:
```powershell
.\venv\Scripts\python.exe manage.py test
```
**Results:** `32/32 tests passed (OK)`.

---

## 5. Security & Operational Compliance

1. **Role-Based Access Control (RBAC):** Admin approval is mandatory for committing historical data imports and accepting AI recommendations into Purchase Requests.
2. **Atomic Integrity:** All stock modifications use `@transaction.atomic` and `select_for_update()`.
3. **Audit Trail:** Every recommendation lifecycle event and import action is recorded in `AuditLog`.
