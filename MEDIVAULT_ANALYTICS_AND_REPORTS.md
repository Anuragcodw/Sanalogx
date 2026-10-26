# MediVault — Phase 7: Advanced Analytics & Enterprise Reporting Module

## Executive Overview
MediVault's Phase 7 Analytics and Reporting Module delivers comprehensive executive, clinical, and regulatory visibility across the entire hospital pharmacy lifecycle. Built around a dynamic request-time date resolution engine and a data freshness assessment service, MediVault guarantees that all reports reflect actual database transactions without temporal drift, static date traps, or fabricated values.

---

## 1. Dynamic Date Resolution Engine (`analytics/date_service.py`)

### Zero Static-Date Guarantee
Traditional reporting dashboards frequently suffer from hardcoded calendar dates, leading to stale or empty views as calendar years advance. MediVault strictly forbids hardcoded dates. Every reporting query dynamically computes its datetime boundaries at request time using the configured Django timezone:

$$\text{Current Date} = \text{timezone.localdate}()$$

### Supported Relative Periods
The `resolve_date_range()` function normalizes all date requests into a canonical context dictionary:

| Period Key | Period Label | Start Boundary | End Boundary |
| :--- | :--- | :--- | :--- |
| `today` | Today | `today 00:00:00.000000` | `today 23:59:59.999999` |
| `yesterday` | Yesterday | `(today - 1d) 00:00:00` | `(today - 1d) 23:59:59` |
| `7d` | Last 7 Days | `(today - 6d) 00:00:00` | `today 23:59:59.999999` |
| `30d` | Last 30 Days (Default) | `(today - 29d) 00:00:00` | `today 23:59:59.999999` |
| `90d` | Last 90 Days | `(today - 89d) 00:00:00` | `today 23:59:59.999999` |
| `this_week` | This Week (Mon–Today) | Current week Monday `00:00:00` | `today 23:59:59.999999` |
| `this_month` | This Month to Date | 1st of current month `00:00:00` | `today 23:59:59.999999` |
| `last_month` | Prior Full Calendar Month | 1st of prior month `00:00:00` | Last day of prior month `23:59:59` |
| `this_quarter`| Current Quarter to Date | Start of current quarter `00:00:00` | `today 23:59:59.999999` |
| `this_year` | This Year to Date | Jan 1st of current year `00:00:00` | `today 23:59:59.999999` |
| `custom` | Custom User Range | User `date_from 00:00:00` | User `date_to 23:59:59` |

### Validation Rules
- **Order Invariant**: `date_from` must always be $\le \text{date\_to}$; invalid bounds raise a `ValidationError`.
- **Future Date Restriction**: By default, reports do not accept future start dates for historical clinical activity.

---

## 2. Data Freshness & Quality Engine (`analytics/freshness_service.py`)

### Transparent Clinical Assurance
To prevent hospital administrators from making decisions based on stale or disconnected data, MediVault inspects live database timestamps across all operational streams:
- `Sale.sale_date` (Max timestamp for completed pharmacy sales)
- `DispensingRecord.created_at` (Max timestamp for inpatient ward dispensing)
- `StockTransaction.created_at` (Max timestamp for all stock movements)
- `GoodsReceipt.receipt_date` (Max timestamp for verified receiving)
- `PurchaseOrder.created_at` (Max timestamp for procurement orders)

### Classification Statuses:
1. **`FRESH`**: Clinical activity recorded within the last 48 hours ($\text{days\_stale} \le 2$). Normal operations verified.
2. **`PARTIALLY_UPDATED`**: Recent activity exists, but certain streams (e.g., dispensing vs POS sales) show divergent timestamps ($2 < \text{days\_stale} \le 7$).
3. **`STALE`**: No clinical transactions recorded for $> 7$ days. Prominent amber/red banner warns management of outdated records.
4. **`INSUFFICIENT_DATA`**: Total transaction count $< 5$. System notes that analytics are based on initial catalog defaults and baselines.

---

## 3. Report Extraction Engine (`reports/report_engine.py`)

### 21 Comprehensive Report Types
The report engine is decoupled from HTTP views, returning pure dictionary payloads: `{'data': [...], 'summary': {...}, 'columns': [...]}`.

```
1.  INVENTORY                   - Complete medicine stock snapshot, values, and status
2.  SALES                       - Completed POS sales, revenues, discounts, taxes
3.  PURCHASE                    - Purchase orders, order statuses, delivery schedules
4.  SUPPLIER                    - Supplier performance, on-time delivery %, fill rates
5.  EXPIRY                      - Expired and near-expiry batch audit
6.  LOW_STOCK                   - Stockout risks enriched with Phase 6 AI 30-day forecast
7.  PROFIT                      - True gross profit using SaleItem.purchase_unit_price cost basis
8.  REVENUE                     - Revenue breakdown by category and payment channel
9.  PRICE_HISTORY               - Empirical supplier price changes from verified GRNs
10. AI_PREDICTION               - Machine learning 30-day demand and confidence scores
11. STOCK_MOVEMENT              - Complete StockTransaction audit trail with before/after balances
12. CONTROLLED                  - Schedule H1/X and narcotic dispensing and stock register
13. DISPENSING                  - Inpatient ward dispensing allocations and doctor orders
14. CONSUMPTION                 - Deduplicated clinical consumption velocity
15. OPTIMIZATION                - Safety stock, reorder levels, and EOQ parameters
16. HOSPITAL_INTEGRATION        - EHR / FHIR prescription orders and status mapping
17. REORDER_RECOMMENDATIONS     - AI reorder proposals with lead-time gap calculations
18. DEMAND_FORECAST             - Multi-horizon forecasts (7d, 14d, 30d, 60d, 90d)
19. DEAD_STOCK                  - 90-day zero demand items (excluding 30-day new stock)
20. OVERSTOCK                   - Excess capital and stock exceeding 90-day demand
21. EXPIRY_RISK                 - Financial exposure across 7d, 30d, 60d, 90d windows
```

### True Historical Cost-Basis Gross Profit
Unlike naive inventory systems that multiply current selling price by current replacement cost, MediVault computes gross profit using the **exact historical purchase price recorded at the time of sale**:
$$\text{Gross Profit} = \sum (\text{SaleItem.total\_price} - (\text{SaleItem.purchase\_unit\_price} \times \text{SaleItem.quantity}))$$
This preserves margin accuracy even when supplier prices fluctuate over time.

---

## 4. Multi-Format Export Generators

### 1. PDF Reports (`reports/pdf_service.py`)
- Powered by `ReportLab`.
- Designed with hospital-grade typography: MediVault Navy (`#0D1B2A`), Teal (`#00B4A6`), and Light Gray (`#F8F9FA`).
- Standard A4 document structure:
  - Header block with MediVault branding and metadata.
  - KPI Summary metric cards.
  - Formatted data tables with zebra striping and repeating headers across page breaks.
  - Footer with timestamp, generation user, unique Report ID, and Authenticity Verification QR Code.

### 2. Excel Reports (`reports/excel_service.py`)
- Powered by `openpyxl`.
- Multi-sheet workbook architecture:
  - **Sheet 1: Summary**: Professional title banner, metadata block, and color-coded KPI summary table.
  - **Sheet 2: Report Data**: Fully formatted data table with frozen header panes (`A2`), auto-adjusted column widths, and styled headers.

### 3. CSV Exports (`reports/csv_service.py`)
- UTF-8 streaming CSV output suitable for external BI ingestion, Excel, or statistical software.

---

## 5. Public QR Authenticity & Privacy Verification

### Privacy-Preserving Verification Workflow
Every official PDF report generated by MediVault contains an encrypted verification QR code in its footer:

1. **Token Generation**: When a report record is created, a cryptographically secure 64-character token (`ReportRecord.verification_token`) is generated via `secrets.token_urlsafe(32)`.
2. **Opaque Verification URL**: The QR code encodes solely:
   `https://<hospital-domain>/reports/verify/<verification_token>/`
3. **Zero Patient Data Leakage**: The public verification page displays:
   - Confirmation: *"Official MediVault Report Verified"*
   - Report Type & Report ID
   - Generation Timestamp & Authorized Generating Role
   - Period Covered & Summary Record Count
   - Cryptographic Checksum
   - **Strict Privacy Rule**: Individual patient names, prescription details, or clinical diagnoses are NEVER rendered on the public verification view.
