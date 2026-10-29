"""
medicines/ocr_service.py
Phase 3 — OCR-Assisted Medicine Packaging Recognition Service

Workflow:
1. Staff uploads or captures an image of a medicine packaging box or blister pack.
2. Image format and size constraints (≤ 5MB, JPG/PNG/WEBP) are strictly enforced.
3. Visible text is extracted using pytesseract (Tesseract OCR).
4. Text is parsed with regular expressions for candidate fields:
   - Medicine name candidates
   - Strength candidates (e.g. 500mg, 10mg, 1% w/w)
   - Dosage form candidates (e.g. Tablet, Capsule, Syrup, Cream)
   - Manufacturer candidates
   - Batch number candidates (e.g. Batch:, Lot:, B.No:)
   - Expiry date candidates (e.g. EXP:, Exp Date:)
5. Candidates are normalized and matched against existing MySQL Medicine and Batch records.
6. A confidence score (0-100%) and confidence level (HIGH, MEDIUM, LOW) are computed.
7. Original extracted text is preserved and displayed for staff verification.
8. Requires manual staff confirmation before navigating to profile or saving a mapping.

Clinical Safety Rules:
- OCR outputs are suggestions, NOT verified facts.
- Never used alone to certify medicine identity, batch validity, or expiry.
- Never automatically alters inventory, dispenses medicine, or creates records.
"""

import re
import os
import io
import shutil
import logging
from datetime import datetime
from PIL import Image, ImageOps, ImageEnhance
from django.utils import timezone
from django.db.models import Q

logger = logging.getLogger(__name__)

# Constants
MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB
ALLOWED_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp')
ALLOWED_MIME_TYPES = ('image/jpeg', 'image/png', 'image/webp')


def is_ocr_available() -> tuple[bool, str]:
    """
    Checks if pytesseract and the underlying Tesseract OCR binary are available.
    Returns (is_available, status_message).
    """
    try:
        import pytesseract
    except ImportError:
        return False, "pytesseract library is not installed in the Python environment."

    tess_path = shutil.which('tesseract')
    if not tess_path:
        # Check standard Windows paths
        win_candidates = [
            r'C:\Program Files\Tesseract-OCR\tesseract.exe',
            r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe',
        ]
        for p in win_candidates:
            if os.path.exists(p):
                pytesseract.pytesseract.tesseract_cmd = p
                tess_path = p
                break

    if not tess_path:
        return False, "Tesseract OCR binary not found on system PATH. Install Tesseract-OCR to enable image text recognition."

    return True, f"Tesseract OCR is available at {tess_path}."


def preprocess_image_for_ocr(pil_img: Image.Image) -> Image.Image:
    """
    Preprocesses packaging image to maximize OCR text extraction quality:
    - Converts to grayscale
    - Auto-contrasts
    - Sharpens edges slightly
    """
    try:
        # Convert to grayscale
        gray = ImageOps.grayscale(pil_img)
        # Auto contrast
        contrast = ImageOps.autocontrast(gray)
        # Moderate sharpness enhancement
        enhancer = ImageEnhance.Sharpness(contrast)
        sharp = enhancer.enhance(1.5)
        return sharp
    except Exception as e:
        logger.warning(f"Image preprocessing warning: {e}")
        return pil_img


def extract_raw_text_from_image(image_input) -> tuple[bool, str, str]:
    """
    Reads an uploaded file object, byte stream, or PIL Image, validates safety,
    and runs Tesseract OCR.
    Returns (success: bool, raw_text: str, error_message: str).
    """
    available, msg = is_ocr_available()
    if not available:
        return False, "", msg

    try:
        import pytesseract

        # 1. Load Image
        if isinstance(image_input, Image.Image):
            pil_img = image_input
        elif hasattr(image_input, 'read'):
            image_bytes = image_input.read()
            if len(image_bytes) > MAX_IMAGE_SIZE_BYTES:
                return False, "", f"Image file size ({len(image_bytes) / (1024*1024):.1f} MB) exceeds maximum allowed limit (5 MB)."
            pil_img = Image.open(io.BytesIO(image_bytes))
        elif isinstance(image_input, (bytes, bytearray)):
            if len(image_input) > MAX_IMAGE_SIZE_BYTES:
                return False, "", f"Image size ({len(image_input) / (1024*1024):.1f} MB) exceeds maximum allowed limit (5 MB)."
            pil_img = Image.open(io.BytesIO(image_input))
        else:
            return False, "", "Invalid image input format."

        # 2. Validate Image Safety
        pil_img.verify()  # verify integrity
        # Reopen after verify() because verify() clears file pointers
        if hasattr(image_input, 'seek'):
            image_input.seek(0)
            pil_img = Image.open(image_input)
        elif isinstance(image_input, (bytes, bytearray)):
            pil_img = Image.open(io.BytesIO(image_input))

        # Check format
        if pil_img.format and pil_img.format.upper() not in ['JPEG', 'PNG', 'WEBP', 'MPO', 'TIFF']:
            return False, "", f"Unsupported image format: {pil_img.format}. Please use JPG, PNG, or WEBP."

        # 3. Preprocess & Run OCR
        processed_img = preprocess_image_for_ocr(pil_img)
        raw_text = pytesseract.image_to_string(processed_img, config='--oem 3 --psm 11')

        if not raw_text or not raw_text.strip():
            # Retry with PSM 3 (fully automatic page segmentation)
            raw_text = pytesseract.image_to_string(processed_img, config='--oem 3 --psm 3')

        clean_text = raw_text.strip() if raw_text else ""
        # Clean replacement characters and OCR noise
        clean_text = clean_text.replace('\ufffd', '').replace('', '')
        return True, clean_text, ""

    except Exception as e:
        logger.error(f"OCR execution failed: {e}", exc_info=True)
        return False, "", f"Image recognition failed: {str(e)}"


def parse_candidate_fields(raw_text: str) -> dict:
    """
    Parses candidate medicine parameters from OCR text via regular expressions:
    - strengths: e.g. '500 mg', '650mg', '10 mg/ml', '1% w/w', '250 mcg'
    - batch numbers: e.g. 'B.No: AB123', 'Batch: 2026-X', 'Lot: 9948'
    - expiry dates: e.g. 'EXP: 12/2027', 'Expiry: 05/26', 'EXP DATE 2028-11'
    - dosage forms: 'tablet', 'capsule', 'syrup', 'suspension', 'cream', 'ointment', 'injection'
    - potential medicine name tokens
    """
    if not raw_text:
        return {
            'strengths': [],
            'dosage_forms': [],
            'batch_numbers': [],
            'expiry_dates': [],
            'tokens': [],
        }

    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    full_text = " ".join(lines)

    # 1. Strengths
    strength_pattern = re.compile(
        r'\b(\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml|iu|%|w\/w|w\/v|mg\/ml))\b',
        re.IGNORECASE
    )
    strengths = list(dict.fromkeys(strength_pattern.findall(full_text)))

    # 2. Dosage Forms
    dosage_keywords = [
        'tablet', 'tablets', 'tab', 'capsule', 'capsules', 'cap',
        'syrup', 'suspension', 'solution', 'injection', 'cream',
        'ointment', 'gel', 'lotion', 'drops', 'inhaler', 'infusion'
    ]
    found_forms = []
    lower_text = full_text.lower()
    for df in dosage_keywords:
        if re.search(r'\b' + re.escape(df) + r'\b', lower_text):
            found_forms.append(df.capitalize())
    dosage_forms = list(dict.fromkeys(found_forms))

    # 3. Batch Numbers
    batch_pattern = re.compile(
        r'(?:batch\s*(?:no\.?|number)?|b\.?\s*no\.?|lot\s*(?:no\.?|number)?|b\/no|bno)[\s.:#-]*([a-zA-Z0-9\/-]{3,25})',
        re.IGNORECASE
    )
    batch_matches = batch_pattern.findall(full_text)
    # Also look for standalone alphanumeric batch-like tokens if labeled
    clean_batches = [b.strip() for b in batch_matches if len(b.strip()) >= 3]

    # 4. Expiry Dates
    expiry_pattern = re.compile(
        r'(?:exp\.?(?:iry)?\s*(?:date)?|expiry\s*(?:date)?|val|bb|use\s*before)[\s.:#-]*((?:\d{1,2}[\/-])?\d{2,4}|\w{3,9}\s*\d{2,4})',
        re.IGNORECASE
    )
    expiry_matches = expiry_pattern.findall(full_text)
    clean_expiries = [e.strip() for e in expiry_matches if len(e.strip()) >= 4]

    # 5. Meaningful Text Tokens (filtering punctuation & short words)
    tokens = []
    for word in re.findall(r'[a-zA-Z0-9]{3,}', full_text):
        w_lower = word.lower()
        if w_lower not in ('the', 'and', 'for', 'with', 'from', 'each', 'contains', 'keep', 'reach', 'children', 'store', 'below', 'mfg', 'lic'):
            tokens.append(word)

    return {
        'strengths': strengths,
        'dosage_forms': dosage_forms,
        'batch_numbers': clean_batches,
        'expiry_dates': clean_expiries,
        'tokens': list(dict.fromkeys(tokens))[:20],
    }


def match_packaging_to_catalogue(raw_text: str, candidate_fields: dict) -> list[dict]:
    """
    Matches parsed text candidates against MySQL database Medicine and MedicineBatch records.
    Returns prioritized candidates with match confidence scores (0-100%).
    """
    from medicines.models import Medicine, MedicineBatch
    from ai_engine.auto_reorder_service import get_usable_stock

    if not raw_text and not candidate_fields:
        return []

    medicines_qs = Medicine.objects.filter(status=Medicine.Status.ACTIVE).select_related('category')
    tokens = candidate_fields.get('tokens', [])
    found_strengths = [s.lower().replace(" ", "") for s in candidate_fields.get('strengths', [])]
    found_forms = [f.lower() for f in candidate_fields.get('dosage_forms', [])]
    raw_lower = raw_text.lower()

    scored_candidates = []

    for med in medicines_qs:
        score = 0
        match_reasons = []

        med_name_clean = med.medicine_name.lower()
        generic_clean = (med.generic_name or "").lower()
        brand_clean = (med.brand_name or "").lower()
        mfr_clean = (med.manufacturer or "").lower()
        strength_clean = (med.strength or "").lower().replace(" ", "")
        form_clean = (med.dosage_form or "").lower()

        # 1. Exact or Substring match on Medicine Name (Weight: 40)
        if med_name_clean and med_name_clean in raw_lower:
            score += 40
            match_reasons.append(f"Medicine name '{med.medicine_name}' found in text (+40%)")
        elif any(part in raw_lower for part in med_name_clean.split() if len(part) >= 4):
            score += 25
            match_reasons.append(f"Partial name match (+25%)")

        # 2. Generic Name match (Weight: 30)
        if generic_clean and generic_clean in raw_lower:
            score += 30
            match_reasons.append(f"Generic name '{med.generic_name}' found in text (+30%)")
        elif any(part in raw_lower for part in generic_clean.split() if len(part) >= 4):
            score += 15
            match_reasons.append(f"Partial generic match (+15%)")

        # 3. Brand Name match (Weight: 25)
        if brand_clean and brand_clean in raw_lower:
            score += 25
            match_reasons.append(f"Brand name '{med.brand_name}' matched (+25%)")

        # 4. Strength match (Weight: 15)
        if strength_clean and any(strength_clean in s or s in strength_clean for s in found_strengths):
            score += 15
            match_reasons.append(f"Strength '{med.strength}' matched (+15%)")

        # 5. Dosage Form match (Weight: 10)
        if form_clean and any(f in form_clean or form_clean in f for f in found_forms):
            score += 10
            match_reasons.append(f"Dosage form '{med.dosage_form}' matched (+10%)")

        # 6. Manufacturer match (Weight: 10)
        if mfr_clean and len(mfr_clean) >= 4 and mfr_clean in raw_lower:
            score += 10
            match_reasons.append(f"Manufacturer '{med.manufacturer}' matched (+10%)")

        # 7. Check if any extracted batch matches this medicine's batches
        matched_batch_obj = None
        for b_cand in candidate_fields.get('batch_numbers', []):
            batch_match = med.batches.filter(batch_number__iexact=b_cand).first()
            if batch_match:
                matched_batch_obj = batch_match
                score += 20
                match_reasons.append(f"Batch number '{b_cand}' matched in inventory (+20%)")
                break

        # Normalize score to max 100
        score = min(100, score)

        # Include candidate if score reaches minimal relevance threshold (>= 25%)
        if score >= 25:
            confidence_level = 'HIGH' if score >= 75 else ('MEDIUM' if score >= 45 else 'LOW')
            usable_stock = get_usable_stock(med)

            scored_candidates.append({
                'medicine_id': med.id,
                'medicine_name': med.medicine_name,
                'generic_name': med.generic_name or '',
                'brand_name': med.brand_name or '',
                'strength': med.strength or '',
                'dosage_form': med.dosage_form or '',
                'manufacturer': med.manufacturer or 'N/A',
                'category': med.category.name if med.category else 'General',
                'internal_barcode': med.effective_internal_barcode,
                'manufacturer_barcode': med.barcode or '',
                'usable_stock': usable_stock,
                'confidence_score': score,
                'confidence_level': confidence_level,
                'match_reasons': match_reasons,
                'matched_batch_number': matched_batch_obj.batch_number if matched_batch_obj else None,
                'extracted_batch_candidate': candidate_fields.get('batch_numbers')[0] if candidate_fields.get('batch_numbers') else None,
                'extracted_expiry_candidate': candidate_fields.get('expiry_dates')[0] if candidate_fields.get('expiry_dates') else None,
                'requires_confirmation': True,
                'is_suggestion': True,
            })

    # Sort descending by confidence score
    scored_candidates.sort(key=lambda c: c['confidence_score'], reverse=True)
    return scored_candidates[:5]  # Top 5 candidates


def process_packaging_image(image_input) -> dict:
    """
    Complete end-to-end OCR processing workflow:
    1. Extracts raw text from packaging image safely.
    2. Identifies candidate packaging fields (strengths, batch, expiry, form).
    3. Matches candidates against MySQL database catalogue.
    4. Returns verified candidate suggestions with confidence levels and full disclaimers.
    """
    success, raw_text, err = extract_raw_text_from_image(image_input)
    if not success:
        return {
            'success': False,
            'message': err,
            'raw_text': '',
            'candidate_fields': {},
            'candidates': [],
            'is_suggestion': True,
            'requires_confirmation': True,
        }

    fields = parse_candidate_fields(raw_text)
    candidates = match_packaging_to_catalogue(raw_text, fields)

    return {
        'success': True,
        'message': f"OCR completed. Extracted {len(raw_text)} character(s) and identified {len(candidates)} candidate match(es).",
        'raw_text': raw_text,
        'candidate_fields': fields,
        'candidates': candidates,
        'candidates_count': len(candidates),
        'has_high_confidence': any(c['confidence_level'] == 'HIGH' for c in candidates),
        'disclaimer': (
            "OCR suggestions are assistive recommendations, not certified facts. "
            "Staff confirmation is mandatory before viewing profile, linking barcodes, or dispensing."
        ),
        'is_suggestion': True,
        'requires_confirmation': True,
    }
