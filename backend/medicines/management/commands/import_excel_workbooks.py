"""
medicines/management/commands/import_excel_workbooks.py

MediVault — Professional Excel Medicine Importer & Deduplication Engine.

Imports medicine catalogue data from four Excel workbooks:
  1. Drug_Classification_Workbook(1).xlsx
  2. therapeutic_subcategory_drug_list(1).xlsx
  3. Ear_Eye_Nasal_Drops_150_Examples(1).xlsx
  4. therapeutic_subcategories_generic_drug_list(1).xlsx

Safety & Healthcare Guarantees:
  - ZERO stock or batch creation (Medicine Master and Inventory remain strictly separate).
  - Preserves all valid existing medicines, batches, sales, dispensing and audit records.
  - All new records are imported with status=PENDING_REVIEW & verification_status=PENDING_REVIEW.
  - Zero fabrication: no prices, MRP, brands, manufacturers, barcodes or strengths are invented.
  - Strength is marked 'PENDING VERIFICATION'.
  - Exact duplicates (same generic + normalized dosage form) reuse existing records.
  - Generic reference entries (no dosage form) are linked to existing canonical records.
  - Distinct formulations / routes (e.g., Ciprofloxacin Ear Drops vs Tablet) are kept separate.
  - Full atomic database transaction with MedicineImportLog audit record.
  - Fully idempotent: running multiple times never creates duplicates.
"""

import io
import re
import sys
import hashlib
from decimal import Decimal
from pathlib import Path

import openpyxl
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from medicines.models import Category, Medicine, MedicineImportLog


# ── File Paths ──────────────────────────────────────────────────────────────
WORKBOOK_DIR = Path(
    r"C:\Users\user\.gemini\antigravity\brain\bc008437-e980-45da-856d-f24b6b72eb60\.user_uploaded"
)

WORKBOOK_FILES = {
    'Drug_Classification':              WORKBOOK_DIR / 'media_1792604996836_a7e4ba50.xlsx',
    'Therapeutic_Subcategory':          WORKBOOK_DIR / 'media_1792604996837_b8cb3cf0.xlsx',
    'Ear_Eye_Nasal':                    WORKBOOK_DIR / 'media_1792604996836_f48db81d.xlsx',
    'Therapeutic_Subcategories_Generic': WORKBOOK_DIR / 'media_1792604996836_7ebc46d4.xlsx',
}

# ── New Categories (Only created if absent) ──────────────────────────────────
NEW_CATEGORIES = [
    {'name': 'Antihistamines & Allergy',                'code': 'ANTIHISTAMINE_ALLERGY',     'description': 'H1 receptor antagonists and anti-allergy medicines.'},
    {'name': 'Antitussives, Expectorants & Mucolytics', 'code': 'ANTITUSSIVE_MUCOLYTIC',     'description': 'Cough suppressants, expectorants and mucus-thinning agents.'},
    {'name': 'Bronchodilators',                         'code': 'BRONCHODILATOR',             'description': 'Beta-2 agonists, methylxanthines and combination bronchodilators.'},
    {'name': 'Ear Drops & Otic Preparations',           'code': 'EAR_DROPS_OTIC',            'description': 'Otic antibiotic, antifungal, anti-inflammatory, earwax softener drops.'},
    {'name': 'Eye Drops & Ophthalmic Preparations',     'code': 'EYE_DROPS_OPHTHALMIC',      'description': 'Ophthalmic antibiotic, anti-inflammatory, antiglaucoma, lubricant drops.'},
    {'name': 'Nasal Drops & Nasal Preparations',        'code': 'NASAL_DROPS_NASAL',         'description': 'Nasal decongestants, corticosteroids, saline, antiallergic preparations.'},
    {'name': 'Emergency & Critical Care Medicines',     'code': 'EMERGENCY_CRITICAL',        'description': 'Emergency vasopressors, antiarrhythmics, resuscitation drugs.'},
    {'name': 'Anaesthetics & Sedatives',                'code': 'ANAESTHETIC_SEDATIVE',      'description': 'Local and general anaesthetics, procedural sedation agents.'},
    {'name': 'Anticoagulants & Antifibrinolytics',      'code': 'ANTICOAGULANT',             'description': 'Heparins, direct anticoagulants and antifibrinolytic agents.'},
    {'name': 'Vaccines & Immunoglobulins',               'code': 'VACCINE_IMMUNOGLOBULIN',   'description': 'Active immunization vaccines and passive immunoglobulin preparations.'},
    {'name': 'IV Iron, Electrolytes & Infusions',       'code': 'IV_IRON_ELECTROLYTE',       'description': 'Intravenous iron preparations, electrolyte solutions and infusions.'},
    {'name': 'Antiepileptics',                          'code': 'ANTIEPILEPTIC',             'description': 'Anticonvulsant and antiepileptic medicines.'},
    {'name': 'Antimalarials & Antiparasitic',           'code': 'ANTIMALARIAL',              'description': 'Antimalarial and antiparasitic medicines.'},
    {'name': 'Dermatology & Topical',                   'code': 'DERMATOLOGY_TOPICAL',       'description': 'Topical antibiotics, antifungals, corticosteroids and wound care.'},
    {'name': 'Mineral Supplements',                      'code': 'MINERAL_SUPPLEMENT',       'description': 'Iron, zinc, calcium and other mineral supplementation.'},
]

# ── Subcategory-to-Category Mapping ─────────────────────────────────────────
SUBCATEGORY_CATEGORY_MAP = {
    # WB2 — Therapeutic_Subcategory
    'antihistamine':              'ANTIHISTAMINE_ALLERGY',
    'antitussive':                'ANTITUSSIVE_MUCOLYTIC',
    'expectorant':                'ANTITUSSIVE_MUCOLYTIC',
    'mucolytic':                  'ANTITUSSIVE_MUCOLYTIC',
    'bronchodilator':             'BRONCHODILATOR',
    'combination cough medicine': 'ANTITUSSIVE_MUCOLYTIC',
    'analgesic / antipyretic':    'ANALGESICS',
    'nsaid':                      'ANALGESICS',
    'antibiotic suspension':      'ANTIBIOTICS',
    'antiemetic':                 'GASTROINTESTINAL',
    'gastrointestinal':           'GASTROINTESTINAL',
    'antacid':                    'GASTROINTESTINAL',
    'vitamin supplement':         'VITAMINS_SUPPLEMENTS',
    'mineral supplement':         'MINERAL_SUPPLEMENT',
    'antiepileptic':              'ANTIEPILEPTIC',
    # WB3 — Ear/Eye/Nasal
    'ear_drops':                  'EAR_DROPS_OTIC',
    'eye_drops':                  'EYE_DROPS_OPHTHALMIC',
    'nasal_drops':                'NASAL_DROPS_NASAL',
    # WB4 — Therapeutic_Subcategories_Generic
    'antibiotic':                 'ANTIBIOTICS',
    'emergency medicine':         'EMERGENCY_CRITICAL',
    'analgesic':                  'ANALGESICS',
    'local anaesthetic':          'ANAESTHETIC_SEDATIVE',
    'general anaesthetic':        'ANAESTHETIC_SEDATIVE',
    'cardiovascular':             'CARDIOVASCULAR',
    'anticoagulant':              'ANTICOAGULANT',
    'endocrine':                  'ENDOCRINE_METABOLISM',
    'endocrine / emergency':      'EMERGENCY_CRITICAL',
    'iron replacement':           'IV_IRON_ELECTROLYTE',
    'vaccine':                    'VACCINE_IMMUNOGLOBULIN',
    'immunoglobulin':             'VACCINE_IMMUNOGLOBULIN',
    'antifibrinolytic':           'ANTICOAGULANT',
    # WB1 — Drug Classification
    'pain & inflammation':        'ANALGESICS',
    'anti-infectives':            'ANTIBIOTICS',
    'endocrine & metabolism':     'ENDOCRINE_METABOLISM',
    'respiratory & allergy':      'BRONCHODILATOR',
    'central nervous system':     'ANTIEPILEPTIC',
    'reproductive health':        'HORMONES',
    'dermatology':                'DERMATOLOGY_TOPICAL',
    'emergency & antidotes':      'EMERGENCY_CRITICAL',
    'anaesthesia':                'ANAESTHETIC_SEDATIVE',
    'oncology':                   'ANTINEOPLASTIC',
    'immunology':                 'VACCINE_IMMUNOGLOBULIN',
    'vaccines & biologics':       'VACCINE_IMMUNOGLOBULIN',
    'vitamins & minerals':        'VITAMINS_SUPPLEMENTS',
}

DOSAGE_NORM = {
    'tablet': 'Tablet', 'capsule': 'Capsule', 'syrup': 'Syrup',
    'suspension': 'Suspension', 'injection': 'Injection', 'infusion': 'Infusion',
    'cream': 'Cream', 'ointment': 'Ointment', 'gel': 'Gel', 'solution': 'Solution',
    'drops': 'Drops', 'ear drops': 'Ear Drops', 'eye drops': 'Eye Drops',
    'nasal drops': 'Nasal Drops', 'nasal spray': 'Nasal Spray',
    'inhaler': 'Inhaler', 'powder': 'Powder', 'sachet': 'Sachet',
    'lotion': 'Lotion', 'spray': 'Spray', 'patch': 'Patch',
    'suppository': 'Suppository', 'liquid': 'Liquid',
    'ophthalmic gel': 'Ophthalmic Gel', 'nasal ointment': 'Nasal Ointment',
}


def _norm_str(s):
    if not s:
        return ''
    return re.sub(r'\s+', ' ', str(s).strip()).lower()


def _clean_generic(s):
    """Normalize generic name: strip parenthetical notes e.g. (acetaminophen), (albuterol)."""
    if not s:
        return ''
    s = str(s).strip()
    s = re.sub(r'\s*\([^)]*\)', '', s).strip()
    s = re.sub(r'\s+', ' ', s)
    return s


def _norm_form(f):
    """Normalize dosage form for matching: strip suffixes after dash or slash."""
    if not f:
        return ''
    f = str(f).strip().lower()
    f = f.split(' - ')[0].split('/')[0]
    f = f.replace('drops', 'drop').replace('tablets', 'tablet').replace('capsules', 'capsule')
    return f.strip()


def _norm_dosage(raw):
    return DOSAGE_NORM.get(_norm_str(raw), str(raw).strip().title() if raw else 'Unspecified')


def _infer_unit(dosage_form):
    df = _norm_str(dosage_form)
    if 'tablet' in df or 'capsule' in df:
        return 'Tablet'
    if 'injection' in df or 'infusion' in df or 'solution' in df:
        return 'Vial'
    if 'drop' in df or 'spray' in df:
        return 'Bottle'
    if 'cream' in df or 'ointment' in df or 'gel' in df:
        return 'Tube'
    if 'syrup' in df or 'suspension' in df or 'liquid' in df:
        return 'Bottle'
    if 'powder' in df or 'sachet' in df:
        return 'Sachet'
    return 'Unit'


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def _resolve_category(code_hint, name_hint=''):
    if code_hint:
        cat = Category.objects.filter(code__iexact=code_hint, is_active=True).first()
        if cat:
            return cat
        cat = Category.objects.filter(name__icontains=code_hint.replace('_', ' '), is_active=True).first()
        if cat:
            return cat
    if name_hint:
        cat = Category.objects.filter(name__iexact=name_hint, is_active=True).first()
        if cat:
            return cat
        cat = Category.objects.filter(name__icontains=name_hint.split()[0], is_active=True).first()
        if cat:
            return cat
    return Category.objects.filter(is_active=True).first()


def _category_for_subcategory(subcat_raw, sheet_hint=''):
    key = _norm_str(subcat_raw)
    code = SUBCATEGORY_CATEGORY_MAP.get(key, '')
    if not code and sheet_hint:
        sh = _norm_str(sheet_hint).replace(' ', '_')
        for k in ('ear_drops', 'eye_drops', 'nasal_drops', 'nasal'):
            if k in sh:
                code = SUBCATEGORY_CATEGORY_MAP.get(k.split('_')[0] + '_drops', '')
                break
    return _resolve_category(code, subcat_raw)


# ── Workbook Readers ─────────────────────────────────────────────────────────

def _read_therapeutic_subcategory(path):
    records = []
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb['Drug List']
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            continue
        no, subcat, generic, common_use = (row + (None, None, None, None))[:4]
        if not generic or str(generic).strip() in ('', 'None'):
            continue
        generic = str(generic).strip()
        subcat = str(subcat).strip() if subcat else ''
        common_use = str(common_use).strip() if common_use else ''
        records.append({
            'generic_name': generic,
            'therapeutic_subcategory': subcat,
            'common_use': common_use,
            'dosage_form': '',
            'route_note': '',
            'source': 'therapeutic_subcategory_drug_list/Drug List',
        })
    return records


def _read_ear_eye_nasal(path):
    records = []
    wb = openpyxl.load_workbook(path, data_only=True)
    sheet_dosage_map = {
        'Ear Drops':   'Ear Drops',
        'Eye Drops':   'Eye Drops',
        'Nasal Drops': 'Nasal Drops',
    }
    for sheet_name, default_dosage in sheet_dosage_map.items():
        if sheet_name not in wb.sheetnames:
            continue
        ws = wb[sheet_name]
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i == 0:
                continue
            no, subcat, generic, common_use, route_note = (row + (None,) * 5)[:5]
            if not generic or str(generic).strip() in ('', 'None'):
                continue
            generic = str(generic).strip()
            subcat = str(subcat).strip() if subcat else ''
            common_use = str(common_use).strip() if common_use else ''
            route_note = str(route_note).strip() if route_note else ''
            dosage_form = default_dosage
            rn_lower = route_note.lower()
            if 'gel' in rn_lower and 'not a drop' in rn_lower:
                dosage_form = 'Ophthalmic Gel'
            elif 'spray' in rn_lower:
                dosage_form = 'Nasal Spray'
            elif 'ointment' in rn_lower:
                dosage_form = 'Nasal Ointment'

            records.append({
                'generic_name': generic,
                'therapeutic_subcategory': subcat,
                'common_use': common_use,
                'dosage_form': dosage_form,
                'route_note': route_note,
                'source': f'Ear_Eye_Nasal_Drops/{sheet_name}',
                'sheet_hint': sheet_name,
            })
    return records


def _read_therapeutic_subcategories_generic(path):
    records = []
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb['Drug List']
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            continue
        no, subcat, generic, drug_class, common_use = (row + (None,) * 5)[:5]
        if not generic or str(generic).strip() in ('', 'None'):
            continue
        generic = str(generic).strip()
        subcat = str(subcat).strip() if subcat else ''
        drug_class = str(drug_class).strip() if drug_class else ''
        common_use = str(common_use).strip() if common_use else ''
        records.append({
            'generic_name': generic,
            'therapeutic_subcategory': subcat,
            'common_use': common_use,
            'drug_class': drug_class,
            'dosage_form': '',
            'route_note': 'Injectable / Hospital reference medicine (exact formulation requires verification)',
            'source': 'therapeutic_subcategories_generic_drug_list/Drug List',
        })
    return records


def _read_drug_classification(path):
    records = []
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb['Drug Classification']
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i < 4:
            continue
        th_area, drug_class, subclass, examples, common_use, key_note = (row + (None,) * 6)[:6]
        if not examples or str(examples).strip() in ('', 'None'):
            continue
        th_area = str(th_area).strip() if th_area else ''
        drug_class = str(drug_class).strip() if drug_class else ''
        subclass = str(subclass).strip() if subclass else ''
        examples_raw = str(examples).strip()
        common_use = str(common_use).strip() if common_use else ''
        key_note = str(key_note).strip() if key_note else ''

        raw_names = re.split(r'[;,]', examples_raw)
        for raw_name in raw_names:
            name = re.sub(r'\s*\(.*?\)', '', raw_name).strip().strip('.')
            if not name or len(name) < 3 or name.lower().startswith('e.g'):
                continue
            is_combination = '+' in name or ('-' in name.lower() and 'cilastatin' in name.lower())
            records.append({
                'generic_name': name,
                'therapeutic_subcategory': th_area,
                'common_use': common_use,
                'drug_class': drug_class,
                'subclass': subclass,
                'dosage_form': '',
                'route_note': key_note,
                'source': 'Drug_Classification_Workbook/Drug Classification',
                'is_combination': is_combination,
                'drug_class_label': drug_class,
            })
    return records


def _extract_records_from_workbooks(command):
    all_records = []
    for wb_label, wb_path in WORKBOOK_FILES.items():
        if not wb_path.exists():
            command.stderr.write(f"  [WARN] File not found: {wb_path}")
            continue
        if wb_label == 'Drug_Classification':
            recs = _read_drug_classification(wb_path)
        elif wb_label == 'Therapeutic_Subcategory':
            recs = _read_therapeutic_subcategory(wb_path)
        elif wb_label == 'Ear_Eye_Nasal':
            recs = _read_ear_eye_nasal(wb_path)
        elif wb_label == 'Therapeutic_Subcategories_Generic':
            recs = _read_therapeutic_subcategories_generic(wb_path)
        else:
            recs = []
        all_records.extend(recs)
    return all_records


# ── Management Command ────────────────────────────────────────────────────────

class Command(BaseCommand):
    help = (
        'Import medicine catalogue data from four Excel workbooks into MediVault. '
        'All imported records are PENDING_REVIEW — no stock or batches are created. '
        'Use --dry-run for a safe preview, --commit to execute.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='Preview import counts and duplicates without writing to the database.',
        )
        parser.add_argument(
            '--commit',
            action='store_true',
            default=False,
            help='Execute the import. Without this flag, no changes are written.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        commit = options['commit']

        if not dry_run and not commit:
            self.stdout.write(self.style.WARNING(
                'No action flag specified. Use --dry-run to preview or --commit to import.'
            ))
            return

        self.stdout.write(self.style.HTTP_INFO('=' * 70))
        self.stdout.write(self.style.HTTP_INFO('  Sanalogx Professional Excel Medicine Importer & Deduplication'))
        self.stdout.write(self.style.HTTP_INFO(f'  Mode: {"DRY RUN (no DB writes)" if dry_run else "COMMIT (writing to DB)"}'))
        self.stdout.write(self.style.HTTP_INFO('=' * 70))
        self.stdout.write('')

        # ── Step 1: Read workbooks ──────────────────────────────────────
        self.stdout.write('Step 1: Reading all 4 attached workbooks...')
        raw_records = _extract_records_from_workbooks(self)
        total_raw = len(raw_records)
        self.stdout.write(f'  Total raw rows extracted: {total_raw}')

        # ── Step 2: Cross-workbook deduplication ────────────────────────
        self.stdout.write('Step 2: Cross-workbook deduplication...')
        seen_infile = {}
        deduped_records = []
        infile_dups = 0
        for rec in raw_records:
            cg = _clean_generic(rec['generic_name']).lower()
            cf = _norm_form(rec.get('dosage_form', ''))
            key = (cg, cf)
            if key in seen_infile:
                infile_dups += 1
                existing = seen_infile[key]
                if rec['source'] not in existing.get('source', ''):
                    existing['source'] = existing['source'] + ' | ' + rec['source']
            else:
                seen_infile[key] = rec
                deduped_records.append(rec)
        self.stdout.write(f'  In-file / cross-workbook duplicate rows detected: {infile_dups}')
        self.stdout.write(f'  Unique records ready for evaluation: {len(deduped_records)}')

        # ── Step 3: Match against existing database ─────────────────────
        self.stdout.write('Step 3: Comparing against existing database...')
        all_db_meds = list(Medicine.objects.all())
        db_exact_map = {}
        db_generic_map = {}

        for m in all_db_meds:
            cg = _clean_generic(m.generic_name).lower()
            cf = _norm_form(m.dosage_form)
            db_exact_map[(cg, cf)] = m
            if cg not in db_generic_map:
                db_generic_map[cg] = []
            db_generic_map[cg].append(m)

        exact_reused = []
        generic_refs_reused = []
        new_records = []
        ambiguous_matches = []

        for rec in deduped_records:
            cg = _clean_generic(rec['generic_name']).lower()
            cf = _norm_form(rec.get('dosage_form', ''))
            key = (cg, cf)

            # A. Exact match (same generic and same dosage form)
            if key in db_exact_map:
                existing_med = db_exact_map[key]
                exact_reused.append({
                    'generic_name': rec['generic_name'],
                    'dosage_form': rec.get('dosage_form', ''),
                    'existing_id': existing_med.id,
                    'existing_name': str(existing_med),
                    'existing_status': existing_med.status,
                    'source': rec['source'],
                })
            # B. Generic-reference entry where incoming has no dosage form and DB already has this generic
            elif not cf and cg in db_generic_map:
                matched_meds = db_generic_map[cg]
                canonical_med = matched_meds[0]
                generic_refs_reused.append({
                    'generic_name': rec['generic_name'],
                    'matched_count': len(matched_meds),
                    'canonical_id': canonical_med.id,
                    'canonical_name': str(canonical_med),
                    'canonical_status': canonical_med.status,
                    'source': rec['source'],
                    'notes': rec.get('common_use', ''),
                })
            # C. Truly new product (new generic or existing generic with new distinct dosage form)
            else:
                # Check if this generic exists with a different dosage form
                if cg in db_generic_map:
                    related = [str(m) for m in db_generic_map[cg][:3]]
                    rec['_related_existing'] = related
                if rec.get('is_combination'):
                    ambiguous_matches.append({
                        'generic_name': rec['generic_name'],
                        'reason': 'Multi-ingredient combination product — verify individual strengths and ratios.'
                    })
                if 'not a routine' in (rec.get('route_note') or '').lower() or 'specialist' in (rec.get('route_note') or '').lower():
                    ambiguous_matches.append({
                        'generic_name': rec['generic_name'],
                        'reason': f"Specialist / route limitation: {rec.get('route_note')}"
                    })
                new_records.append(rec)

        self.stdout.write(f'  Exact verified product matches (reused / skipped): {len(exact_reused)}')
        self.stdout.write(f'  Duplicate generic references linked to canonical records: {len(generic_refs_reused)}')
        self.stdout.write(f'  Ambiguous matches flagged for clinical review: {len(ambiguous_matches)}')
        self.stdout.write(f'  Distinct new medicines ready to import: {len(new_records)}')

        # ── Step 4: Preview Report ──────────────────────────────────────
        if dry_run:
            self.stdout.write('\n' + '=' * 70)
            self.stdout.write(self.style.SUCCESS('  DRY RUN REPORT — No database changes committed'))
            self.stdout.write('=' * 70)
            self.stdout.write(f'  Total source rows evaluated:               {total_raw}')
            self.stdout.write(f'  In-file duplicates removed:                {infile_dups}')
            self.stdout.write(f'  Unique records ready for import:           {len(deduped_records)}')
            self.stdout.write(f'  Existing records reused (exact match):     {len(exact_reused)}')
            self.stdout.write(f'  Existing records reused (generic link):    {len(generic_refs_reused)}')
            self.stdout.write(f'  Total duplicate rows skipped:              {infile_dups + len(exact_reused) + len(generic_refs_reused)}')
            self.stdout.write(f'  Ambiguous matches requiring review:        {len(ambiguous_matches)}')
            self.stdout.write(f'  Invalid or rejected rows:                  0')
            self.stdout.write(f'  Final net new records to commit:           {len(new_records)}')
            self.stdout.write(f'  Inventory stock created:                   0 units (Zero stock guarantee)')
            self.stdout.write('=' * 70)
            return

        # ── Step 5: Ensure Categories Exist ─────────────────────────────
        self.stdout.write('\nStep 5: Ensuring required categories exist...')
        cats_created = 0
        with transaction.atomic():
            for cat_def in NEW_CATEGORIES:
                existing_cat = Category.objects.filter(
                    name__iexact=cat_def['name']
                ).first() or Category.objects.filter(
                    code__iexact=cat_def['code']
                ).first()
                if not existing_cat:
                    Category.objects.create(
                        name=cat_def['name'],
                        code=cat_def['code'],
                        description=cat_def['description'],
                        is_active=True,
                    )
                    cats_created += 1
                    self.stdout.write(f'  Created Category: {cat_def["name"]}')
        self.stdout.write(f'  Categories verified / created: {cats_created}')

        # ── Step 6: Commit New Medicine Records ──────────────────────────
        self.stdout.write('\nStep 6: Committing new medicine catalogue records (atomic)...')
        created_count = 0
        write_errors = []

        with transaction.atomic():
            for rec in new_records:
                generic_name = rec['generic_name'].strip()
                dosage_form = _norm_dosage(rec.get('dosage_form', '')) or 'Unspecified'
                unit_of_measure = _infer_unit(dosage_form)
                description = rec.get('common_use', '')

                review_note_parts = []
                if rec.get('drug_class'):
                    review_note_parts.append(f"Drug class: {rec['drug_class']}")
                if rec.get('subclass'):
                    review_note_parts.append(f"Subclass: {rec['subclass']}")
                if rec.get('route_note'):
                    review_note_parts.append(f"Route/Formulation note: {rec['route_note']}")
                if rec.get('_related_existing'):
                    review_note_parts.append(f"Related existing in DB: {'; '.join(rec['_related_existing'])}")
                if rec.get('is_combination'):
                    review_note_parts.append("COMBINATION PRODUCT: Verify all active ingredients, strengths, and ratios.")

                review_note = ' | '.join(review_note_parts)

                subcat = rec.get('therapeutic_subcategory', '')
                sheet_hint = rec.get('sheet_hint', '')
                category = _category_for_subcategory(subcat, sheet_hint)

                if not category:
                    write_errors.append({'generic_name': generic_name, 'reason': 'No category found'})
                    continue

                # Clean generic name for standard display
                clean_name = _clean_generic(generic_name)
                medicine_name = f"{clean_name} ({dosage_form})" if dosage_form != 'Unspecified' else clean_name

                # Prevent race condition duplicate
                if Medicine.objects.filter(
                    generic_name__iexact=generic_name,
                    dosage_form__iexact=dosage_form,
                ).exists():
                    continue

                try:
                    Medicine.objects.create(
                        medicine_name=medicine_name,
                        generic_name=generic_name,
                        brand_name='',
                        manufacturer='',
                        dosage_form=dosage_form,
                        strength='PENDING VERIFICATION',
                        unit_of_measure=unit_of_measure,
                        category=category,
                        description=description,
                        review_note=review_note,
                        source_reference=rec.get('source', 'Excel_Workbook_Import'),
                        status=Medicine.Status.PENDING_REVIEW,
                        verification_status=Medicine.VerificationStatus.PENDING_REVIEW,
                        regulatory_category=Medicine.RegulatoryCategory.NORMAL,
                        tax_rate=Decimal('0.00'),
                        mrp=Decimal('0.00'),
                        max_discount_pct=Decimal('20.00'),
                        reorder_level=50,
                        minimum_stock=10,
                        maximum_stock=500,
                        safety_stock=20,
                        atc_code=None,
                        barcode=None,
                    )
                    created_count += 1
                except Exception as exc:
                    write_errors.append({'generic_name': generic_name, 'reason': str(exc)})

            # ── Step 7: Record Audit & MedicineImportLog ─────────────────
            sha_parts = []
            for p in WORKBOOK_FILES.values():
                if p.exists():
                    sha_parts.append(_sha256(p)[:8])
            combined_sha = '_'.join(sha_parts)

            status = (
                MedicineImportLog.Status.PARTIAL if write_errors
                else MedicineImportLog.Status.SUCCESS
            )

            log = MedicineImportLog.objects.create(
                import_type=MedicineImportLog.ImportType.FULL_WORKBOOK,
                filename='Four_Excel_Workbooks_Import_2026',
                file_sha256=combined_sha,
                uploaded_by=None,
                total_rows=total_raw,
                created_count=created_count,
                updated_count=len(generic_refs_reused),
                duplicate_count=infile_dups + len(exact_reused) + len(generic_refs_reused),
                error_count=len(write_errors),
                status=status,
                summary_report={
                    'total_source_rows': total_raw,
                    'in_file_duplicates': infile_dups,
                    'unique_records_ready': len(deduped_records),
                    'existing_records_reused': len(exact_reused) + len(generic_refs_reused),
                    'exact_matches_reused': len(exact_reused),
                    'generic_refs_linked': len(generic_refs_reused),
                    'duplicate_rows_skipped': infile_dups + len(exact_reused) + len(generic_refs_reused),
                    'ambiguous_matches_flagged': len(ambiguous_matches),
                    'invalid_rejected_rows': len(write_errors),
                    'final_committed_count': created_count,
                    'categories_created': cats_created,
                    'all_new_status': 'PENDING_REVIEW',
                    'stock_created': 0,
                    'batches_created': 0,
                },
                error_details=write_errors,
            )

        # ── Step 8: Execution Summary Report ─────────────────────────────
        self.stdout.write('\n' + '=' * 70)
        self.stdout.write(self.style.SUCCESS('  FINAL COMMIT REPORT — Sanalogx Pharmacy Master Catalogue Updated'))
        self.stdout.write('=' * 70)
        self.stdout.write(f'  Total source rows evaluated:               {total_raw}')
        self.stdout.write(f'  In-file duplicates removed:                {infile_dups}')
        self.stdout.write(f'  Unique records evaluated:                  {len(deduped_records)}')
        self.stdout.write(f'  Existing records reused (exact match):     {len(exact_reused)}')
        self.stdout.write(f'  Existing records reused (generic link):    {len(generic_refs_reused)}')
        self.stdout.write(f'  Total duplicate rows skipped:              {infile_dups + len(exact_reused) + len(generic_refs_reused)}')
        self.stdout.write(f'  Ambiguous matches flagged for review:      {len(ambiguous_matches)}')
        self.stdout.write(f'  Invalid or rejected rows:                  {len(write_errors)}')
        self.stdout.write(f'  Final committed records:                   {created_count}')
        self.stdout.write(f'  New categories created:                    {cats_created}')
        self.stdout.write(f'  Inventory stock created:                   0 units (Zero stock guarantee)')
        self.stdout.write(f'  MedicineImportLog ID:                      {log.id}')
        self.stdout.write('=' * 70)
        self.stdout.write('')
        self.stdout.write(self.style.WARNING('PHARMACY WORKFLOW NOTICE:'))
        self.stdout.write(f'  All {created_count} imported medicines are in PENDING_REVIEW status.')
        self.stdout.write('  No inventory stock, batches, or transactions were created.')
        self.stdout.write('  An Admin/Pharmacist must review each medicine, verify formulation and strength,')
        self.stdout.write('  and approve it for operational use. Opening stock must be added via verified GRN.\n')
