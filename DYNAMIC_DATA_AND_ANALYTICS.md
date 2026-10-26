# MediVault — Dynamic Data & Analytics Reference

## Core Principle

> **All dashboards, reports, analytics, consumption calculations, forecasts and AI recommendations must use the current application date and actual available database records dynamically. Never hardcode September, October, 2026, or any other month as the default reporting period.**

This document describes how MediVault enforces dynamic date awareness across every module.

---

## 1. Central Date Resolution Engine

**File:** [`analytics/date_service.py`](analytics/date_service.py)

The single canonical function for resolving any date range:

```python
from analytics.date_service import resolve_date_range

# Standard periods
result = resolve_date_range(period='30d')       # Last 30 days from today
result = resolve_date_range(period='this_month') # 1st of current month → today
result = resolve_date_range(period='last_month') # Previous month (full)
result = resolve_date_range(period='today')      # Today only

# Custom range from user input
result = resolve_date_range(
    period='custom',
    date_from='2026-01-01',
    date_to='2026-03-31'
)

# Testing with fixed reference date (never leaks to production)
result = resolve_date_range(period='today', as_of_date=date(2025, 6, 15))
```

### Return Value Structure

```python
{
    'date_from':        date,       # Start date (inclusive)
    'date_to':          date,       # End date (inclusive)
    'start_datetime':   datetime,   # tz-aware, 00:00:00 of date_from
    'end_datetime':     datetime,   # tz-aware, 23:59:59.999999 of date_to
    'period_label':     str,        # Human-readable e.g. "Last 30 Days"
    'days_count':       int,        # Number of days in range
    'is_custom':        bool,       # True if user-specified date range
    'is_current_month': bool,       # True only for 'this_month' period
    'generated_at':     datetime,   # Timestamp of resolution (for cache invalidation)
}
```

### Supported Period Keywords

| Keyword | Description | Base Reference |
|---------|-------------|----------------|
| `today` | Current day only | `timezone.localdate()` |
| `yesterday` | Previous day | `today - 1 day` |
| `7d` | Last 7 days | `today - 6 days` → today |
| `30d` | Last 30 days | `today - 29 days` → today |
| `90d` | Last 90 days | `today - 89 days` → today |
| `this_week` | Current ISO week Monday → today | `today.weekday()` |
| `this_month` | 1st of current month → today | `today.replace(day=1)` |
| `last_month` | Full previous calendar month | month arithmetic |
| `this_quarter` | Start of current quarter → today | `(month - 1) // 3` |
| `this_year` | Jan 1 of current year → today | `today.replace(month=1, day=1)` |
| `custom` | User-specified dates | `date_from` / `date_to` params |

---

## 2. Data Freshness Engine

**File:** [`analytics/freshness_service.py`](analytics/freshness_service.py)

Evaluates data recency by inspecting actual database records. Used by dashboards to display transparency warnings.

```python
from analytics.freshness_service import get_data_freshness_report

report = get_data_freshness_report()
```

### Freshness Classification

| Status | Condition | UI Color |
|--------|-----------|----------|
| `FRESH` | Latest transaction ≤ 1 day ago | 🟢 Green |
| `PARTIALLY_UPDATED` | Latest transaction 2–7 days ago | 🟡 Yellow |
| `STALE` | Latest transaction > 7 days ago | 🔴 Red |
| `INSUFFICIENT_DATA` | < 5 total stock transactions | ⚪ Grey |

### Return Value (key fields)

```python
{
    'status':                 str,        # FreshnessStatus constant
    'overall_status':         str,        # Alias for status
    'data_sources':           list,       # Per-source breakdown [{name, status, latest, days_ago}]
    'warning_message':        str|None,   # Human-readable warning (None if FRESH)
    'latest_transaction_date': date|None, # Most recent clinical activity date
    'days_stale':             int,        # Days since latest transaction
    'is_stale':               bool,       # True if days_stale > 1
    'generated_at':           datetime,   # Report generation timestamp
}
```

---

## 3. Consumption Analytics — Dynamic Deduplication

**File:** [`inventory/consumption_service.py`](inventory/consumption_service.py)

### Key Functions

```python
from inventory.consumption_service import (
    get_consumption_for_period,
    calculate_net_consumption_in_range,
    get_today_consumption,
    detect_stockout_suppression,
)
```

#### `calculate_net_consumption_in_range(medicine, start_dt, end_dt)`
Core engine — queries actual StockTransactions and deduplicates SALE+DISPENSE:

- Iterates DISPENSE transactions first, tracks `reference_number` in `seen_refs`
- Iterates SALE transactions, **skips** any whose `reference_number` already seen (prevents double-counting dispensing-linked sales)
- Subtracts RETURN transactions from gross consumption
- Returns net consumption units

#### `get_consumption_for_period(medicine, days, as_of_date=None)`
Convenience wrapper using `resolve_date_range` internally. Never hardcodes dates.

#### `detect_stockout_suppression(medicine, days)`
Detects periods where `new_stock=0` in StockTransaction — indicates true demand may exceed recorded consumption (suppressed by stockout).

---

## 4. AI Demand Forecasting — No Future Data Leakage

**File:** [`ai_engine/intelligence_service.py`](ai_engine/intelligence_service.py)

```python
from ai_engine.intelligence_service import get_demand_forecast, get_demand_forecast_multi_horizon

# Standard forecast — cutoff = now, uses all transactions up to today
forecast = get_demand_forecast(medicine)

# With explicit cutoff (for testing or holdout evaluation)
forecast = get_demand_forecast(medicine, cutoff_date=date(2025, 12, 31))
```

### Cutoff Date Enforcement

The `cutoff_date` parameter ensures forecasts are trained **strictly on transactions before the cutoff**. This prevents:
- **Training data leakage**: Future transactions cannot influence the model
- **Evaluation bias**: Holdout testing uses realistic train/test split

### Multi-Horizon Forecast Keys

```python
{
    'horizon_7d':  {'forecast': float, 'data_origin': 'FORECAST'|'ESTIMATE', ...},
    'horizon_14d': {...},
    'horizon_30d': {...},
    'horizon_60d': {...},
    'horizon_90d': {...},
    'cutoff_date': datetime,
    'data_origin': 'FORECAST'|'ESTIMATE',
}
```

`data_origin = 'ESTIMATE'` means there was insufficient data for a trained forecast — the system used statistical estimates instead. Both are clearly labeled in the UI.

### Forecast Accuracy Evaluation

```python
from ai_engine.intelligence_service import evaluate_forecast_accuracy

# Time-based holdout evaluation (last 14 days withheld for testing)
accuracy = evaluate_forecast_accuracy(medicine, test_days=14, train_window_days=60)
# Returns: {'mae': float, 'rmse': float, 'data_origin': str, 'test_days': int}
```

---

## 5. Reports — Dynamic Date Integration

**File:** [`reports/views.py`](reports/views.py)

The `_parse_date_range()` helper in reports uses its own date parsing but respects user-submitted `date_from`/`date_to` parameters. New report types (`DEMAND_FORECAST`, `REORDER_RECOMMENDATIONS`, `DEAD_STOCK`, `OVERSTOCK`, `EXPIRY_RISK`, `HOSPITAL_INTEGRATION`) use `analytics.date_service.resolve_date_range` internally via `reports/report_engine.py`.

---

## 6. Anti-Patterns (Prohibited)

These patterns are **forbidden** across all MediVault modules:

```python
# ❌ NEVER DO THIS
date_from = date(2026, 9, 1)     # Hardcoded month
date_to   = date(2026, 9, 30)    # Hardcoded month
period_label = "September 2026"  # Hardcoded label

# ❌ NEVER DO THIS
Sale.objects.filter(sale_date__month=9, sale_date__year=2026)

# ❌ NEVER DO THIS — Fake future data to make dashboard look current
StockTransaction.objects.create(
    transaction_type='SALE',
    quantity=50,
    created_at=timezone.now()  # Fake transaction injected to inflate stats
)

# ✅ ALWAYS DO THIS INSTEAD
from analytics.date_service import resolve_date_range
date_info = resolve_date_range(period='this_month')  # Always current month
Sale.objects.filter(
    sale_date__gte=date_info['start_datetime'],
    sale_date__lte=date_info['end_datetime'],
)
```
