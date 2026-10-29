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
