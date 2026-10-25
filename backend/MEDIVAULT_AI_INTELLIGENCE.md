# MediVault — Phase 6: AI Intelligence & Inventory Automation

## Executive Overview
MediVault's Phase 6 AI Intelligence & Inventory Automation transforms hospital pharmacy inventory from reactive replenishment into a predictive, clinical-grade supply chain engine. By combining historical dispensing velocity, sale consumption, supplier lead times, and open purchase order commitments, MediVault eliminates catastrophic stockouts of critical medicines while preventing overstock and expiry waste.

---

## 1. Architectural Architecture & Core Principles

```
  +-------------------------------------------------------------+
  |              MediVault Clinical Data Streams                |
  |   (Completed Sales + Confirmed Dispensing - Valid Returns)  |
  +-------------------------------------------------------------+
                                 |
                                 v
  +-------------------------------------------------------------+
  |        inventory/consumption_service.py                     |
  |        - Strict deduplication of clinical consumption       |
  |        - Excludes cancelled sales and non-demand write-offs |
  +-------------------------------------------------------------+
                                 |
                                 v
  +-------------------------------------------------------------+
  |        ai_engine/intelligence_service.py                    |
  |  +------------------------+   +---------------------------+ |
  |  | Multi-Horizon Forecast |   | Stockout Prevention       | |
  |  | (7d, 14d, 30d, 60d, 90d|   | - Lead time awareness     | |
  |  | No future-leakage      |   | - Open PO awareness       | |
  |  | MAE / RMSE evaluation  |   | - VED vs Schedule H1 sep. | |
  |  +------------------------+   +---------------------------+ |
  |  +------------------------+   +---------------------------+ |
  |  | Expiry Risk Analysis   |   | Dead Stock & Overstock    | |
  |  | (7d, 30d, 60d, 90d)    |   | (90d zero-demand,         | |
  |  | Financial exposure     |   |  30d new-stock exclusion) | |
  |  +------------------------+   +---------------------------+ |
  +-------------------------------------------------------------+
                                 |
        +------------------------+------------------------+
        |                                                 |
        v                                                 v
+-------------------------------+             +---------------------------+
| AI Reorder Recommendations    |             | Controlled AI Assistant   |
| (Actionable Draft Orders)     |             | (Chatbot & MediVoice UI)  |
+-------------------------------+             +---------------------------+
```

### Core Design Guarantees:
1. **Zero Future-Data Leakage**: Every forecast query strictly enforces `cutoff_date`. Historical validation splits demand data into non-overlapping training and test intervals.
2. **Empirical Clinical Consumption**: Demand calculations strictly aggregate genuine patient consumption (`SALE` + `DISPENSE` minus `RETURN`), excluding inventory adjustments, damage, expired disposals, and cancelled sales.
3. **Open PO Awareness**: Reorder algorithms factor in active, in-flight procurement (`APPROVED`, `ORDERED`, `PARTIAL_RECEIPT`) to avoid duplicative capital tie-up.
4. **Separation of Urgency vs Regulation**: Clinical criticality (Vital, Essential, Desirable - VED) dictates reorder prioritization, while regulatory classifications (Schedule H1, Schedule X, Narcotics) dictate compliance workflows and audit logging, never confusing regulatory friction with clinical urgency.
5. **Human-in-the-Loop Safeguards**: AI engines generate recommendations and prepare draft orders. The AI assistant is strictly read-only and non-mutating; autonomous stock deductions or autonomous PO approvals are forbidden.

---

## 2. Multi-Horizon Demand Forecasting

### Multi-Horizon Calculations
The forecasting engine (`get_demand_forecast_multi_horizon`) provides visibility across 5 discrete planning horizons:
- **7-Day Horizon**: Immediate short-term clinical velocity and buffer monitoring.
- **14-Day Horizon**: Near-term replenishment cycle for high-velocity inpatient pharmaceuticals.
- **30-Day Horizon**: Primary monthly reorder baseline and safety stock calculation.
- **60-Day Horizon**: Mid-term supplier contract planning and bulk discount forecasting.
- **90-Day Horizon**: Quarterly budgeting and seasonal demand analysis.

### ML Demand Model & Cold-Start Fallback
- **ML Model**: `RandomForestRegressor` trained on historical consumption features (current stock, reorder level, minimum stock, safety stock, and recent 7-day velocity).
- **Cold-Start Rule**: If a medicine has fewer than 5 transactions in the 30-day window or zero recorded consumption, the engine automatically falls back to clinical rule-based heuristics:
  $$\text{Forecast}_{30\text{d}} = \max(\text{reorder\_level} \times 2, \text{safety\_stock} \times 3)$$
  It flags `is_cold_start = True` and sets `data_quality = 'INSUFFICIENT_DATA'`.

### Chronological Holdout Validation (MAE & RMSE)
Model performance is continuously verifiable via `evaluate_forecast_accuracy`:
- Splits history at a dynamic cutoff date $(T - \text{test\_days})$.
- Trains the model strictly on transactions $\le T - \text{test\_days}$.
- Compares predicted daily rate against actual daily consumption in the test holdout.
- Computes Mean Absolute Error (MAE) and Root Mean Squared Error (RMSE):
  $$\text{MAE} = \frac{1}{N} \sum |y_{\text{pred}} - y_{\text{actual}}|$$
  $$\text{RMSE} = \sqrt{\frac{1}{N} \sum (y_{\text{pred}} - y_{\text{actual}})^2}$$

---

## 3. Explainable Stockout Prevention Engine

### Days of Stock Remaining & Lead-Time Gap
For every active pharmaceutical, the stockout prevention engine calculates:
1. **Usable Stock**: Total quantity available across active, non-expired, non-quarantined batches matching FEFO integrity.
2. **Days of Stock Remaining**:
   $$\text{Days Remaining} = \lfloor \frac{\text{Usable Stock}}{\text{Daily Demand Rate}} \rfloor$$
3. **Supplier Lead Time**: Actual empirical delivery days of the preferred supplier (default: 7 days).
4. **Stockout Risk Gap**:
   $$\text{Stockout Risk Days} = \text{Days of Stock Remaining} - \text{Supplier Lead Days}$$
   *A negative value indicates the medicine will stock out before a new delivery can arrive.*

### Lead-Time Aware Urgency Tiers
- **CRITICAL (Stockout Before Delivery)**: Usable stock runs out before expected supplier delivery ($\text{Stockout Risk Days} < 0$), current stock is zero, or stock $\le 30\%$ of reorder level.
- **HIGH (Below Reorder Level)**: Effective available stock $\le \text{reorder\_level}$.
- **MEDIUM (Forecast Consumption Alert)**: Projected 30-day demand depletes stock below safety buffer.

### Open Purchase Order Awareness
To prevent double ordering, the engine calculates:
$$\text{Effective Available} = \text{Usable Stock} + \sum \text{Pending Quantity on Open POs}$$
Reorder recommendations are suppressed or adjusted to:
$$\text{Recommended Quantity} = \min(\text{Max Stock} - \text{Effective Available}, \max(0, \text{Forecast}_{30\text{d}} + \text{Safety Stock} - \text{Effective Available}))$$

---

## 4. Expiry, Dead Stock & Overstock Optimization

### Dynamic Expiry Tiers
- **Expired**: Batches with $\text{expiry\_date} \le \text{today}$ (quarantined from dispensing).
- **Critical (0–7 Days)**: Imminent expiry; immediate clinical substitution or manufacturer return alert.
- **High Risk (8–30 Days)**: Fast-track dispensing prioritization via FEFO.
- **Medium Risk (31–60 Days)**: Active monitoring.
- **Early Warning (61–90 Days)**: Reorder threshold check.
- **Financial Exposure**: Calculated as $\sum (\text{batch.quantity\_available} \times \text{batch.purchase\_price})$ for all batches expiring within each window.

### Dead Stock Detection with New Stock Exclusion
A medicine is classified as **Dead Stock** only when:
1. Current usable stock $> 0$.
2. Zero clinical consumption (`SALE` + `DISPENSE`) has occurred across the past 90 days.
3. **New Stock Exclusion**: Batches received within the last 30 days are strictly excluded from dead stock classification to prevent flagging freshly arrived catalog expansions.

### Overstock Intelligence
A medicine is classified as **Overstock** when:
- $\text{Current Stock} > \text{Maximum Stock}$, or
- $\text{Days of Stock Remaining} > 90\text{ days}$ based on current consumption velocity.
- The engine calculates excess units and estimated capital locked up.

---

## 5. Controlled MediVault AI Assistant (Chatbot & MediVoice)

### Conversational & Voice Interaction Architecture
- Built into `templates/medivoice_widget.html`, included globally across all authenticated pages.
- Web Speech API integration provides hands-free voice transcription and speech synthesis feedback.
- Accessible via interactive quick chips: `Low Stock`, `Expiry Risk`, `Today Sales`, `AI Reorder`, `Dead Stock`.

### Intent Routing & Security Boundaries
- All user input routes through `ai_engine/chat_service.py` (`process_ai_chat_query`) and `ai_engine/voice_service.py`.
- **Zero SQL / Raw Prompt Execution**: User natural language maps strictly to predefined clinical intents:
  - `STOCK_QUERY`: Live batch stock, location, and expiry checks.
  - `LOW_STOCK_QUERY`: Real-time low stock and out-of-stock listings.
  - `EXPIRY_QUERY`: Batches nearing expiry within 30/90 days.
  - `REORDER_RECOMMENDATIONS`: AI reorder proposals with supplier rankings.
  - `SALES_SUMMARY`: Revenue, gross profit, and invoice totals.
  - `DISPENSING_STATUS`: Inpatient orders and controlled drug audit logs.
  - `DEAD_STOCK_QUERY`: Idle capital and non-moving batches.
- **Role Enforcement**: Pharmacists cannot access administrative approval panels; unauthenticated requests are blocked.
- **Non-Mutating Principle**: The assistant prepares links, draft parameters, and clinical data; it cannot approve orders, dispense drugs, or alter database records directly.
