# MediVault — Phase 7: Real-Time Notifications & Automated Scheduling

## Executive Overview
MediVault's Notification Engine provides reliable, clinical-grade alerting for hospital pharmacy operations. By utilizing cryptographic fingerprint deduplication, multi-channel dispatch (In-App and Email), and intelligent severity escalation, MediVault ensures that clinical staff and administrators are alerted to inventory risks before they compromise patient care, while strictly eliminating notification spam and alert fatigue.

---

## 1. Central Notification Architecture

```
                       +-----------------------------------+
                       |    Automated / Scheduled Checks   |
                       | (Management Command / Cron / App) |
                       +-----------------------------------+
                                         |
                                         v
                       +-----------------------------------+
                       |    notifications/service.py       |
                       |    - Dynamic Rule Evaluation      |
                       |    - Threshold Filtering          |
                       +-----------------------------------+
                                         |
                                         v
                       +-----------------------------------+
                       |    Fingerprint Deduplication      |
                       | SHA256(entity_type + id + cat +   |
                       |        severity + date_bucket)    |
                       +-----------------------------------+
                                    /         \
                       [Exists within]       [New Alert]
                       [dedup window ]            |
                             |                    v
                          SUPPRESS        +-----------------+
                                          | Create Record   |
                                          | (DB Persist)    |
                                          +-----------------+
                                            /             \
                                           v               v
                             +-------------------+   +--------------------+
                             | In-App Bell & Hub |   | Secure SMTP Email  |
                             | (Live AJAX Badge) |   | (HTML + Plaintext) |
                             +-------------------+   +--------------------+
```

### Core Architecture Components:
- **`notifications/models.py` (`Notification`)**:
  - `recipient`: User target (Admin, Pharmacist, or Broadcast).
  - `notification_type`: `LOW_STOCK`, `OUT_OF_STOCK`, `NEAR_EXPIRY`, `EXPIRED`, `OVERDUE_ORDER`, `DEAD_STOCK`, `PRICE_CHANGE`, `DISCREPANCY`.
  - `severity`: `INFO`, `WARNING`, `CRITICAL`.
  - `fingerprint`: Unique SHA-256 hash preventing duplicate alert generation.
  - `is_read`: Boolean read status with timestamp.
  - `action_url`: Direct deep link to resolve the alert in the relevant module.
- **`notifications/service.py`**: Central coordinator for all alert detection, formatting, deduplication, and multi-channel transmission.

---

## 2. Cryptographic Fingerprint Deduplication

### Alert Fatigue Prevention
In a 24/7 hospital environment, repeated identical notifications desensitize clinical staff. MediVault solves this with a mathematical fingerprint deduplication engine:

$$\text{Fingerprint} = \text{SHA256}(\text{recipient\_id} + \text{notification\_type} + \text{entity\_id} + \text{severity} + \text{date\_bucket})$$

### Deduplication Rules:
1. **Daily Bucket (`YYYY-MM-DD`)**: For routine warnings (e.g., Near Expiry 30 Days, Dead Stock), fingerprints include the calendar day, ensuring at most one alert per 24 hours.
2. **State-Change Invalidation**: If the severity or stock state changes (e.g., from `LOW_STOCK` to `OUT_OF_STOCK`), the fingerprint changes immediately, allowing urgent escalation without delay.
3. **Lookup Integrity**: Before persisting any notification, `service.create_notification` queries existing records within the deduplication window. If an identical fingerprint exists, creation is skipped silently without throwing database errors.

---

## 3. Notification Categories & Escalation Matrix

| Notification Type | Severity | Trigger Criteria | Action URL | Target Recipient |
| :--- | :--- | :--- | :--- | :--- |
| **OUT_OF_STOCK** | `CRITICAL` | Usable stock $= 0$ for active medicine | `/inventory/` or `/purchases/create/?medicine_id=X` | Pharmacists & Admins |
| **LOW_STOCK** | `CRITICAL` | Usable stock $\le 30\%$ of reorder level | `/inventory/` | Pharmacists & Admins |
| **LOW_STOCK** | `WARNING` | Usable stock $\le$ reorder level | `/inventory/` | Pharmacists & Admins |
| **EXPIRED** | `CRITICAL` | Batch expiry date $\le$ today with positive stock | `/inventory/expiry/` | Pharmacists & Admins |
| **NEAR_EXPIRY (7d)** | `CRITICAL` | Batch expires in $\le 7$ days | `/inventory/expiry/` | Pharmacists & Admins |
| **NEAR_EXPIRY (30d)**| `WARNING` | Batch expires in $\le 30$ days | `/inventory/expiry/` | Pharmacists & Admins |
| **NEAR_EXPIRY (90d)**| `INFO` | Batch expires in $\le 90$ days | `/inventory/expiry/` | Pharmacists |
| **OVERDUE_ORDER** | `WARNING` | PO in-flight past `expected_delivery_date` | `/purchases/tracking/` | Administrators |
| **DEAD_STOCK** | `WARNING` | Zero consumption in 90d (stock $> 0$, received $> 30$d ago) | `/ai-intelligence/` | Administrators |
| **PRICE_CHANGE** | `INFO` | Supplier invoice price differs from catalog baseline | `/suppliers/price-comparison/` | Administrators |

---

## 4. Multi-Channel Delivery Engine

### 1. In-App Notification Center & Navbar Bell
- **Real-Time Badge**: Displays unread notification count on navbar bell icon dynamically via lightweight JSON endpoint (`/notifications/api/bell/`).
- **Dropdown Preview**: Renders the 5 most recent unread alerts with direct deep-linking to corrective action workflows.
- **Dedicated Notification Center** (`/notifications/`): Comprehensive management view supporting:
  - Filter by category (`all`, `low_stock`, `expiry`, `purchases`, `unread`).
  - Single-click "Mark as Read".
  - Bulk "Mark All as Read".
  - Action buttons routing directly to stock reorders, disposal write-offs, or PO tracking.

### 2. Secure HTML & Plaintext Email Dispatch
- Utilizes Django's configured email backend with fail-safe SMTP exception handling.
- Sends dual-part emails (`text/html` and `text/plain`) styled with MediVault brand colors (Navy `#0D1B2A`, Teal `#00B4A6`).
- Automatic fallback prevents background task crashes when SMTP server is unreachable or offline during automated runs.

---

## 5. Management Commands & Automated Scheduling

MediVault includes two production-ready Django management commands:

### 1. `python manage.py check_notifications`
The primary operational command for automated alert scanning and health verification.
- **Dry-Run Mode**: `python manage.py check_notifications --dry-run` (simulates checks without writing to DB or dispatching emails).
- **Category Filtering**: `python manage.py check_notifications --category=low_stock` (available: `all`, `low_stock`, `expiry`, `overdue`, `dead_stock`).

### 2. `python manage.py send_scheduled_notifications`
Legacy compatible runner executing all 6 standard scheduled checks in sequence.

### Recommended Production Cron Configuration

#### Linux / Systemd Crontab (`/etc/cron.d/medivault_alerts`):
```cron
# Daily 06:00 UTC - Full notification sweep (Expiry, Dead Stock, Low Stock, Overdue POs)
0 6 * * * medivault /opt/medivault/venv/bin/python /opt/medivault/backend/manage.py check_notifications >> /var/log/medivault/notifications.log 2>&1

# Hourly - Urgent Stockout and Critical Low Stock checks
0 * * * * medivault /opt/medivault/venv/bin/python /opt/medivault/backend/manage.py check_notifications --category=low_stock >> /var/log/medivault/notifications_urgent.log 2>&1
```

#### Windows Task Scheduler (PowerShell):
```powershell
# Create Daily Notification Task
$Action = New-ScheduledTaskAction -Execute "python.exe" -Argument "manage.py check_notifications" -WorkingDirectory "C:\Users\user\Desktop\Hospital-Stock-Optimization\MediVault\backend"
$Trigger = New-ScheduledTaskTrigger -Daily -At 6:00AM
Register-ScheduledTask -Action $Action -Trigger $Trigger -TaskName "MediVault_Check_Notifications" -Description "Daily MediVault automated alert scanning"
```

---

## 6. Notification API Reference

| Endpoint | Method | Role | Description |
| :--- | :--- | :--- | :--- |
| `/notifications/api/bell/` | `GET` | Authenticated | Returns `{unread_count, notifications: [...]}` for navbar bell. |
| `/notifications/api/<id>/read/` | `POST` | Authenticated | Marks a specific notification as read. |
| `/notifications/api/mark-all-read/` | `POST` | Authenticated | Marks all notifications for the current user as read. |
| `/notifications/` | `GET` | Authenticated | HTML view for Notification Management Center. |
