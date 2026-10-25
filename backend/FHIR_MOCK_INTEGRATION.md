# MediVault FHIR Mock Integration Guide

## What This Is

MediVault implements a **native FHIR R4 compliant integration adapter** that allows hospital EHR systems to submit medication orders using the standard FHIR R4 `MedicationRequest` resource. This is NOT a copy of any proprietary system — it's a native implementation of open FHIR R4 standards.

> ⚠️ **This is a simulation layer.** MediVault is a pharmacy stock optimization system — it receives FHIR orders but never automatically dispenses. Every inbound order requires pharmacist review and verification.

---

## Quick Start — Testing with Mock EHR Simulator

### 1. Access the Mock Simulator

Navigate to `/integrations/mock-simulator/` (Admin role required)

The simulator provides:
- Pre-loaded sample FHIR payloads (Paracetamol, Amoxicillin)
- Live JSON editor with syntax highlighting
- Real-time ingestion testing against your actual MediVault database
- Idempotency demonstration

### 2. Submit via REST API

```bash
# Test the capability statement (no auth required)
curl http://localhost:8000/api/v1/fhir/metadata/

# Submit a FHIR MedicationRequest
curl -X POST http://localhost:8000/api/v1/fhir/MedicationRequest/ \
  -H "Content-Type: application/json" \
  -H "X-MediVault-System-Code: CERNER_MOCK" \
  -d '{
    "resourceType": "MedicationRequest",
    "id": "test-rx-001",
    "status": "active",
    "intent": "order",
    "medicationCodeableConcept": {
      "text": "Paracetamol 500mg",
      "coding": [{
        "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
        "code": "198440",
        "display": "Paracetamol 500mg Tablet"
      }]
    },
    "subject": {
      "reference": "Patient/UHID-77291",
      "display": "John Smith (Ward 4B)"
    },
    "requester": {
      "display": "Dr. Sarah Johnson, MD"
    },
    "dispenseRequest": {
      "quantity": {"value": 30, "unit": "tablets"}
    },
    "dosageInstruction": [{
      "text": "1 tablet every 6 hours for fever"
    }]
  }'
```

**Response (new order):**
```json
{
  "status": "created",
  "order_id": 1,
  "external_order_id": "test-rx-001",
  "idempotency_key": "CERNER_MOCK:test-rx-001",
  "prescription_id": "RX-EHR-2026-ABCD1234",
  "mapped_medicine": "Paracetamol 500mg",
  "requires_pharmacist_review": true,
  "review_url": "/integrations/orders/1/review/"
}
```

**Response (duplicate — idempotency):**
```json
{
  "status": "existing",
  "order_id": 1,
  "message": "Order already received. Idempotency key matched."
}
```

---

## API Reference

### `GET /api/v1/fhir/metadata/`
Returns the FHIR R4 CapabilityStatement. No authentication required.

```json
{
  "resourceType": "CapabilityStatement",
  "fhirVersion": "4.0.1",
  "status": "active",
  "date": "2026-09-26",
  "rest": [{
    "mode": "server",
    "resource": [
      {"type": "MedicationRequest", "interaction": [{"code": "read"}, {"code": "create"}]},
      {"type": "MedicationDispense", "interaction": [{"code": "read"}]}
    ]
  }]
}
```

### `POST /api/v1/fhir/MedicationRequest/`

**Authentication:** One of:
- `X-MediVault-API-Key: mv_ehr_<key>` header
- `Authorization: Bearer mv_ehr_<key>` header
- `X-MediVault-System-Code: <system_code>` header (for demo/simulator mode)

**Request body:** FHIR R4 MedicationRequest JSON

**Status codes:**
- `201 Created` — New order ingested, prescription created
- `200 OK` — Duplicate order (idempotency matched)
- `400 Bad Request` — Invalid FHIR payload
- `422 Unprocessable Entity` — Validation error

### `GET /api/v1/fhir/MedicationRequest/<external_id>/`
Retrieve order status for an existing external order ID.

### `GET /api/v1/fhir/MedicationDispense/<dispensing_id>/`
Retrieve FHIR R4 MedicationDispense payload for a completed dispensing record.

### `POST /api/v1/fhir/mock-bundle/`
Submit a FHIR Bundle for bulk simulation (demo mode only).

---

## FHIR Payload Requirements

### Minimum Required Fields

| Field | Path | Description |
|-------|------|-------------|
| resourceType | `root.resourceType` | Must be `"MedicationRequest"` |
| id | `root.id` | External order identifier (string) |
| medication | `root.medicationCodeableConcept.text` | Human-readable medication name |
| patient | `root.subject.reference` | `"Patient/<UHID>"` format |
| quantity | `root.dispenseRequest.quantity.value` | Number of units requested |

### Optional Fields

| Field | Path | Notes |
|-------|------|-------|
| prescriber | `root.requester.display` | Doctor's name |
| priority | `root.priority` | routine / urgent / asap / stat |
| dosage | `root.dosageInstruction[0].text` | Dosage instructions text |
| diagnosis | `root.reasonCode[0].text` | Clinical indication |
| RxNorm code | `root.medicationCodeableConcept.coding[0].code` | Used for future matching |

---

## Workflow After Order Ingestion

```
EHR System sends FHIR MedicationRequest
            │
            ▼
┌──────────────────────────────────────┐
│  MediVault: Validate & Ingest        │
│  • FHIR schema validation            │
│  • Idempotency check                 │
│  • Auto-match medicine               │
│  • Create HospitalMedicationOrder    │
│    (status = PENDING_REVIEW)         │
│  • Create linked Prescription        │
│    (status = PENDING)                │
│  • Log to IntegrationMessageLog      │
└──────────────────────────────────────┘
            │
            ▼ Pharmacist receives notification
┌──────────────────────────────────────┐
│  Pharmacist Reviews Order            │
│  /integrations/orders/<id>/review/   │
│  • Views FHIR payload                │
│  • Confirms/corrects medicine map    │
│  • VERIFY → Prescription = VERIFIED  │
│  • REJECT → Order closed             │
└──────────────────────────────────────┘
            │ (VERIFIED only)
            ▼
┌──────────────────────────────────────┐
│  Secure Dispensing Workspace         │
│  • FEFO batch selection              │
│  • Barcode/QR verification           │
│  • Stock deduction (StockTransaction)│
│  • Controlled medicine logging       │
│  • Outbound FHIR MedicationDispense  │
└──────────────────────────────────────┘
```

---

## Connecting a Real EHR System

### Step 1: Register the EHR System
In Django Admin → External Hospital Systems → Add New:
- `name`: Your EHR system name
- `system_code`: Unique code (e.g. `CERNER_CITY_GH`)
- `system_type`: Select appropriate type
- Copy the auto-generated `api_key`

### Step 2: Configure EHR to Send to MediVault
Direct your EHR's FHIR outbound webhook to:
```
POST https://your-medivault-server/api/v1/fhir/MedicationRequest/
```
With header: `X-MediVault-API-Key: mv_ehr_<your_api_key>`

### Step 3: Test with Mock Bundle
Use `/api/v1/fhir/mock-bundle/` to submit a test batch and verify orders appear in the Orders Queue.

---

## Security Model

| Concern | Implementation |
|---------|----------------|
| No auto-dispensing | `dispensing_record` null until pharmacist acts |
| No stock deduction | StockTransaction never created during ingestion |
| Idempotency | Composite key prevents duplicate orders |
| Audit trail | Every API event logged in IntegrationMessageLog (immutable) |
| RBAC | All UI views require ADMIN or PHARMACIST role |
| API auth | API key required for production hospital connections |
