"""
inventory/historical_import.py
MediVault — Historical Consumption Data Import Engine

Allows Admin to upload CSV or Excel (.xlsx) files containing past consumption data
to jump-start or calibrate the AI/ML Demand Forecasting Engine.

Design guarantees:
  - NEVER deducts or adds to current physical stock.
  - NEVER creates purchase receipts, completed sales, or dispensing events.
  - All imported transactions use transaction_type='HISTORICAL_IMPORT' for clear provenance.
  - SHA-256 fingerprint on each row prevents duplicate row re-import.
  - HistoricalImportLog records uploader, filename, timestamp, and row-level results.
  - Idempotent: safe to retry — duplicate rows detected and skipped.
  - Imported data is clearly labelled and distinguishable from real-time transactions.
"""

import io
import csv
import hashlib
import json
from datetime import datetime
from decimal import Decimal

import openpyxl
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError

from medicines.models import Medicine
from inventory.models import Inventory, StockTransaction, ConsumptionCache, HistoricalImportLog
from inventory.consumption_service import get_consumption_analytics


def _row_fingerprint(medicine_id: int, txn_date: str, qty: int, reference: str) -> str:
    """Stable SHA-256 fingerprint for duplicate detection across import batches."""
    key = f"{medicine_id}|{txn_date}|{qty}|{reference}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


class ImportResult(dict):
    """
    Result object returned by execute_historical_import.
    Behaves as both a dict (with 'created' and 'skipped' keys) and an integer
    equal to 'created' (for backwards compatibility with callers expecting int).
    """
    def __init__(self, created: int = 0, skipped: int = 0):
        super().__init__(created=created, skipped=skipped)
        self.created = created
        self.skipped = skipped

    def __int__(self):
        return self.created

    def __index__(self):
        return self.created

    def __eq__(self, other):
        if isinstance(other, int):
            return self.created == other
        return super().__eq__(other)


def parse_import_file(file_obj, filename: str) -> list:
    """
    Parses an uploaded CSV or XLSX file and returns raw row dictionaries.
    Expected columns (case-insensitive):
      - medicine (or medicine_id, medicine_name, or barcode)
      - quantity (or qty, units, consumed)
      - date (or consumption_date, transaction_date)
      - reference (optional)
      - notes (optional)
    """
    rows = []
    fname = filename.lower()

    if fname.endswith('.csv'):
        content = file_obj.read()
        if isinstance(content, bytes):
            try:
                decoded = content.decode('utf-8-sig')
            except UnicodeDecodeError:
                decoded = content.decode('latin-1')
        else:
            decoded = content
        reader = csv.DictReader(io.StringIO(decoded))
        for row in reader:
            norm_row = {k.strip().lower(): v.strip() for k, v in row.items() if k}
            rows.append(norm_row)

    elif fname.endswith(('.xlsx', '.xls')):
        wb = openpyxl.load_workbook(file_obj, data_only=True)
        sheet = wb.active
        headers = []
        for i, row in enumerate(sheet.iter_rows(values_only=True)):
            if i == 0:
                headers = [str(cell).strip().lower() if cell is not None else '' for cell in row]
            else:
                if not any(row):
                    continue
                row_dict = {}
                for h, val in zip(headers, row):
                    if h:
                        if isinstance(val, datetime):
                            row_dict[h] = val.strftime('%Y-%m-%d')
                        else:
                            row_dict[h] = str(val).strip() if val is not None else ''
                rows.append(row_dict)
    else:
        raise ValidationError("Unsupported file format. Please upload a CSV or Excel (.xlsx) file.")

    return rows


def validate_and_preview_import(raw_rows: list) -> dict:
    """
    Validates parsed rows against the database and returns a preview summary.
    Detects:
      - Unknown medicines
      - Invalid/negative quantities
      - Invalid dates
      - In-file duplicate rows (same medicine + date + qty)
      - Already-imported rows (matching fingerprint in existing StockTransaction records)

    Returns:
      {
        'total_rows': int,
        'valid_count': int,
        'error_count': int,
        'duplicate_count': int,
        'preview_rows': list[dict],
        'errors': list[dict],
        'matched_medicines': int,
      }
    """
    preview_rows = []
    errors = []
    seen_keys = set()
    matched_med_ids = set()
    duplicate_count = 0

    # Pre-cache medicines for fast lookup
    all_meds = Medicine.objects.all()
    med_by_id = {str(m.id): m for m in all_meds}
    med_by_name = {m.medicine_name.strip().lower(): m for m in all_meds}
    med_by_barcode = {m.barcode.strip(): m for m in all_meds if getattr(m, 'barcode', None)}

    # Pre-load existing historical import fingerprints from StockTransaction notes
    existing_fingerprints = set(
        StockTransaction.objects.filter(
            transaction_type=StockTransaction.TransactionType.HISTORICAL_IMPORT
        ).values_list('reference_number', flat=True)
    )

    for idx, row in enumerate(raw_rows, start=2):  # row 1 is header
        # Identify medicine
        med_identifier = (
            row.get('medicine') or row.get('medicine_name') or
            row.get('medicine_id') or row.get('barcode') or ''
        ).strip()

        matched_med = None
        if med_identifier in med_by_id:
            matched_med = med_by_id[med_identifier]
        elif med_identifier.lower() in med_by_name:
            matched_med = med_by_name[med_identifier.lower()]
        elif med_identifier in med_by_barcode:
            matched_med = med_by_barcode[med_identifier]

        if not matched_med:
            errors.append({
                'row_num': idx,
                'field': 'medicine',
                'message': f"Medicine '{med_identifier}' not found in system.",
                'raw': row,
            })
            continue

        # Parse quantity
        qty_str = row.get('quantity') or row.get('qty') or row.get('units') or row.get('consumed') or '0'
        try:
            qty = int(float(qty_str))
            if qty <= 0:
                raise ValueError()
        except (ValueError, TypeError):
            errors.append({
                'row_num': idx,
                'field': 'quantity',
                'message': f"Invalid quantity '{qty_str}'. Must be a positive integer.",
                'raw': row,
            })
            continue

        # Parse date
        date_str = (
            row.get('date') or row.get('consumption_date') or
            row.get('transaction_date') or ''
        ).strip()

        txn_date = None
        if date_str:
            for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%Y/%m/%d', '%m/%d/%Y'):
                try:
                    txn_date = datetime.strptime(date_str, fmt).date()
                    break
                except ValueError:
                    continue
        if not txn_date:
            txn_date = timezone.now().date()

        # Validate date is not in the future
        if txn_date > timezone.now().date():
            errors.append({
                'row_num': idx,
                'field': 'date',
                'message': f"Date '{txn_date}' is in the future. Historical imports must use past dates.",
                'raw': row,
            })
            continue

        reference = row.get('reference', f"HIST-{txn_date}").strip() or f"HIST-{txn_date}"
        fingerprint = _row_fingerprint(matched_med.id, str(txn_date), qty, reference)

        # Duplicate check: within this file
        dedup_key = (matched_med.id, txn_date, qty, reference)
        if dedup_key in seen_keys:
            duplicate_count += 1
            errors.append({
                'row_num': idx,
                'field': 'duplicate',
                'message': f"Duplicate row in file: {matched_med.medicine_name} ({qty} units on {txn_date}).",
                'raw': row,
            })
            continue
        seen_keys.add(dedup_key)

        # Duplicate check: already imported in database
        if fingerprint in existing_fingerprints:
            duplicate_count += 1
            errors.append({
                'row_num': idx,
                'field': 'already_imported',
                'message': f"Already imported: {matched_med.medicine_name} ({qty} units on {txn_date}) [fingerprint match].",
                'raw': row,
            })
            continue

        matched_med_ids.add(matched_med.id)
        preview_rows.append({
            'row_num': idx,
            'medicine_id': matched_med.id,
            'medicine_name': matched_med.medicine_name,
            'quantity': qty,
            'date': txn_date.strftime('%Y-%m-%d'),
            'transaction_type': 'HISTORICAL_IMPORT',
            'reference': reference,
            'fingerprint': fingerprint,
            'notes': row.get('notes', 'Historical Consumption Import'),
        })

    return {
        'total_rows': len(raw_rows),
        'valid_count': len(preview_rows),
        'error_count': len(errors),
        'duplicate_count': duplicate_count,
        'preview_rows': preview_rows,
        'errors': errors,
        'matched_medicines': len(matched_med_ids),
    }


@transaction.atomic
def execute_historical_import(validated_rows: list, performed_by, filename: str = 'manual', import_log: HistoricalImportLog = None) -> dict:
    """
    Commits validated rows to the database as HISTORICAL_IMPORT StockTransaction records.
    - Does NOT change current physical stock (previous_stock == new_stock == current_stock).
    - Tags each transaction with transaction_type='HISTORICAL_IMPORT'.
    - Uses row fingerprint as reference_number for future duplicate detection.
    - Updates ConsumptionCache for affected medicines.
    - Returns {'created': int, 'skipped': int}.
    """
    created_count = 0
    skipped_count = 0
    affected_medicines = set()

    # Re-check fingerprints inside transaction to prevent race conditions
    existing_fingerprints = set(
        StockTransaction.objects.filter(
            transaction_type=StockTransaction.TransactionType.HISTORICAL_IMPORT
        ).values_list('reference_number', flat=True)
    )

    for row in validated_rows:
        fingerprint = row.get('fingerprint', '')
        if fingerprint and fingerprint in existing_fingerprints:
            skipped_count += 1
            continue

        try:
            medicine = Medicine.objects.get(pk=row['medicine_id'])
        except Medicine.DoesNotExist:
            skipped_count += 1
            continue

        inventory, _ = Inventory.objects.get_or_create(
            medicine=medicine,
            defaults={
                'minimum_stock': medicine.minimum_stock,
                'maximum_stock': medicine.maximum_stock,
                'reorder_level': medicine.reorder_level,
                'safety_stock': medicine.safety_stock,
            }
        )

        txn_date_str = row['date']
        txn_date = datetime.strptime(txn_date_str, '%Y-%m-%d').date()
        naive_dt = datetime.combine(txn_date, datetime.min.time())
        aware_dt = timezone.make_aware(naive_dt, timezone.get_current_timezone())

        # CRITICAL: previous_stock == new_stock — no physical stock change
        txn = StockTransaction(
            inventory=inventory,
            medicine=medicine,
            transaction_type=StockTransaction.TransactionType.HISTORICAL_IMPORT,
            quantity=row['quantity'],
            previous_stock=inventory.current_stock,
            new_stock=inventory.current_stock,  # No stock change
            reference_number=fingerprint,  # Fingerprint used for dedup detection
            notes=f"[Historical Import] {row.get('notes', '')} | Source: {filename} | Ref: {row.get('reference', '')}",
            performed_by=performed_by,
        )
        txn.save()
        # Force created_at to the historical date for correct analytics
        StockTransaction.objects.filter(pk=txn.pk).update(created_at=aware_dt)

        if fingerprint:
            existing_fingerprints.add(fingerprint)

        created_count += 1
        affected_medicines.add(medicine)

    # Refresh ConsumptionCache for all affected medicines
    for med in affected_medicines:
        refresh_consumption_cache(med)

    # Update import log if provided
    if import_log:
        import_log.imported_rows = created_count
        import_log.status = HistoricalImportLog.ImportStatus.COMPLETED
        import_log.completed_at = timezone.now()
        import_log.save(update_fields=['imported_rows', 'status', 'completed_at'])

    return ImportResult(created=created_count, skipped=skipped_count)


def create_import_log(filename: str, uploaded_by, preview_data: dict) -> HistoricalImportLog:
    """
    Creates or retrieves an HistoricalImportLog for this upload session.
    Uses a file+user+timestamp fingerprint to allow safe retries.
    """
    import_batch_ref = hashlib.sha256(
        f"{filename}|{uploaded_by.id}|{preview_data['total_rows']}|{preview_data['valid_count']}".encode()
    ).hexdigest()[:32]

    log, _ = HistoricalImportLog.objects.get_or_create(
        import_batch_ref=import_batch_ref,
        defaults={
            'uploaded_by': uploaded_by,
            'filename': filename,
            'status': HistoricalImportLog.ImportStatus.PENDING,
            'total_rows': preview_data['total_rows'],
            'valid_rows': preview_data['valid_count'],
            'error_rows': preview_data['error_count'],
            'row_errors': preview_data.get('errors', [])[:50],  # Cap to 50 for storage
        }
    )
    return log


def refresh_consumption_cache(medicine) -> ConsumptionCache:
    """
    Recalculates and caches consumption metrics for a single medicine.
    """
    analytics = get_consumption_analytics(medicine)
    cache, _ = ConsumptionCache.objects.update_or_create(
        medicine=medicine,
        defaults={
            'consumed_7d': analytics['consumed_7d'],
            'consumed_14d': analytics['consumed_14d'],
            'consumed_30d': analytics['consumed_30d'],
            'consumed_90d': analytics['consumed_90d'],
            'daily_avg_30d': Decimal(str(analytics['daily_avg_30d'])),
            'data_quality': analytics['data_quality'],
            'last_movement_date': analytics['last_movement_date'],
        }
    )
    return cache
