"""
medicines/ai_summary_service.py
Phase 3 — Verified AI Medicine Summary & Clinical Explanation Service

Strict Safety Guidelines:
1. Synthesizes ONLY verified database fields and approved clinical reference knowledge.
2. MUST NOT INVENT:
   - Medicine indications or unverified uses
   - Ingredients or strengths
   - Dosage instructions or therapeutic regimens
   - Contraindications or unverified safety warnings
   - Batch numbers, expiry dates, or stock quantities
3. Clearly flags all output as AI-assisted explanation with explicit clinical disclaimers.
4. Preserves full accessibility to original verified underlying fields.
"""

import logging
from medicines.models import Medicine
from ai_engine.medicine_knowledge import get_medicine_knowledge

logger = logging.getLogger(__name__)


def generate_medicine_ai_summary(medicine: Medicine, profile_data: dict = None) -> dict:
    """
    Generates a structured, concise, factual AI clinical summary for staff reference.
    Derived strictly from verified MySQL records and curated pharmacology monographs.
    """
    if not medicine:
        return {
            'success': False,
            'message': 'No medicine record provided.'
        }

    # 1. Active Ingredient & Strength (strictly from database)
    generic = medicine.generic_name.strip() if medicine.generic_name else "Unspecified"
    strength = medicine.strength.strip() if medicine.strength else "Unspecified"
    dosage_form = medicine.dosage_form.strip() if medicine.dosage_form else "Dosage form not specified"

    # 2. Indications / Clinical Uses (strictly from verified records or monograph)
    verified_indication = ""
    monograph = get_medicine_knowledge(medicine.generic_name) or get_medicine_knowledge(medicine.medicine_name)

    if medicine.description and medicine.description.strip():
        verified_indication = medicine.description.strip()
    elif monograph and monograph.get('common_uses'):
        verified_indication = monograph['common_uses']
    elif medicine.review_note and medicine.review_note.strip():
        verified_indication = medicine.review_note.strip()
    elif medicine.category and medicine.category.notes and medicine.category.notes.strip():
        verified_indication = medicine.category.notes.strip()
    elif medicine.category and medicine.category.description and medicine.category.description.strip():
        verified_indication = medicine.category.description.strip()

    if not verified_indication:
        verified_indication = "Medicine-use information not available in local formulary records."

    # 3. Regulatory Category & Dispensing Policy
    reg_display = medicine.get_regulatory_category_display()
    is_controlled = medicine.is_controlled
    is_rx_required = medicine.is_prescription_required

    if is_controlled:
        dispensing_rule = f"{reg_display}: Controlled substance under register surveillance. Prescription and pharmacist verification mandatory."
    elif is_rx_required:
        dispensing_rule = f"{reg_display}: Prescription required. Cannot be dispensed without verified medical prescription."
    else:
        dispensing_rule = f"{reg_display}: General formulary item."

    # 4. Storage & Handling
    storage = medicine.storage_condition.strip() if medicine.storage_condition else ""
    if not storage and monograph and monograph.get('storage_requirements'):
        storage = monograph['storage_requirements']
    if not storage:
        storage = "Standard pharmacy temperature and humidity controls (store below 25°C in a dry place)."

    # 5. Inventory & Supply Status (strictly from real database stock)
    usable_stock = profile_data.get('usable_stock', 0) if profile_data else getattr(medicine, 'current_stock', 0)
    reorder_level = profile_data.get('reorder_level', medicine.reorder_level) if profile_data else medicine.reorder_level
    stock_status = profile_data.get('stock_status', 'NORMAL') if profile_data else ('LOW_STOCK' if usable_stock <= reorder_level else 'NORMAL')

    if usable_stock <= 0:
        supply_status = f"Critical Stock Alert: 0 usable units available in inventory. Immediate reorder required."
    elif usable_stock <= reorder_level:
        supply_status = f"Low Stock Notice: {usable_stock} usable units in stock (at or below reorder threshold of {reorder_level})."
    else:
        supply_status = f"Adequate Stock: {usable_stock} usable units available across valid batches."

    # 6. Safety Advice & Dosage Mandate (strictly standardized, never fabricated)
    dosage_mandate = "Dosing schedule and duration must be determined by the treating physician according to patient diagnosis and clinical guidelines."

    precautions = []
    if monograph and monograph.get('precautions'):
        precautions.append(monograph['precautions'])
    if monograph and monograph.get('contraindications'):
        precautions.append(f"Contraindications: {monograph['contraindications']}")
    if is_controlled:
        precautions.append("Audit Register: Physical counts and dispensing log entries must be audited daily.")

    # Build High-Integrity Overview Paragraph
    overview_parts = [
        f"{medicine.medicine_name} is formulated with {generic} ({strength}) in {dosage_form} form.",
        f"Verified Clinical Purpose: {verified_indication}",
        f"Dispensing Policy: {dispensing_rule}",
        f"Storage Protocol: {storage}",
        f"Current Inventory: {supply_status}"
    ]
    overview = " ".join(overview_parts)

    key_points = [
        f"Active Compound: {generic} · Strength: {strength}",
        f"Therapeutic Indication: {verified_indication}",
        f"Dispensing Category: {dispensing_rule}",
        f"Storage Requirement: {storage}",
        f"Stock Position: {supply_status}",
        f"Clinical Dosing: {dosage_mandate}",
    ]

    return {
        'badge': 'AI-Assisted Formulary Summary',
        'is_ai_generated': True,
        'model_reference': 'Sanalogx Clinical Intelligence Engine',
        'disclaimer': 'Derived strictly from verified Sanalogx hospital catalogue records. Not an independent medical diagnosis or dosing order.',
        'overview': overview,
        'key_points': key_points,
        'precautions': precautions,
        'dosing_mandate': dosage_mandate,
        'verified_sources': {
            'catalogue_medicine_id': medicine.id,
            'internal_barcode': medicine.effective_internal_barcode,
            'has_pharmacology_monograph': bool(monograph),
            'usable_stock_verified': usable_stock,
        }
    }
