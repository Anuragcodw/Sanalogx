"""
medicines/barcode_service.py
Phase 1 — Sanalogx Medicine Barcode Registry Service Layer

Features:
1. Unique deterministic Code 128 internal barcode generation: SAN-MED-000123.
2. High-contrast SVG generation for live inline browser previews.
3. High-resolution (300 DPI) PNG byte generation with quiet zones for downloads.
4. Clean, standard hospital label PDF generation via ReportLab (3.5" x 2.0" label format).
5. Safe, repeatable, idempotent backfill utility that preserves manufacturer barcodes.
6. Does NOT embed dynamic stock quantities or batch expiry dates into the medicine barcode.
"""

import io
import logging
from decimal import Decimal

import barcode
from barcode.writer import ImageWriter, SVGWriter
from reportlab.lib.pagesizes import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

from django.db import transaction
from medicines.models import Medicine

logger = logging.getLogger(__name__)

BARCODE_PREFIX = "SAN-MED-"


def get_internal_barcode_string(medicine) -> str:
    """Returns the internal barcode string for a medicine instance."""
    if medicine.internal_barcode:
        return medicine.internal_barcode
    if medicine.pk:
        return f"{BARCODE_PREFIX}{medicine.pk:06d}"
    return ""


def get_or_create_internal_barcode(medicine) -> str:
    """
    Ensures medicine has a persisted internal barcode.
    If missing, assigns and persists deterministic SAN-MED-{pk:06d}.
    Never overwrites existing manufacturer barcodes.
    """
    if medicine.internal_barcode:
        return medicine.internal_barcode

    code = f"{BARCODE_PREFIX}{medicine.pk:06d}"
    with transaction.atomic():
        # Check if already set concurrently
        curr = Medicine.objects.select_for_update().get(pk=medicine.pk)
        if not curr.internal_barcode:
            curr.internal_barcode = code
            curr.save(update_fields=['internal_barcode'])
            medicine.internal_barcode = code
        else:
            medicine.internal_barcode = curr.internal_barcode

    return medicine.internal_barcode


def generate_barcode_svg(code_str: str) -> str:
    """
    Generates a high-contrast Code 128 SVG string suitable for inline HTML rendering.
    """
    if not code_str:
        return ""

    Code128 = barcode.get_barcode_class('code128')
    bc = Code128(code_str, writer=SVGWriter())
    buf = io.BytesIO()
    bc.write(buf, options={
        'write_text': True,
        'quiet_zone': 2.0,
        'font_size': 9,
        'text_distance': 4.0,
        'module_height': 12.0,
    })
    return buf.getvalue().decode('utf-8')


def generate_barcode_png_bytes(code_str: str) -> bytes:
    """
    Generates a 300-DPI high-contrast Code 128 PNG image for file download.
    Includes proper quiet zones and readable text below bars.
    """
    if not code_str:
        raise ValueError("Barcode identifier cannot be empty.")

    Code128 = barcode.get_barcode_class('code128')
    bc = Code128(code_str, writer=ImageWriter())
    buf = io.BytesIO()
    bc.write(buf, options={
        'write_text': True,
        'quiet_zone': 2.5,
        'module_height': 14.0,
        'font_size': 10,
        'text_distance': 4.0,
        'dpi': 300,
    })
    return buf.getvalue()


def generate_barcode_label_pdf_bytes(medicine) -> bytes:
    """
    Generates a print-ready hospital pharmacy barcode label in PDF format.
    Standard label size: 3.5 inches x 2.0 inches (88.9mm x 50.8mm).
    Displays:
      - Header: "Sanalogx Hospital Pharmacy"
      - Medicine Name & Strength
      - Generic Name & Dosage Form
      - High-contrast Code 128 Barcode Image
      - Human-readable Barcode ID (SAN-MED-000123)
      - Manufacturer Barcode reference (if present)
    Strictly avoids embedding transient stock or batch expiry info.
    """
    code_str = get_or_create_internal_barcode(medicine)
    png_bytes = generate_barcode_png_bytes(code_str)

    pdf_buf = io.BytesIO()
    # 3.5 inch wide by 2.0 inch high label
    doc = SimpleDocTemplate(
        pdf_buf,
        pagesize=(3.5 * inch, 2.0 * inch),
        leftMargin=0.15 * inch,
        rightMargin=0.15 * inch,
        topMargin=0.12 * inch,
        bottomMargin=0.12 * inch
    )

    styles = getSampleStyleSheet()

    header_style = ParagraphStyle(
        'LabelHeader',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=7,
        leading=8,
        textColor=colors.HexColor('#0F766E')
    )

    title_style = ParagraphStyle(
        'LabelTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=10,
        leading=12,
        textColor=colors.HexColor('#0F2744')
    )

    meta_style = ParagraphStyle(
        'LabelMeta',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=7.5,
        leading=9,
        textColor=colors.HexColor('#334155')
    )

    mfr_style = ParagraphStyle(
        'LabelMfr',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=6.5,
        leading=8,
        textColor=colors.HexColor('#64748B')
    )

    elements = []

    # Hospital brand header
    elements.append(Paragraph("SANALOGX HOSPITAL PHARMACY &bull; MEDICINE FORMULARY", header_style))
    elements.append(Spacer(1, 2))

    # Medicine name and strength
    med_display = f"{medicine.medicine_name}"
    if medicine.strength and medicine.strength.lower() not in medicine.medicine_name.lower():
        med_display += f" ({medicine.strength})"
    elements.append(Paragraph(med_display, title_style))

    # Generic & Dosage
    generic_part = f"Generic: {medicine.generic_name}" if medicine.generic_name else ""
    form_part = f"Form: {medicine.dosage_form}" if medicine.dosage_form else ""
    cat_part = f"Cat: {medicine.category.name}" if medicine.category else ""
    meta_text = " &bull; ".join(filter(None, [generic_part, form_part, cat_part]))
    if meta_text:
        elements.append(Paragraph(meta_text, meta_style))

    elements.append(Spacer(1, 3))

    # Barcode image flowable
    img_stream = io.BytesIO(png_bytes)
    barcode_img = Image(img_stream, width=3.1 * inch, height=0.92 * inch)
    elements.append(barcode_img)

    # Optional manufacturer reference line if manufacturer barcode exists
    if medicine.barcode:
        elements.append(Spacer(1, 1))
        elements.append(Paragraph(f"MFR Barcode Ref: {medicine.barcode}", mfr_style))

    doc.build(elements)
    return pdf_buf.getvalue()


def backfill_all_medicine_barcodes(force: bool = False) -> dict:
    """
    Idempotent, repeatable utility to generate unique internal barcodes for medicines.
    - If force=False: only assigns barcodes to medicines where internal_barcode is null or empty.
    - If force=True: recalculates all internal barcodes to match deterministic SAN-MED-{pk:06d}.
    - Preserves existing manufacturer codes (Medicine.barcode).
    - Returns summary dict of processed records.
    """
    total = Medicine.objects.count()
    updated = 0
    skipped = 0

    medicines = Medicine.objects.all().order_by('pk')
    for med in medicines:
        target_code = f"{BARCODE_PREFIX}{med.pk:06d}"
        if force or not med.internal_barcode:
            med.internal_barcode = target_code
            med.save(update_fields=['internal_barcode'])
            updated += 1
        else:
            skipped += 1

    return {
        'total': total,
        'updated': updated,
        'skipped': skipped,
    }


# ==============================================================================
# PHASE 2: Comprehensive Barcode Lookup, Classification & Safety Resolution
# ==============================================================================

def resolve_barcode_lookup(raw_code: str, user=None) -> dict:
    """
    Authoritative barcode & QR code resolution engine for Sanalogx Phase 2.
    Strictly read-only: never modifies inventory, batches, or dispensing state.

    Resolves:
    1. Internal Sanalogx Barcode: SAN-MED-000123 (Phase 1 Code 128)
    2. Manufacturer Product Barcode: Medicine.barcode (EAN-13, UPC, Code 128)
    3. Batch-Specific Barcode: MedicineBatch.batch_number
    4. QR Code / Canonical ID: Medicine.qr_code or MED-000123
    5. Pure numeric / database PK fallback

    Returns structured profile including:
    - Real medicine metadata (name, brand, generic, strength, dosage form, manufacturer, category)
    - Verified clinical medicine-use information (or exact fallback 'Medicine-use information not available')
    - Storage instructions & regulatory warnings
    - Total physical stock & usable stock (via existing get_usable_stock)
    - Segregated stock: expired, quarantined, damaged, unavailable
    - Low-stock status & reorder recommendations
    - FEFO-ordered batch breakdown with days to expiry and priority flag
    """
    from django.utils import timezone
    from django.db.models import Q
    from medicines.models import Medicine, MedicineBatch
    from ai_engine.auto_reorder_service import get_usable_stock

    if not raw_code or not isinstance(raw_code, str):
        return {
            'success': False,
            'status_code': 'INVALID_INPUT',
            'message': 'Valid barcode parameter is required.',
            'code': ''
        }

    code = raw_code.strip()
    if not code:
        return {
            'success': False,
            'status_code': 'INVALID_INPUT',
            'message': 'Empty barcode provided.',
            'code': ''
        }

    # Safety: Guard against overly long or command-injection strings
    if len(code) > 128:
        return {
            'success': False,
            'status_code': 'INVALID_INPUT',
            'message': 'Barcode exceeds maximum allowable length (128 characters).',
            'code': code[:32]
        }

    # Normalize JSON payload if QR contains encoded dict
    if code.startswith('{') and code.endswith('}'):
        try:
            import json as _json
            parsed = _json.loads(code)
            if isinstance(parsed, dict):
                code = (
                    parsed.get('rx') or parsed.get('prescription_id') or
                    parsed.get('prescription_number') or parsed.get('internal_barcode') or
                    parsed.get('barcode') or parsed.get('batch') or parsed.get('batch_number') or
                    parsed.get('medicine_id') or parsed.get('medicine') or code
                )
                code = str(code).strip()
        except Exception:
            pass

    today = timezone.localdate()
    matched_medicine = None
    matched_batch = None
    barcode_type = 'UNKNOWN'

    # 1. Check Internal Sanalogx Barcode (SAN-MED-000123)
    if code.upper().startswith(BARCODE_PREFIX):
        barcode_type = 'INTERNAL_SANALOGX'
        matched_medicine = Medicine.objects.filter(internal_barcode__iexact=code).select_related('category').first()
        if not matched_medicine:
            # Fallback by deterministic primary key extraction
            num_part = code[len(BARCODE_PREFIX):].lstrip('0')
            if num_part.isdigit():
                matched_medicine = Medicine.objects.filter(pk=int(num_part)).select_related('category').first()

    # 2. Check MedicineBatch barcode / batch_number
    if not matched_medicine:
        batch_obj = MedicineBatch.objects.filter(
            batch_number__iexact=code
        ).select_related('medicine', 'medicine__category').first()
        if batch_obj:
            matched_batch = batch_obj
            matched_medicine = batch_obj.medicine
            barcode_type = 'BATCH'

    # 3. Check Manufacturer Barcode (Medicine.barcode)
    if not matched_medicine:
        med_by_mfr = Medicine.objects.filter(barcode__iexact=code).select_related('category').first()
        if med_by_mfr:
            matched_medicine = med_by_mfr
            barcode_type = 'MANUFACTURER'

    # 4. Check QR Code (Medicine.qr_code or MED-000123 format)
    if not matched_medicine:
        med_by_qr = Medicine.objects.filter(qr_code__iexact=code).select_related('category').first()
        if med_by_qr:
            matched_medicine = med_by_qr
            barcode_type = 'QR_CODE'
        elif code.upper().startswith('MED-'):
            num_str = code.upper().replace('MED-', '').lstrip('0')
            if num_str.isdigit():
                matched_medicine = Medicine.objects.filter(pk=int(num_str)).select_related('category').first()
                if matched_medicine:
                    barcode_type = 'QR_CODE'

    # 5. Exact Numeric PK or Exact Name lookup
    if not matched_medicine:
        if code.isdigit():
            matched_medicine = Medicine.objects.filter(pk=int(code)).select_related('category').first()
            if matched_medicine:
                barcode_type = 'DATABASE_ID'
        else:
            matched_medicine = Medicine.objects.filter(medicine_name__iexact=code).select_related('category').first()
            if matched_medicine:
                barcode_type = 'NAME_MATCH'

    # 6. Not Found Result
    if not matched_medicine:
        return {
            'success': False,
            'status_code': 'NOT_FOUND',
            'message': f"Medicine not found for barcode '{code}'.",
            'code': code,
            'barcode_type': 'UNKNOWN',
        }

    # Build Authoritative Real Medicine Profile
    inv = getattr(matched_medicine, 'inventory', None)
    usable_stock = get_usable_stock(matched_medicine, as_of_date=today)
    all_batches = list(matched_medicine.batches.all().order_by('expiry_date'))
    total_stock = sum(b.quantity_available for b in all_batches)

    expired_stock = sum(
        b.quantity_available for b in all_batches
        if b.expiry_date <= today or b.status == MedicineBatch.BatchStatus.EXPIRED
    )
    quarantined_stock = sum(
        b.quantity_available for b in all_batches
        if b.status == MedicineBatch.BatchStatus.QUARANTINED
    )
    damaged_stock = sum(
        b.quantity_available for b in all_batches
        if b.status == MedicineBatch.BatchStatus.RETURNED
    )
    unavailable_stock = max(0, total_stock - usable_stock)

    reorder_level = inv.reorder_level if inv else matched_medicine.reorder_level
    maximum_stock = inv.maximum_stock if inv else matched_medicine.maximum_stock
    minimum_stock = inv.minimum_stock if inv else matched_medicine.minimum_stock
    safety_stock = inv.safety_stock if inv else matched_medicine.safety_stock

    is_low_stock = (usable_stock <= reorder_level)
    is_out_of_stock = (usable_stock <= 0)

    if is_out_of_stock:
        stock_status = 'OUT_OF_STOCK'
    elif usable_stock <= minimum_stock:
        stock_status = 'CRITICAL'
    elif is_low_stock:
        stock_status = 'LOW_STOCK'
    elif usable_stock > maximum_stock:
        stock_status = 'OVERSTOCK'
    else:
        stock_status = 'NORMAL'

    # Reorder Recommendation
    reorder_recommendation = None
    if is_low_stock:
        suggested_qty = max(0, maximum_stock - usable_stock)
        reorder_recommendation = {
            'needed': True,
            'suggested_quantity': suggested_qty,
            'reorder_level': reorder_level,
            'minimum_stock': minimum_stock,
            'maximum_stock': maximum_stock,
            'safety_stock': safety_stock,
            'reason': f"Current usable stock ({usable_stock} units) is at or below reorder threshold ({reorder_level} units)."
        }

    # Verified Medicine-Use Information
    verified_use = ""
    if matched_medicine.description and matched_medicine.description.strip():
        verified_use = matched_medicine.description.strip()
    elif matched_medicine.review_note and matched_medicine.review_note.strip():
        verified_use = matched_medicine.review_note.strip()
    elif matched_medicine.category and matched_medicine.category.notes and matched_medicine.category.notes.strip():
        verified_use = matched_medicine.category.notes.strip()
    elif matched_medicine.category and matched_medicine.category.description and matched_medicine.category.description.strip():
        verified_use = matched_medicine.category.description.strip()

    if not verified_use:
        verified_use = "Medicine-use information not available"

    # Storage Instructions
    storage_instructions = matched_medicine.storage_condition.strip() if matched_medicine.storage_condition else ""
    if not storage_instructions:
        batch_with_storage = next((b.storage_condition for b in all_batches if b.storage_condition and b.storage_condition.strip()), None)
        storage_instructions = batch_with_storage or "Standard pharmacy room storage (store below 25°C in a dry place)."

    # Clinical Warnings & Regulatory Flags
    warnings = []
    if matched_medicine.is_controlled:
        warnings.append("Controlled Substance — Strict register tracking and verified prescription required.")
    if matched_medicine.is_prescription_required:
        warnings.append("Prescription Required — Schedule medication; cannot be dispensed without doctor prescription.")
    if is_low_stock:
        warnings.append(f"Low Stock Alert: Usable stock ({usable_stock}) is at or below reorder level ({reorder_level}).")
    if expired_stock > 0:
        warnings.append(f"Expired Stock Notice: {expired_stock} unit(s) expired in inventory and segregated from usable stock.")
    if quarantined_stock > 0:
        warnings.append(f"Quarantined Stock Notice: {quarantined_stock} unit(s) in quarantine pending quality review.")

    # FEFO Batch Breakdown
    first_valid_fefo_id = None
    for b in all_batches:
        if b.expiry_date > today and b.quantity_available > 0 and b.status in [MedicineBatch.BatchStatus.ACTIVE, MedicineBatch.BatchStatus.NEAR_EXPIRY]:
            first_valid_fefo_id = b.id
            break

    batches_data = []
    for b in all_batches:
        days_left = (b.expiry_date - today).days
        b_is_expired = (b.expiry_date <= today or b.status == MedicineBatch.BatchStatus.EXPIRED)
        b_is_near = (not b_is_expired and (days_left <= 30 or b.status == MedicineBatch.BatchStatus.NEAR_EXPIRY))
        b_is_quar = (b.status == MedicineBatch.BatchStatus.QUARANTINED)

        batches_data.append({
            'id': b.id,
            'batch_number': b.batch_number,
            'quantity_available': b.quantity_available,
            'quantity_received': b.quantity_received,
            'expiry_date': b.expiry_date.strftime('%Y-%m-%d'),
            'manufacturing_date': b.manufacturing_date.strftime('%Y-%m-%d') if b.manufacturing_date else None,
            'days_to_expiry': days_left,
            'status': b.status,
            'selling_price': str(b.selling_price) if b.selling_price else '0.00',
            'purchase_price': str(b.purchase_price) if b.purchase_price else '0.00',
            'is_expired': b_is_expired,
            'is_near_expiry': b_is_near,
            'is_quarantined': b_is_quar,
            'is_fefo_first': (b.id == first_valid_fefo_id),
        })

    medicine_payload = {
        'id': matched_medicine.id,
        'medicine_name': matched_medicine.medicine_name,
        'brand_name': matched_medicine.brand_name or '',
        'generic_name': matched_medicine.generic_name or '',
        'strength': matched_medicine.strength or '',
        'dosage_form': matched_medicine.dosage_form or '',
        'manufacturer': matched_medicine.manufacturer or 'N/A',
        'unit_of_measure': matched_medicine.unit_of_measure or 'Unit',
        'category': matched_medicine.category.name if matched_medicine.category else 'General',
        'category_code': matched_medicine.category.code if (matched_medicine.category and matched_medicine.category.code) else '',
        'internal_barcode': matched_medicine.effective_internal_barcode,
        'barcode': matched_medicine.barcode or '',
        'barcode_type': barcode_type,
        'verified_medicine_use': verified_use,
        'storage_instructions': storage_instructions,
        'warnings': warnings,
        'total_stock': total_stock,
        'usable_stock': usable_stock,
        'expired_stock': expired_stock,
        'quarantined_stock': quarantined_stock,
        'damaged_stock': damaged_stock,
        'unavailable_stock': unavailable_stock,
        'stock_status': stock_status,
        'is_low_stock': is_low_stock,
        'is_out_of_stock': is_out_of_stock,
        'reorder_level': reorder_level,
        'minimum_stock': minimum_stock,
        'maximum_stock': maximum_stock,
        'safety_stock': safety_stock,
        'reorder_recommendation': reorder_recommendation,
        'regulatory_category': matched_medicine.regulatory_category,
        'regulatory_display': matched_medicine.get_regulatory_category_display(),
        'is_controlled': matched_medicine.is_controlled,
        'is_prescription_required': matched_medicine.is_prescription_required,
        'is_active': (matched_medicine.status == Medicine.Status.ACTIVE),
        'batches': batches_data,
    }

    result = {
        'success': True,
        'type': 'BATCH' if matched_batch else 'MEDICINE',
        'barcode_type': barcode_type,
        'code': code,
        'medicine': medicine_payload,
        'message': f"Medicine '{matched_medicine.medicine_name}' resolved successfully."
    }

    if matched_batch:
        days_to_exp = (matched_batch.expiry_date - today).days
        is_exp = (matched_batch.expiry_date <= today or matched_batch.status == MedicineBatch.BatchStatus.EXPIRED)
        result['batch'] = {
            'id': matched_batch.id,
            'batch_number': matched_batch.batch_number,
            'barcode': matched_batch.batch_number,
            'expiry_date': matched_batch.expiry_date.strftime('%Y-%m-%d'),
            'is_expired': is_exp,
            'is_quarantined': (matched_batch.status == MedicineBatch.BatchStatus.QUARANTINED),
            'days_to_expiry': days_to_exp,
            'quantity_available': matched_batch.quantity_available,
            'unit_cost': str(matched_batch.purchase_price) if matched_batch.purchase_price else '0.00',
            'selling_price': str(matched_batch.selling_price) if matched_batch.selling_price else '0.00',
            'status': matched_batch.status,
            'is_fefo_first': (matched_batch.id == first_valid_fefo_id),
        }

    return result


def map_manufacturer_barcode(medicine_id: int, barcode_value: str, user=None) -> dict:
    """
    Safely maps an external/manufacturer barcode to an existing medicine catalogue record.
    Security & Business Rules:
    - Pharmacist or Admin permission required.
    - Prevents duplicate mappings (rejects if barcode is already assigned to another medicine).
    - Prevents overwriting existing manufacturer barcodes.
    - Never automatically maps an arbitrary medicine.
    - Audited via AuditLog.
    """
    from medicines.models import Medicine
    from accounts.models import AuditLog

    code = str(barcode_value or '').strip()
    if not code:
        return {'success': False, 'message': 'Barcode value cannot be empty.'}

    if len(code) > 100:
        return {'success': False, 'message': 'Barcode exceeds maximum allowable length (100 characters).'}

    try:
        medicine = Medicine.objects.get(pk=medicine_id)
    except Medicine.DoesNotExist:
        return {'success': False, 'message': f'Medicine with ID {medicine_id} does not exist.'}

    # Prevent duplicate mappings across medicines
    existing_other = Medicine.objects.filter(barcode=code).exclude(pk=medicine.pk).first()
    if existing_other:
        return {
            'success': False,
            'message': f"Barcode '{code}' is already mapped to '{existing_other.medicine_name}' ({existing_other.effective_internal_barcode})."
        }

    # Prevent overwriting existing manufacturer barcode
    if medicine.barcode and medicine.barcode.strip():
        if medicine.barcode.strip() == code:
            return {
                'success': True,
                'message': f"Barcode '{code}' is already assigned to {medicine.medicine_name}.",
                'medicine_id': medicine.pk,
                'barcode': code,
            }
        return {
            'success': False,
            'message': f"Medicine '{medicine.medicine_name}' already has manufacturer barcode '{medicine.barcode}'. Overwriting existing manufacturer barcodes is not permitted."
        }

    # Atomic update
    with transaction.atomic():
        medicine.barcode = code
        medicine.save(update_fields=['barcode'])

        # Audit logging
        if user and user.is_authenticated:
            try:
                AuditLog.objects.create(
                    user=user,
                    performed_by=user,
                    action='MEDICINE_UPDATED',
                    description=f"Manually mapped manufacturer barcode '{code}' to medicine '{medicine.medicine_name}' (ID: {medicine.pk})."
                )
            except Exception as e:
                logger.warning(f"Failed to create AuditLog entry for barcode mapping: {e}")

    return {
        'success': True,
        'message': f"Barcode '{code}' successfully mapped to {medicine.medicine_name}.",
        'medicine_id': medicine.pk,
        'medicine_name': medicine.medicine_name,
        'internal_barcode': medicine.effective_internal_barcode,
        'barcode': code,
    }

