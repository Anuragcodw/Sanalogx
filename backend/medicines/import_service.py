"""
medicines/import_service.py
MediVault — Category Master & Medicine Master CSV/Excel Import Engine

Safety guarantees:
  - Never creates MedicineBatch, Inventory, StockTransaction or any stock record.
  - Starter_Examples are imported as PENDING_REVIEW (draft) status only.
  - Duplicate categories are mapped by name (case-insensitive); never overwritten.
  - Duplicate medicines (by generic_name + strength + dosage_form) are detected
    and reported — existing verified records are never overwritten.
  - Row-level errors are collected and returned for download as CSV error report.
  - All imports are wrapped in atomic transactions with AuditLog entries.
  - Sensitive fields (barcodes, prices, batch numbers) from templates are
    intentionally ignored — admins supply those during pharmacy review.
"""

import io
import csv
import hashlib
from decimal import Decimal, InvalidOperation

import openpyxl
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError

from medicines.models import Category, Medicine, MedicineImportLog


# ── File Parsing ────────────────────────────────────────────────────────────

def _read_file_sha256(file_obj) -> str:
    """Compute SHA-256 of entire upload for provenance."""
    file_obj.seek(0)
    content = file_obj.read()
    file_obj.seek(0)
    return hashlib.sha256(content).hexdigest()


def _parse_csv(file_obj) -> list[dict]:
    """Parse CSV file; returns list of normalised dicts."""
    content = file_obj.read()
    if isinstance(content, bytes):
        try:
            content = content.decode('utf-8-sig')
        except UnicodeDecodeError:
            content = content.decode('latin-1')
    reader = csv.DictReader(io.StringIO(content))
    rows = []
    for row in reader:
        norm = {k.strip().lower().replace(' ', '_'): (v.strip() if v else '') for k, v in row.items() if k}
        if any(norm.values()):
            rows.append(norm)
    return rows


def _parse_xlsx_sheet(sheet) -> list[dict]:
    """Parse a single openpyxl worksheet; returns list of normalised dicts."""
    headers = []
    rows = []
    for i, row in enumerate(sheet.iter_rows(values_only=True)):
        if i == 0:
            headers = [str(c).strip().lower().replace(' ', '_') if c is not None else '' for c in row]
            continue
        if not any(row):
            continue
        row_dict = {}
        for h, val in zip(headers, row):
            if h:
                row_dict[h] = str(val).strip() if val is not None else ''
        if any(row_dict.values()):
            rows.append(row_dict)
    return rows


def parse_import_file(file_obj, filename: str) -> dict:
    """
    Parse a CSV or XLSX workbook and return a sheet_map of:
      {'sheet_name': [row_dicts, ...], ...}
    For CSV, the single sheet is named 'default'.
    """
    fname = filename.lower()
    if fname.endswith('.csv'):
        return {'default': _parse_csv(file_obj)}
    elif fname.endswith(('.xlsx', '.xls')):
        file_obj.seek(0)
        wb = openpyxl.load_workbook(file_obj, data_only=True)
        return {ws.title: _parse_xlsx_sheet(ws) for ws in wb.worksheets if ws.max_row > 1}
    else:
        raise ValidationError("Unsupported file format. Upload a CSV or Excel (.xlsx) file.")


# ── Category Import ─────────────────────────────────────────────────────────

CATEGORY_REQUIRED_FIELDS = {'category_name', 'name'}

_REGULATORY_MAP = {
    'general': 'NORMAL', 'otc': 'NORMAL', 'normal': 'NORMAL',
    'schedule h': 'SCHEDULE_H', 'schedule_h': 'SCHEDULE_H',
    'schedule h1': 'SCHEDULE_H1', 'schedule_h1': 'SCHEDULE_H1',
    'schedule x': 'SCHEDULE_X', 'schedule_x': 'SCHEDULE_X',
    'narcotic': 'NARCOTIC', 'ndps': 'NARCOTIC',
    'controlled': 'CONTROLLED_OTHER',
}


def _extract_category_name(row: dict) -> str:
    return (
        row.get('category_name') or row.get('name') or
        row.get('category') or row.get('cat_name') or ''
    ).strip()


def validate_and_preview_categories(rows: list[dict]) -> dict:
    """
    Validate Category_Master rows and return preview + errors.
    Duplicate existing categories are reported as duplicates, not errors.
    """
    existing_names = {c.name.strip().lower(): c for c in Category.objects.all()}
    existing_codes = {c.code.strip().lower(): c for c in Category.objects.filter(code__isnull=False) if c.code}

    preview_rows, errors, duplicates = [], [], []
    seen_names = set()

    for idx, row in enumerate(rows, start=2):
        name = _extract_category_name(row)
        if not name:
            errors.append({'row_num': idx, 'field': 'name', 'message': 'Category name is required.', 'raw': row})
            continue

        code = (row.get('category_code') or row.get('code') or '').strip().upper().replace(' ', '_')
        description = row.get('description') or row.get('notes') or ''
        notes = row.get('notes') or row.get('pharmacy_notes') or ''

        lower_name = name.lower()
        lower_code = code.lower() if code else None

        is_duplicate_name = lower_name in existing_names
        is_duplicate_code = lower_code and lower_code in existing_codes
        is_infile_dup = lower_name in seen_names

        if is_infile_dup:
            errors.append({'row_num': idx, 'field': 'name', 'message': f"Duplicate category name '{name}' within file.", 'raw': row})
            continue

        seen_names.add(lower_name)

        status = 'duplicate' if is_duplicate_name else 'new'
        existing_obj = existing_names.get(lower_name)

        preview_rows.append({
            'row_num': idx,
            'name': name,
            'code': code,
            'description': description,
            'notes': notes,
            'status': status,
            'existing_id': existing_obj.id if existing_obj else None,
            'is_duplicate_code': is_duplicate_code,
        })
        if is_duplicate_name:
            duplicates.append({'row_num': idx, 'name': name})

    return {
        'total_rows': len(rows),
        'valid_count': len(preview_rows),
        'new_count': sum(1 for r in preview_rows if r['status'] == 'new'),
        'duplicate_count': len(duplicates),
        'error_count': len(errors),
        'preview_rows': preview_rows,
        'errors': errors,
        'duplicates': duplicates,
    }


@transaction.atomic
def execute_category_import(preview_data: dict, uploaded_by, filename: str, file_sha256: str = '') -> MedicineImportLog:
    """
    Execute the category import. Skips duplicates (maps them), creates new categories.
    Never overwrites existing category names or descriptions.
    """
    from accounts.models import AuditLog

    created, updated, skipped = 0, 0, 0
    errors = []

    for row in preview_data.get('preview_rows', []):
        if row['status'] == 'duplicate':
            # Optionally update code/notes if missing on existing
            cat = Category.objects.filter(name__iexact=row['name']).first()
            if cat:
                changed = False
                if row['code'] and not cat.code:
                    cat.code = row['code']
                    changed = True
                if row['notes'] and not cat.notes:
                    cat.notes = row['notes']
                    changed = True
                if changed:
                    cat.save(update_fields=['code', 'notes', 'updated_at'])
                    updated += 1
                else:
                    skipped += 1
        elif row['status'] == 'new':
            try:
                Category.objects.create(
                    name=row['name'],
                    code=row['code'] or None,
                    description=row['description'],
                    notes=row['notes'],
                    is_active=True,
                )
                created += 1
            except Exception as exc:
                errors.append({'row_num': row['row_num'], 'field': '', 'message': str(exc), 'raw': row})

    status = (
        MedicineImportLog.Status.FAILED if created == 0 and errors
        else MedicineImportLog.Status.PARTIAL if errors
        else MedicineImportLog.Status.SUCCESS
    )

    log = MedicineImportLog.objects.create(
        import_type=MedicineImportLog.ImportType.CATEGORY_MASTER,
        filename=filename,
        file_sha256=file_sha256,
        uploaded_by=uploaded_by,
        total_rows=preview_data.get('total_rows', 0),
        created_count=created,
        updated_count=updated,
        duplicate_count=preview_data.get('duplicate_count', 0),
        error_count=len(errors),
        status=status,
        summary_report={
            'created': created,
            'updated': updated,
            'skipped': skipped,
            'duplicate_mapped': preview_data.get('duplicate_count', 0),
        },
        error_details=errors,
    )

    try:
        AuditLog.log(
            action_type='CATEGORY_IMPORT',
            performed_by=uploaded_by,
            object_type='Category',
            object_id=log.id,
            object_repr=filename,
            description=f"Category import: {created} created, {updated} updated, {skipped} skipped, {len(errors)} errors.",
        )
    except Exception:
        pass

    return log


# ── Medicine Import ─────────────────────────────────────────────────────────

MEDICINE_REQUIRED_FIELDS = {'generic_name', 'dosage_form', 'strength', 'unit_of_measure'}

_DOSAGE_NORMALISE = {
    'tab': 'Tablet', 'tablet': 'Tablet', 'tablets': 'Tablet',
    'cap': 'Capsule', 'capsule': 'Capsule', 'capsules': 'Capsule',
    'syrup': 'Syrup', 'suspension': 'Suspension', 'susp': 'Suspension',
    'inj': 'Injection', 'injection': 'Injection',
    'drops': 'Drops', 'ointment': 'Ointment', 'cream': 'Cream',
    'gel': 'Gel', 'powder': 'Powder', 'inhaler': 'Inhaler',
    'patch': 'Patch', 'solution': 'Solution', 'lotion': 'Lotion',
    'spray': 'Spray', 'suppository': 'Suppository', 'enema': 'Enema',
    'granules': 'Granules', 'liquid': 'Liquid', 'oral liquid': 'Oral Liquid',
}

_UNIT_NORMALISE = {
    'strip': 'Strip', 'strips': 'Strip',
    'box': 'Box', 'boxes': 'Box',
    'bottle': 'Bottle', 'bottles': 'Bottle',
    'vial': 'Vial', 'vials': 'Vial',
    'ampoule': 'Ampoule', 'amp': 'Ampoule', 'ampoules': 'Ampoule',
    'sachet': 'Sachet', 'sachets': 'Sachet',
    'tube': 'Tube', 'tubes': 'Tube',
    'unit': 'Unit', 'units': 'Unit',
    'tablet': 'Tablet', 'tablets': 'Tablet',
    'capsule': 'Capsule', 'capsules': 'Capsule',
    'ml': 'mL', 'ml bottle': 'mL Bottle',
}


def _normalise_dosage(val: str) -> str:
    return _DOSAGE_NORMALISE.get(val.strip().lower(), val.strip().title()) if val else ''


def _normalise_unit(val: str) -> str:
    return _UNIT_NORMALISE.get(val.strip().lower(), val.strip()) if val else ''


def _infer_unit_from_dosage(dosage_form: str) -> str:
    df = (dosage_form or '').lower()
    if 'tablet' in df:
        return 'Tablet'
    if 'capsule' in df:
        return 'Capsule'
    if any(k in df for k in ['syrup', 'suspension', 'liquid', 'solution']):
        return 'Bottle'
    if 'injection' in df or 'ampoule' in df:
        return 'Vial'
    if 'infusion' in df or 'iv' in df:
        return 'Bottle'
    if any(k in df for k in ['cream', 'ointment', 'gel']):
        return 'Tube'
    if 'drop' in df:
        return 'Bottle'
    if 'inhaler' in df:
        return 'Inhaler'
    if 'powder' in df or 'sachet' in df:
        return 'Sachet'
    return 'Unit'


def _resolve_category(row: dict) -> Category | None:
    """
    Resolve category from row using code → name → partial name fallback.
    Returns Category or None.
    """
    cat_code = (row.get('category_code') or row.get('cat_code') or '').strip().upper().replace(' ', '_')
    cat_name = (row.get('category') or row.get('category_name') or '').strip()

    if cat_code:
        cat = Category.objects.filter(code__iexact=cat_code, is_active=True).first()
        if cat:
            return cat
        cat = Category.objects.filter(name__iexact=cat_code.replace('_', ' '), is_active=True).first()
        if cat:
            return cat
        return None

    if cat_name:
        cat = Category.objects.filter(name__iexact=cat_name, is_active=True).first()
        if cat:
            return cat
        # Partial fuzzy match
        cat = Category.objects.filter(name__icontains=cat_name.split()[0], is_active=True).first()
        if cat:
            return cat
        return None

    # Fallback to category by dosage form if unassigned (neither code nor name provided)
    dosage_raw = (row.get('dosage_form') or row.get('dosage_form_or_item_type') or row.get('form') or '').strip().lower()
    if 'tablet' in dosage_raw or 'capsule' in dosage_raw:
        cat = Category.objects.filter(name__icontains='Tablet', is_active=True).first()
        if cat:
            return cat
    elif 'syrup' in dosage_raw or 'suspension' in dosage_raw:
        cat = Category.objects.filter(name__icontains='Syrup', is_active=True).first()
        if cat:
            return cat
    elif 'injection' in dosage_raw:
        cat = Category.objects.filter(name__icontains='Injection', is_active=True).first()
        if cat:
            return cat
    elif 'drop' in dosage_raw:
        cat = Category.objects.filter(name__icontains='Drop', is_active=True).first()
        if cat:
            return cat

    return Category.objects.filter(name__iexact='Other', is_active=True).first() or Category.objects.filter(is_active=True).first()


def _check_medicine_duplicate(generic_name: str, strength: str, dosage_form: str) -> Medicine | None:
    """Check if a medicine with identical generic_name + strength + dosage_form already exists."""
    return Medicine.objects.filter(
        generic_name__iexact=generic_name,
        strength__iexact=strength,
        dosage_form__iexact=dosage_form,
    ).first()


def _is_starter_example_row(row: dict, sheet_name: str = '') -> bool:
    """
    Determine if a row originates from a starter/candidate/draft sheet.
    Rows with source_type='example' or sheet named 'Starter_Examples' or 'All_Items_Import'
    or containing PENDING_REVIEW verification_status are treated as drafts.
    """
    sname = (sheet_name or '').lower()
    if any(k in sname for k in ['starter', 'example', 'all_items', 'draft']):
        return True
    ver_stat = (row.get('verification_status') or '').strip().upper()
    if ver_stat == 'PENDING_REVIEW':
        return True
    src = (row.get('source_type') or row.get('source') or row.get('row_type') or row.get('source_reference') or '').lower()
    return any(k in src for k in ['example', 'starter', 'draft', 'sample', 'candidate'])


def validate_and_preview_medicines(rows: list[dict], sheet_name: str = 'default') -> dict:
    """
    Validate Medicine_Import / Starter_Examples rows.
    Returns structured preview with per-row status and errors.
    """
    preview_rows, errors = [], []
    is_example_sheet = _is_starter_example_row({}, sheet_name)

    for idx, row in enumerate(rows, start=2):
        row_errors = []

        # Required fields with flexible column header mapping
        generic_name = (
            row.get('generic_name') or
            row.get('generic_or_product_name') or
            row.get('generic') or
            row.get('medicine_or_brand_name') or ''
        ).strip()
        dosage_raw = (
            row.get('dosage_form') or
            row.get('dosage_form_or_item_type') or
            row.get('dosage') or
            row.get('form') or
            row.get('item_type') or ''
        ).strip()
        dosage_form = _normalise_dosage(dosage_raw)
        strength = (
            row.get('strength') or
            row.get('strength_concentration_size') or
            row.get('concentration') or
            row.get('size') or ''
        ).strip()
        unit_raw = (
            row.get('unit_of_measure') or
            row.get('unit') or
            row.get('uom') or ''
        ).strip()
        if not unit_raw and dosage_form:
            unit_raw = _infer_unit_from_dosage(dosage_form)
        unit_of_measure = _normalise_unit(unit_raw) or 'Unit'

        if not generic_name:
            row_errors.append({'field': 'generic_name', 'message': 'Generic name is required.'})
        if not dosage_form:
            row_errors.append({'field': 'dosage_form', 'message': 'Dosage form is required.'})
        if not strength:
            row_errors.append({'field': 'strength', 'message': 'Strength is required.'})
        if not unit_of_measure:
            row_errors.append({'field': 'unit_of_measure', 'message': 'Unit of measure is required.'})

        if row_errors:
            for e in row_errors:
                errors.append({'row_num': idx, 'field': e['field'], 'message': e['message'], 'raw': row})
            continue

        # Determine draft status
        is_example = is_example_sheet or _is_starter_example_row(row, sheet_name)

        # Category resolution
        category = _resolve_category(row)
        cat_name_in_row = (row.get('category') or row.get('category_name') or row.get('category_code') or '').strip()
        if not category and cat_name_in_row:
            errors.append({
                'row_num': idx,
                'field': 'category',
                'message': f"Category '{cat_name_in_row}' not found. Create the category first.",
                'raw': row,
            })
            continue

        # Duplicate detection
        existing = _check_medicine_duplicate(generic_name, strength, dosage_form)

        # Build brand_name & medicine_name
        brand_name = (row.get('brand_name') or '').strip()
        if not brand_name and row.get('medicine_or_brand_name') and row.get('generic_name') and row.get('medicine_or_brand_name') != row.get('generic_name'):
            brand_name = row.get('medicine_or_brand_name').strip()
        manufacturer = (row.get('manufacturer') or row.get('mfr') or '').strip()

        medicine_name = (row.get('medicine_name') or row.get('medicine_or_brand_name') or '').strip()
        if not medicine_name:
            if brand_name:
                medicine_name = f"{brand_name} {strength}".strip()
            else:
                medicine_name = f"{generic_name} {strength}".strip()

        # ATC code
        atc_code = (row.get('atc_code') or row.get('atc') or '').strip().upper()

        # Regulatory category
        regulatory_raw = (row.get('regulatory_category') or row.get('regulatory') or row.get('schedule') or 'NORMAL').strip()
        regulatory = _REGULATORY_MAP.get(regulatory_raw.lower(), 'NORMAL')

        # Storage
        storage_condition = (row.get('storage_condition') or row.get('storage') or '').strip()

        # Description / notes
        description = (row.get('description') or row.get('therapeutic_notes') or '').strip()
        review_note = (row.get('review_note') or row.get('pharmacy_note') or '').strip()

        # Source reference
        source_reference = (
            row.get('source_reference') or row.get('source') or
            ('Starter_Examples' if is_example else 'Medicine_Import')
        )

        row_status = 'duplicate' if existing else ('draft' if is_example else 'new')

        preview_rows.append({
            'row_num': idx,
            'medicine_name': medicine_name,
            'generic_name': generic_name,
            'brand_name': brand_name,
            'manufacturer': manufacturer,
            'dosage_form': dosage_form,
            'strength': strength,
            'unit_of_measure': unit_of_measure,
            'category_name': category.name if category else '',
            'category_id': category.id if category else None,
            'atc_code': atc_code,
            'regulatory': regulatory,
            'storage_condition': storage_condition,
            'description': description,
            'review_note': review_note,
            'source_reference': source_reference,
            'is_example': is_example,
            'status': row_status,
            'existing_id': existing.id if existing else None,
            'existing_name': str(existing) if existing else None,
        })

    return {
        'total_rows': len(rows),
        'valid_count': len(preview_rows),
        'new_count': sum(1 for r in preview_rows if r['status'] == 'new'),
        'draft_count': sum(1 for r in preview_rows if r['status'] == 'draft'),
        'duplicate_count': sum(1 for r in preview_rows if r['status'] == 'duplicate'),
        'error_count': len(errors),
        'preview_rows': preview_rows,
        'errors': errors,
    }


@transaction.atomic
def execute_medicine_import(
    preview_data: dict,
    uploaded_by,
    filename: str,
    import_type: str = 'MEDICINE_IMPORT',
    file_sha256: str = '',
) -> MedicineImportLog:
    """
    Execute medicine import. Creates new or draft records only.
    Duplicates (existing verified entries) are NEVER overwritten.
    """
    from accounts.models import AuditLog

    created, draft_created, skipped = 0, 0, 0
    errors = []

    for row in preview_data.get('preview_rows', []):
        if row['status'] == 'duplicate':
            skipped += 1
            continue

        category = None
        if row['category_id']:
            try:
                category = Category.objects.get(pk=row['category_id'])
            except Category.DoesNotExist:
                errors.append({'row_num': row['row_num'], 'field': 'category', 'message': 'Category disappeared between preview and import.', 'raw': row})
                continue

        if not category:
            # Uncategorised imports require a default category (Admin should have created it)
            errors.append({'row_num': row['row_num'], 'field': 'category', 'message': 'No valid category for this row.', 'raw': row})
            continue

        is_draft = row.get('is_example', False) or row.get('status') == 'draft'

        try:
            med = Medicine(
                medicine_name=row['medicine_name'],
                generic_name=row['generic_name'],
                brand_name=row.get('brand_name', ''),
                manufacturer=row.get('manufacturer', ''),
                dosage_form=row['dosage_form'],
                strength=row['strength'],
                unit_of_measure=row['unit_of_measure'],
                category=category,
                atc_code=row.get('atc_code') or None,
                regulatory_category=row.get('regulatory', 'NORMAL'),
                storage_condition=row.get('storage_condition', ''),
                description=row.get('description', ''),
                review_note=row.get('review_note', ''),
                source_reference=row.get('source_reference', import_type),
                # Draft medicines are PENDING_REVIEW. Normal imports are ACTIVE but unverified.
                status=(
                    Medicine.Status.PENDING_REVIEW if is_draft
                    else Medicine.Status.ACTIVE
                ),
                verification_status=(
                    Medicine.VerificationStatus.PENDING_REVIEW
                ),
                # Intentionally leave prices, mrp, barcode, manufacturer blank —
                # pharmacist fills these during review.
                tax_rate=Decimal('0.00'),
                mrp=Decimal('0.00'),
                max_discount_pct=Decimal('20.00'),
                reorder_level=50,
                minimum_stock=10,
                maximum_stock=500,
                safety_stock=20,
            )
            med.full_clean(exclude=['barcode', 'qr_code', 'image'])
            med.save()
            if is_draft:
                draft_created += 1
            else:
                created += 1
        except Exception as exc:
            errors.append({
                'row_num': row['row_num'],
                'field': '',
                'message': str(exc),
                'raw': row,
            })

    status = (
        MedicineImportLog.Status.FAILED if (created + draft_created) == 0 and errors
        else MedicineImportLog.Status.PARTIAL if errors
        else MedicineImportLog.Status.SUCCESS
    )

    log = MedicineImportLog.objects.create(
        import_type=import_type,
        filename=filename,
        file_sha256=file_sha256,
        uploaded_by=uploaded_by,
        total_rows=preview_data.get('total_rows', 0),
        created_count=created + draft_created,
        updated_count=0,
        duplicate_count=preview_data.get('duplicate_count', 0),
        error_count=len(errors),
        status=status,
        summary_report={
            'created_active': created,
            'created_draft': draft_created,
            'skipped_duplicates': skipped,
            'errors': len(errors),
        },
        error_details=errors,
    )

    try:
        AuditLog.log(
            action_type='MEDICINE_IMPORT',
            performed_by=uploaded_by,
            object_type='Medicine',
            object_id=log.id,
            object_repr=filename,
            description=(
                f"Medicine import ({import_type}): {created} active, {draft_created} draft, "
                f"{skipped} duplicates skipped, {len(errors)} errors."
            ),
        )
    except Exception:
        pass

    return log


# ── Error Report CSV Builder ─────────────────────────────────────────────────

def build_error_report_csv(import_log: MedicineImportLog) -> str:
    """
    Build a downloadable CSV error report from a MedicineImportLog.
    Returns the CSV content as a string.
    """
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'Import Type', 'File', 'Imported At',
        'Row #', 'Field', 'Error Message',
    ])
    for err in import_log.error_details:
        writer.writerow([
            import_log.get_import_type_display(),
            import_log.filename,
            import_log.created_at.strftime('%Y-%m-%d %H:%M UTC'),
            err.get('row_num', ''),
            err.get('field', ''),
            err.get('message', ''),
        ])
    return output.getvalue()


# ── Multi-Sheet Workbook Router ──────────────────────────────────────────────

# Sheet names from the attached workbook → import type
KNOWN_SHEETS = {
    'category_master': MedicineImportLog.ImportType.CATEGORY_MASTER,
    'medicine_import': MedicineImportLog.ImportType.MEDICINE_IMPORT,
    'starter_examples': MedicineImportLog.ImportType.STARTER_EXAMPLES,
}


def classify_sheet(sheet_name: str) -> str | None:
    """Return the import type key for a known sheet name, else None."""
    key = sheet_name.lower().strip().replace(' ', '_')
    return KNOWN_SHEETS.get(key)
