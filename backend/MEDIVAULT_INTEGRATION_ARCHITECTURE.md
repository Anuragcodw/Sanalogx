# MediVault Integration Architecture

## Overview

MediVault implements a native hospital-pharmacy integration layer inspired by enterprise EHR connectivity concepts. This layer allows external Hospital Information Systems (HIS) and Electronic Health Records (EHR) to submit medication orders to MediVault using the FHIR R4 standard, while enforcing strict pharmacist-authorization workflows.

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     External Hospital Systems                           │
│   Oracle Health / Cerner Millennium   Epic Systems   Custom FHIR R4    │
└─────────────────────────────────────────────────────────────────────────┘
                                   │  FHIR R4 MedicationRequest (JSON)
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│            MediVault FHIR Integration Adapter Layer                     │
│                                                                         │
│  POST /api/v1/fhir/MedicationRequest/                                   │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │ 1. Validate FHIR R4 Schema (required fields, resourceType)      │   │
│  │ 2. Authenticate Hospital System (X-MediVault-API-Key header)    │   │
│  │ 3. Idempotency Check (system_code + external_order_id key)      │   │
│  │ 4. Medicine Auto-Matching (name → MediVault Medicine Master)    │   │
│  │ 5. Create HospitalMedicationOrder (status=PENDING_REVIEW)       │   │
│  │ 6. Auto-create linked Prescription (status=PENDING)             │   │
│  │ 7. Append IntegrationMessageLog (immutable audit trail)         │   │
│  └─────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│              Pharmacist Review & Authorization Step                     │
│                                                                         │
│  /integrations/orders/<id>/review/                                      │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │ 1. Pharmacist sees: patient, medication, FHIR payload, dosage   │   │
│  │ 2. Maps raw EHR medication → internal Medicine Master record    │   │
│  │ 3. Verifies clinical order details                              │   │
│  │ 4. Action: VERIFY (→ ready for dispensing) or REJECT           │   │
│  └─────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘
                                   │ (VERIFIED only)
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│              Secure Dispensing Workspace (Existing Module)              │
│                                                                         │
│  FEFO batch selection → Barcode verification → Stock deduction →        │
│  Dispensing audit trail → FHIR MedicationDispense generation           │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Core Components

### 1. `integrations/models.py` — Three Domain Models

#### `ExternalHospitalSystem`
Registers each connected EHR partner with authentication credentials and metadata.

| Field | Type | Purpose |
|-------|------|---------|
| `system_code` | CharField (unique) | Immutable identifier e.g. `CERNER_CITY_01` |
| `api_key` | CharField | Auto-generated, used in `X-MediVault-API-Key` header |
| `system_type` | CharField | ORACLE_CERNER / EPIC_EHR / CUSTOM_FHIR / SIMULATOR |
| `is_demo_simulator` | BooleanField | Flags test/mock records for reporting |
| `last_sync_at` | DateTimeField | Updated on every successful inbound event |

#### `HospitalMedicationOrder`
Maps each incoming FHIR MedicationRequest to a MediVault workflow object.

| Field | Type | Purpose |
|-------|------|---------|
| `idempotency_key` | CharField (unique) | `f"{system_code}:{external_order_id}"` — prevents duplicates |
| `status` | CharField | PENDING_REVIEW → VERIFIED → DISPENSED (or REJECTED/FAILED) |
| `mapped_medicine` | FK → Medicine | Result of auto-matching (null if unmatched, pharmacist maps manually) |
| `prescription` | FK → Prescription | Auto-created in PENDING status — requires pharmacist verification |
| `dispensing_record` | FK → DispensingRecord | Set only after successful pharmacist-authorized dispensing |
| `fhir_payload` | JSONField | Full raw FHIR R4 JSON for audit and re-processing |

**Safety invariant:** `dispensing_record` is always null until a pharmacist actively verifies and routes to the Secure Dispensing Workspace. No external order can auto-deduct stock.

#### `IntegrationMessageLog`
Immutable, append-only audit trail for all FHIR events.

- All integration API calls are logged here (both inbound and outbound)
- Django admin blocks add/change/delete permissions
- Status: SUCCESS / VALIDATION_ERROR / FAILED / RETRY

---

### 2. `integrations/fhir_service.py` — FHIR Adapter Functions

| Function | Purpose |
|----------|---------|
| `validate_and_parse_fhir_medication_request(payload)` | FHIR R4 schema validation; raises `ValidationError` on invalid payloads |
| `find_matching_medicine(name, code)` | 3-tier matching: exact name → contains name → contains generic name |
| `ingest_fhir_medication_order(payload, hospital_system)` | `@transaction.atomic` — idempotent ingestion returning `(order, is_created)` |
| `generate_fhir_medication_dispense(disp_record)` | Builds FHIR R4 MedicationDispense outbound payload |
| `get_fhir_capability_statement()` | Returns FHIR R4 CapabilityStatement metadata |

---

### 3. `integrations/api_views.py` — FHIR REST API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `GET /api/v1/fhir/metadata/` | GET | FHIR CapabilityStatement (unauthenticated) |
| `POST /api/v1/fhir/MedicationRequest/` | POST | Ingest inbound FHIR order |
| `GET /api/v1/fhir/MedicationRequest/<id>/` | GET | Retrieve order status |
| `GET /api/v1/fhir/MedicationDispense/<id>/` | GET | Retrieve FHIR dispense payload |
| `POST /api/v1/fhir/mock-bundle/` | POST | Submit test FHIR bundle (simulator) |

**Authentication:** `X-MediVault-API-Key` header OR `Authorization: Bearer <key>`. If no key is provided and `X-MediVault-System-Code` header is present, the system looks up the matching `ExternalHospitalSystem` for demo/simulator mode.

---

### 4. `integrations/views.py` — Pharmacist UI

| URL | View | Description |
|-----|------|-------------|
| `/integrations/` | `integration_dashboard_view` | KPI cards, systems table, orders queue, audit logs |
| `/integrations/orders/` | `integration_orders_view` | Paginated, searchable medication orders list |
| `/integrations/orders/<id>/review/` | `order_review_view` | Clinical review: verify/reject with medicine mapping |
| `/integrations/mock-simulator/` | `mock_ehr_simulator_view` | Interactive FHIR JSON editor for testing |

**RBAC:** All UI views require `ADMIN` or `PHARMACIST` role (`is_approved=True`).

---

## Idempotency Logic

Every inbound FHIR order is processed with a composite idempotency key:

```python
idempotency_key = f"{system_code}:{external_order_id}"
```

If a duplicate submission arrives (same key), the existing order is returned immediately without creating a new Prescription or StockTransaction. The duplicate attempt is still logged in `IntegrationMessageLog`.

---

## Medicine Auto-Matching Algorithm

When a FHIR order arrives, MediVault attempts to automatically map the external medication name to an internal Medicine Master record using a 3-tier waterfall:

1. **Exact match:** `medicine_name__iexact = raw_medication_name`
2. **Contains match:** `medicine_name__icontains = raw_medication_name`
3. **Generic name:** `generic_name__icontains = raw_medication_name`

If no match is found, the order is still accepted with `mapped_medicine=null`. The pharmacist manually selects the correct medicine during the review step.

---

## Pharmacist Safety Guarantees

| Safety Rule | Implementation |
|-------------|----------------|
| Never auto-dispense | `dispensing_record` stays null until pharmacist explicitly acts |
| Never auto-deduct stock | No `StockTransaction` is created during ingestion |
| Prescription always PENDING | `Prescription.status = PENDING` on creation |
| Only verified orders route to dispensing | `order.status == VERIFIED` is required before SecureDispensing can process |
| External orders bypass nothing | Full FEFO, barcode, controlled medicine rules apply at dispensing time |
