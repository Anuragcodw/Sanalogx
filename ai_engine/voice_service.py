"""
ai_engine/voice_service.py
Phase 6 — Sanalogx AI Voice Assistant Service Engine
Voice-Controlled Hospital Pharmacy Workflows with Strict Safety & Auditing

Key Capabilities:
1. Natural language processing supporting English, Hindi, and Hinglish pharmacy speech.
2. Robust hybrid intent detection:
   - High-precision deterministic regex & pattern matcher.
   - ML Intent Classifier fallback when trained model files are present.
   - AI Chatbot delegation for general clinical/pharmacology inquiries.
3. Medicine disambiguation: When multiple active medicines match a spoken name,
   returns structured choices so the user can select the intended medicine.
4. Core Voice Workflow Intents:
   - CHECK_MEDICINE_STOCK: Live usable stock across active batches, status, dosage form.
   - CHECK_LOW_STOCK: Medicines at or below reorder level requiring replenishment.
   - CHECK_OUT_OF_STOCK: Medicines with zero usable inventory.
   - CHECK_EXPIRY: Date-range parsing (next month, 30/60/90 days, next week).
   - STOCK_MOVEMENTS: Today's transactions, additions vs deductions.
   - PENDING_PURCHASE_REQUESTS: Pending procurement requests awaiting approval.
   - REORDER_RECOMMENDATIONS: Stock items needing reorder from AI/min-max analysis.
   - NAVIGATION: Deep links to existing hospital pharmacy workspaces.
5. Consequential Stock Addition Workflow ("Add 50 Paracetamol tablets"):
   - 2-Step Confirmation: Never modifies stock on speech recognition alone.
   - Permission Verification: Pharmacist or Admin role required.
   - Batch Validity: Requires an active, non-expired batch; NEVER invents fictitious batches.
   - Secure Confirmation Token: Cached with 5-minute timeout.
   - Execution (CONFIRM_STOCK_ACTION): Calls inventory.services.add_stock, creates StockTransaction,
     records AuditLog, and invalidates token (idempotent).
   - Cancellation (CANCEL_STOCK_ACTION): Discards token leaving inventory untouched.
6. Voice-driven Purchase Request Creation ("Create purchase request for 100 units of Paracetamol"):
   - Permission Verification: Pharmacist or Admin role required.
   - Calls auto_reorder_service.create_auto_reorder_request with PENDING_APPROVAL status.
   - Prevents duplicate requests if an open PO or active PR already exists.
"""

import os
import re
import uuid
import joblib
import logging
from decimal import Decimal
from datetime import timedelta, date, datetime

from django.db import transaction
from django.db.models import Sum, Count, Q, F
from django.utils import timezone
from django.core.cache import cache

from accounts.models import User
from medicines.models import Medicine, MedicineBatch
from inventory.models import Inventory, StockAlert, StockTransaction
from purchases.models import PurchaseRequest, PurchaseRequestItem, PurchaseOrder, PurchaseOrderItem
from .models import VoiceCommandLog, AuditLog

logger = logging.getLogger(__name__)

# ML model singletons
_model = None
_vectorizer = None
_MODEL_LOADED = False

# ─── Brand & Common Aliases Dictionary ─────────────────────────────────────────
MEDICINE_ALIASES = {
    'dolo': 'Paracetamol',
    'dolo 650': 'Paracetamol',
    'dolo-650': 'Paracetamol',
    'calpol': 'Paracetamol',
    'crocin': 'Paracetamol',
    'pacimol': 'Paracetamol',
    'augmentin': 'Amoxicillin',
    'mox': 'Amoxicillin',
    'novamox': 'Amoxicillin',
    'azithral': 'Azithromycin',
    'zithromax': 'Azithromycin',
    'cifran': 'Ciprofloxacin',
    'ciplox': 'Ciprofloxacin',
    'cipro': 'Ciprofloxacin',
    'flagyl': 'Metronidazole',
    'metrogyl': 'Metronidazole',
    'brufen': 'Ibuprofen',
    'advil': 'Ibuprofen',
    'voveran': 'Diclofenac Sodium',
    'voltaren': 'Diclofenac Sodium',
    'tramazac': 'Tramadol',
    'ultram': 'Tramadol',
    'glycomet': 'Metformin',
    'glucophage': 'Metformin',
    'daonil': 'Glibenclamide',
    'lantus': 'Insulin Glargine',
    'amlong': 'Amlodipine',
    'norvasc': 'Amlodipine',
    'omez': 'Omeprazole',
    'prilosec': 'Omeprazole',
    'pan 40': 'Pantoprazole',
    'pantocid': 'Pantoprazole',
    'cetzine': 'Cetirizine',
    'zyrtec': 'Cetirizine',
    'asthalin': 'Salbutamol Inhaler',
    'ventolin': 'Salbutamol Inhaler',
    'atorva': 'Atorvastatin',
    'lipitor': 'Atorvastatin',
    'ecosprin': 'Aspirin',
    'disprin': 'Aspirin',
}

STOPWORDS = {
    'show', 'find', 'get', 'check', 'how', 'many', 'what', 'is', 'the', 'of', 'in',
    'available', 'stock', 'tablet', 'tablets', 'capsule', 'capsules', 'unit', 'units',
    'strip', 'strips', 'bottle', 'bottles', 'box', 'boxes', 'vial', 'vials', 'please',
    'tell', 'me', 'list', 'display', 'current', 'are', 'there', 'any', 'dawai', 'dawain',
    'kitna', 'kitni', 'hai', 'karo', 'batao', 'dikhao', 'kijiye', 'bachi', 'khatam',
    'add', 'order', 'create', 'purchase', 'request', 'open', 'navigate', 'to', 'for'
}


def get_voice_model():
    """Safely loads ML model and vectorizer if present; does not throw if absent."""
    global _model, _vectorizer, _MODEL_LOADED
    if not _MODEL_LOADED:
        _MODEL_LOADED = True
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        model_path = os.path.join(base_dir, "ml", "models", "voice_intent_model.joblib")
        vec_path = os.path.join(base_dir, "ml", "models", "voice_vectorizer.joblib")
        if os.path.exists(model_path) and os.path.exists(vec_path):
            try:
                _model = joblib.load(model_path)
                _vectorizer = joblib.load(vec_path)
            except Exception as e:
                logger.warning(f"Voice ML model load error: {e}")
                _model = None
                _vectorizer = None
    return _model, _vectorizer


def clean_query_text(text):
    """Normalize input query string."""
    if not isinstance(text, str):
        return ""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s\-]', ' ', text)
    return " ".join(text.split())


def extract_entities(query_text):
    """
    Extract quantity, batch number, date horizon, and target medicine.
    Handles exact names, brand aliases, and disambiguates multiple candidates.
    """
    entities = {
        'quantity': None,
        'batch_number': None,
        'days_horizon': 30,
        'medicine': None,
        'is_ambiguous': False,
        'matching_medicines': [],
        'extracted_name': None,
    }

    # 1. Extract quantity (positive integer)
    qty_matches = re.findall(r'\b(\d+)\b', query_text)
    if qty_matches:
        # Check if quantity represents a days duration (e.g. "in 60 days")
        days_match = re.search(r'\b(\d+)\s*(?:days?|din)\b', query_text)
        if days_match:
            entities['days_horizon'] = int(days_match.group(1))
            # Pick any other number as quantity if present
            other_qtys = [int(q) for q in qty_matches if int(q) != entities['days_horizon']]
            if other_qtys:
                entities['quantity'] = other_qtys[0]
        else:
            entities['quantity'] = int(qty_matches[0])

    # 2. Extract batch number if explicitly mentioned (e.g. "batch PARA-2026-A" or "PARA-2026-A")
    batch_pattern = re.search(r'\b(?:batch\s+)?([a-z0-9]+-[a-z0-9\-]+)\b', query_text, re.IGNORECASE)
    if batch_pattern:
        entities['batch_number'] = batch_pattern.group(1).upper()

    # 3. Extract time horizon for expiry queries
    if re.search(r'\b(?:next\s+week|1\s+week|ek\s+hafta|7\s+days?)\b', query_text):
        entities['days_horizon'] = 7
    elif re.search(r'\b(?:next\s+month|1\s+month|agla\s+mahina|next\s+30\s+days?|30\s+days?)\b', query_text):
        entities['days_horizon'] = 30
    elif re.search(r'\b(?:next\s+60\s+days?|60\s+days?|2\s+months?|do\s+mahine)\b', query_text):
        entities['days_horizon'] = 60
    elif re.search(r'\b(?:next\s+90\s+days?|90\s+days?|3\s+months?|teen\s+mahine)\b', query_text):
        entities['days_horizon'] = 90

    # 4. Extract Medicine entity with brand aliases and disambiguation
    # Check aliases first
    candidate_term = None
    for alias, canonical in MEDICINE_ALIASES.items():
        if re.search(r'\b' + re.escape(alias) + r'\b', query_text):
            candidate_term = canonical
            break

    # If no alias matched, check active medicines in database
    active_meds = Medicine.objects.filter(status=Medicine.Status.ACTIVE)

    if not candidate_term:
        # Longest match against existing medicine names or generic names
        best_match_str = ""
        for med in active_meds:
            m_name = med.medicine_name.lower()
            g_name = med.generic_name.lower() if med.generic_name else ""
            if m_name and m_name in query_text and len(m_name) > len(best_match_str):
                best_match_str = m_name
                candidate_term = med.medicine_name
            elif g_name and g_name in query_text and len(g_name) > len(best_match_str):
                best_match_str = g_name
                candidate_term = med.generic_name

    # If still not found, check remaining words after stripping stopwords and numbers
    if not candidate_term:
        words = [w for w in query_text.split() if w not in STOPWORDS and not w.isdigit() and len(w) >= 3]
        for w in words:
            matched = active_meds.filter(
                Q(medicine_name__icontains=w) | Q(generic_name__icontains=w)
            )
            if matched.exists():
                candidate_term = w
                break

    entities['extracted_name'] = candidate_term

    if candidate_term:
        # Search matching medicines
        matches = list(active_meds.filter(
            Q(medicine_name__icontains=candidate_term) | Q(generic_name__icontains=candidate_term)
        ).distinct())

        if len(matches) == 1:
            entities['medicine'] = matches[0]
            entities['is_ambiguous'] = False
        elif len(matches) > 1:
            # Check for an exact name match (e.g. medicine named exactly "Paracetamol")
            exact_matches = [
                m for m in matches
                if m.medicine_name.strip().lower() == candidate_term.strip().lower()
                or (m.generic_name and m.generic_name.strip().lower() == candidate_term.strip().lower())
            ]
            if len(exact_matches) == 1:
                entities['medicine'] = exact_matches[0]
                entities['is_ambiguous'] = False
            else:
                # Truly ambiguous: multiple formulations exist without an exact single match
                entities['medicine'] = None
                entities['is_ambiguous'] = True
                entities['matching_medicines'] = matches[:5]
        else:
            entities['medicine'] = None

    return entities


def detect_intent(cleaned_text, entities):
    """
    Deterministic regex & pattern matcher for 100% precision on pharmacy voice commands.
    Falls back to ML model or general search when regex does not trigger.
    """
    text = cleaned_text

    # 1. Explicit Stock Addition ("Add 50 Paracetamol", "50 Paracetamol add karo")
    if (
        re.search(r'\b(?:add|insert|stock\s+badhao|badhao|daalo|dalen)\b', text)
        and (entities.get('quantity') is not None or re.search(r'\b\d+\b', text))
        and not re.search(r'\b(?:purchase\s+request|purchase\s+order|pr|po)\b', text)
    ) or re.search(r'\b\d+\s+(?:.+?)\s+(?:add|badhao|daalo)(?:\s+karo|\s+kijiye)?\b', text):
        return 'ADD_STOCK_REQUEST', 0.98

    # 2. Purchase Request Creation ("Create purchase request for 100 Paracetamol", "Order 100 Paracetamol")
    if (
        re.search(r'\b(?:create|raise|make|generate|banao)\b.*\b(?:purchase\s+request|pr|order|kharid)\b', text)
        or re.search(r'\b(?:purchase\s+request|pr)\b.*\b(?:banao|create|generate)\b', text)
        or re.search(r'\b(?:order|reorder|procure)\s+\d+\b', text)
    ):
        return 'CREATE_PURCHASE_REQUEST', 0.96

    # 3. Explicit Navigation Commands ("Navigate to expiry management", "Go to inventory", "Open catalogue")
    if re.search(r'\b(?:navigate\s+to|go\s+to)\b', text) or (
        re.search(r'\b(?:open|kholo)\b', text) and not re.search(r'\b\d+\b', text)
    ):
        return 'NAVIGATION', 0.95

    # 4. Stock Movements ("Show today's stock movements", "aaj ke transactions")
    if re.search(r'\b(?:stock\s+movement|movement|movements|today\'?s?\s+transactions?|aaj\s+ke\s+transaction|aaj\s+ki\s+movement|recent\s+movement)\b', text):
        return 'STOCK_MOVEMENTS', 0.95

    # 5. Pending Purchase Requests ("Show pending purchase requests", "pending prs")
    if re.search(r'\b(?:pending\s+purchase\s+requests?|pending\s+prs?|pending\s+requests?|pending\s+orders?|pending\s+kharid)\b', text):
        return 'PENDING_PURCHASE_REQUESTS', 0.95

    # 6. Low Stock ("Show medicines with low stock", "kam stock wali dawaiyan")
    if re.search(r'\b(?:low\s+stock|kam\s+stock|below\s+reorder|stock\s+kam\s+hai|shortage)\b', text):
        return 'CHECK_LOW_STOCK', 0.95

    # 7. Out of Stock ("Find medicines that are out of stock", "khatam dawaiyan")
    if re.search(r'\b(?:out\s+of\s+stock|zero\s+stock|khatam\s+dawai|nil\s+stock|no\s+stock)\b', text):
        return 'CHECK_OUT_OF_STOCK', 0.95

    # 8. Expiry ("Show medicines expiring next month", "near expiry", "expire hone wali")
    if re.search(r'\b(?:expir\w*|near\s+expiry|expire\s+hone|agla\s+mahina\s+expire)', text):
        return 'CHECK_EXPIRY', 0.95

    # 9. Reorder Recommendations ("Show medicines that need reordering", "reorder list")
    if re.search(r'\b(?:need\s+reorder|needing\s+reorder|reorder\s+list|reorder\s+recommendations?|reorder\s+karni|reordering)\b', text):
        return 'REORDER_RECOMMENDATIONS', 0.95

    # 10. Medicine Stock Query ("How many Paracetamol tablets are available?", "Paracetamol stock", "stock kitna hai")
    if (
        entities.get('medicine') is not None
        or re.search(r'\b(?:stock|available|availability|how\s+many|kitna|kitni|bachi)\b', text)
    ):
        if not re.search(r'\b(?:add|increase|order|create)\b', text):
            return 'CHECK_MEDICINE_STOCK', 0.90

    # 11. Confirmation Spoken Shorthand ("Confirm", "Yes confirm", "Proceed", "Cancel")
    if re.search(r'\b(?:confirm|yes|proceed|haan\s+confirm|kardo)\b', text):
        return 'CONFIRM_STOCK_ACTION', 0.99
    if re.search(r'\b(?:cancel|stop|roko|mat\s+karo)\b', text):
        return 'CANCEL_STOCK_ACTION', 0.99

    # 12. Help
    if re.search(r'\b(?:help|madad|kya\s+kar\s+sakte|commands)\b', text):
        return 'HELP', 0.95

    # ML Classifier Fallback if model files exist
    try:
        model, vectorizer = get_voice_model()
        if model and vectorizer:
            tfidf_vec = vectorizer.transform([text])
            probs = model.predict_proba(tfidf_vec)[0]
            max_idx = probs.argmax()
            conf = float(probs[max_idx])
            if conf >= 0.40:
                return model.classes_[max_idx], conf
    except Exception:
        pass

    # If an active medicine was identified, treat as stock check
    if entities.get('medicine') or entities.get('is_ambiguous'):
        return 'CHECK_MEDICINE_STOCK', 0.80

    return 'UNKNOWN', 0.0


# ─────────────────────────────────────────────────────────────────────────────
# INTENT HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

def handle_check_medicine_stock(user, entities, cleaned_text):
    """Queries usable stock from valid batches and reports status."""
    if entities.get('is_ambiguous'):
        meds = entities.get('matching_medicines', [])
        options_str = ", ".join([m.medicine_name for m in meds])
        return {
            'success': True,
            'intent': 'CHECK_MEDICINE_STOCK',
            'confidence': 0.95,
            'is_ambiguous': True,
            'matching_medicines': [{'id': m.id, 'name': m.medicine_name} for m in meds],
            'speech': f"Multiple medicines match '{entities.get('extracted_name')}': {options_str}. Please choose which one you would like to inspect.",
            'action': 'CHOICE_REQUIRED',
            'action_url': '/medicines/'
        }

    med = entities.get('medicine')
    if not med:
        return {
            'success': True,
            'intent': 'CHECK_MEDICINE_STOCK',
            'confidence': 0.80,
            'speech': f"I could not locate that medicine in the catalogue. Opening Medicine Master catalogue.",
            'action': 'REDIRECT',
            'action_url': '/medicines/'
        }

    from .auto_reorder_service import get_usable_stock
    usable_stock = get_usable_stock(med)
    inv = getattr(med, 'inventory', None)
    stock_status = inv.get_stock_status_display() if inv else 'Active'
    unit = med.dosage_form or med.unit_of_measure or 'units'

    # Check active batches
    today = timezone.localdate()
    active_batch_count = med.batches.filter(
        status__in=[MedicineBatch.BatchStatus.ACTIVE, MedicineBatch.BatchStatus.NEAR_EXPIRY],
        expiry_date__gt=today,
        quantity_available__gt=0
    ).count()

    speech = (
        f"{med.medicine_name} currently has {usable_stock} {unit} available across "
        f"{active_batch_count} active batches. Current stock status is {stock_status}."
    )
    return {
        'success': True,
        'intent': 'CHECK_MEDICINE_STOCK',
        'confidence': 0.95,
        'speech': speech,
        'medicine_name': med.medicine_name,
        'usable_stock': usable_stock,
        'action': 'REDIRECT',
        'action_url': f"/inventory/medicine/{med.id}/"
    }


def handle_check_low_stock(user, entities):
    """Identifies medicines at or below their reorder level."""
    from .auto_reorder_service import get_usable_stock

    active_meds = Medicine.objects.filter(status=Medicine.Status.ACTIVE).select_related('inventory')
    low_stock_meds = []
    for med in active_meds:
        inv = getattr(med, 'inventory', None)
        reorder_level = inv.reorder_level if (inv and inv.reorder_level is not None) else med.reorder_level
        usable = get_usable_stock(med)
        if usable <= reorder_level:
            low_stock_meds.append((med.medicine_name, usable, reorder_level))

    count = len(low_stock_meds)
    if count == 0:
        speech = "Excellent news! There are currently no medicines with low stock. All inventory levels are above their reorder thresholds."
    else:
        sample_names = ", ".join([name for name, _, _ in low_stock_meds[:3]])
        speech = f"There are currently {count} medicines at or below their reorder level requiring restocking, including {sample_names}. Opening low stock inventory."

    return {
        'success': True,
        'intent': 'CHECK_LOW_STOCK',
        'confidence': 0.95,
        'speech': speech,
        'count': count,
        'action': 'REDIRECT',
        'action_url': '/inventory/?status=LOW_STOCK'
    }


def handle_check_out_of_stock(user, entities):
    """Identifies medicines with 0 inventory."""
    from .auto_reorder_service import get_usable_stock

    active_meds = Medicine.objects.filter(status=Medicine.Status.ACTIVE)
    out_of_stock_names = []
    for med in active_meds:
        usable = get_usable_stock(med)
        if usable == 0:
            out_of_stock_names.append(med.medicine_name)

    count = len(out_of_stock_names)
    if count == 0:
        speech = "All medicines currently have stock available. Zero items are out of stock."
    else:
        sample_names = ", ".join(out_of_stock_names[:3])
        speech = f"There are {count} medicines completely out of stock with zero inventory, including {sample_names}. Opening out-of-stock inventory."

    return {
        'success': True,
        'intent': 'CHECK_OUT_OF_STOCK',
        'confidence': 0.95,
        'speech': speech,
        'count': count,
        'action': 'REDIRECT',
        'action_url': '/inventory/?status=OUT_OF_STOCK'
    }


def handle_check_expiry(user, entities):
    """Queries batches expiring within the specified time horizon."""
    today = timezone.localdate()
    days = entities.get('days_horizon', 30) or 30
    cutoff_date = today + timedelta(days=days)

    expiring_batches = MedicineBatch.objects.filter(
        expiry_date__gt=today,
        expiry_date__lte=cutoff_date,
        quantity_available__gt=0
    ).exclude(
        status__in=[MedicineBatch.BatchStatus.EXPIRED, MedicineBatch.BatchStatus.QUARANTINED, MedicineBatch.BatchStatus.RETURNED]
    ).select_related('medicine').order_by('expiry_date')

    count = expiring_batches.count()
    if count == 0:
        speech = f"There are no medicine batches expiring within the next {days} days."
    else:
        sample = expiring_batches.first()
        speech = (
            f"There are {count} active medicine batches expiring in the next {days} days "
            f"({cutoff_date.strftime('%B %Y')}), including batch {sample.batch_number} of {sample.medicine.medicine_name}. "
            f"Opening Expiry Management workspace."
        )

    return {
        'success': True,
        'intent': 'CHECK_EXPIRY',
        'confidence': 0.95,
        'speech': speech,
        'count': count,
        'days': days,
        'action': 'REDIRECT',
        'action_url': '/inventory/expiry/'
    }


def handle_stock_movements(user, entities):
    """Summarizes today's inventory stock transactions."""
    today = timezone.localdate()
    txns = StockTransaction.objects.filter(created_at__date=today)
    total_txns = txns.count()

    receipts = txns.filter(
        transaction_type__in=[
            StockTransaction.TransactionType.PURCHASE,
            StockTransaction.TransactionType.ADJUSTMENT,
            StockTransaction.TransactionType.OPENING,
        ]
    ).aggregate(s=Sum('quantity'))['s'] or 0

    deductions = txns.filter(
        transaction_type__in=[
            StockTransaction.TransactionType.SALE,
            StockTransaction.TransactionType.DISPENSE,
            StockTransaction.TransactionType.DAMAGE,
            StockTransaction.TransactionType.EXPIRY,
        ]
    ).aggregate(s=Sum('quantity'))['s'] or 0

    speech = (
        f"Today's stock movements: {total_txns} total transactions recorded. "
        f"{receipts} units added to stock, and {deductions} units dispensed or sold. Opening stock history."
    )
    return {
        'success': True,
        'intent': 'STOCK_MOVEMENTS',
        'confidence': 0.95,
        'speech': speech,
        'total_transactions': total_txns,
        'receipts': receipts,
        'deductions': deductions,
        'action': 'REDIRECT',
        'action_url': '/inventory/history/'
    }


def handle_pending_purchase_requests(user, entities):
    """Lists pending purchase requests awaiting approval."""
    pending_prs = PurchaseRequest.objects.filter(status=PurchaseRequest.PRStatus.PENDING_APPROVAL)
    count = pending_prs.count()
    total_items = PurchaseRequestItem.objects.filter(purchase_request__in=pending_prs).count()

    if count == 0:
        speech = "There are currently no pending purchase requests awaiting approval. All procurement requests are up to date."
    else:
        sample = pending_prs.first()
        speech = (
            f"There are currently {count} pending purchase requests awaiting approval, "
            f"totaling {total_items} line items, including {sample.request_id}. Opening Purchase Requests."
        )

    return {
        'success': True,
        'intent': 'PENDING_PURCHASE_REQUESTS',
        'confidence': 0.95,
        'speech': speech,
        'pending_count': count,
        'action': 'REDIRECT',
        'action_url': '/purchases/requests/'
    }


def handle_reorder_recommendations(user, entities):
    """Reports medicines currently flagged for replenishment by AI and min-max rules."""
    from .auto_reorder_service import calculate_auto_reorder_details
    from .models import AIRecommendation

    # Check active AI reorder recommendations
    recs = AIRecommendation.objects.filter(
        recommendation_type=AIRecommendation.RecommendationType.REORDER,
        status=AIRecommendation.Status.PENDING
    ).select_related('medicine')

    count = recs.count()
    if count > 0:
        sample_names = ", ".join([r.medicine.medicine_name for r in recs[:3] if r.medicine])
        speech = (
            f"AI Intelligence has identified {count} medicines requiring reorder replenishment, "
            f"including {sample_names}. Opening AI Recommendation Center."
        )
    else:
        speech = "All medicines are currently within safe stock thresholds. No urgent reorder recommendations are pending."

    return {
        'success': True,
        'intent': 'REORDER_RECOMMENDATIONS',
        'confidence': 0.95,
        'speech': speech,
        'count': count,
        'action': 'REDIRECT',
        'action_url': '/ai-intelligence/recommendations/?type=REORDER'
    }


def handle_navigation(user, cleaned_text):
    """Maps natural spoken navigation targets to application routes."""
    text = cleaned_text
    is_admin = (getattr(user, 'role', '') == User.Role.ADMIN or user.is_superuser)

    routes = [
        (r'\b(?:expiry|expiring|near\s+expiry)\b', '/inventory/expiry/', "Opening Expiry Management workspace."),
        (r'\b(?:inventory|stock\s+overview)\b', '/inventory/', "Opening Inventory Dashboard."),
        (r'\b(?:medicine\s+master|medicines|catalogue)\b', '/medicines/', "Opening Medicine Master catalogue."),
        (r'\b(?:direct\s+dispensing|dispense|dispensing)\b', '/inventory/dispense/direct/', "Opening Direct Pharmacist Dispensing."),
        (r'\b(?:purchase\s+request|requests)\b', '/purchases/requests/', "Opening Purchase Requests."),
        (r'\b(?:purchase\s+orders?|procurement|purchases)\b', '/purchases/', "Opening Procurement and Purchase Orders."),
        (r'\b(?:ai\s+intelligence|intelligence\s+dashboard|forecast)\b', '/ai-intelligence/', "Opening Sanalogx AI Intelligence Dashboard."),
        (r'\b(?:reports?|invoices?)\b', '/reports/', "Opening Reports and Dispensing Invoices."),
        (r'\b(?:notifications?|alerts?)\b', '/notifications/', "Opening Alerts and Notifications."),
        (r'\b(?:price\s+comparison|suppliers?|vendor)\b', '/suppliers/price-comparison/', "Opening Supplier Price Intelligence."),
        (r'\b(?:dashboard|home)\b', '/admin-dashboard/' if is_admin else '/pharmacist-dashboard/', "Navigating to your dashboard."),
    ]

    for pattern, url, speech in routes:
        if re.search(pattern, text):
            return {
                'success': True,
                'intent': 'NAVIGATION',
                'confidence': 0.95,
                'speech': speech,
                'action': 'REDIRECT',
                'action_url': url
            }

    return {
        'success': True,
        'intent': 'NAVIGATION',
        'confidence': 0.85,
        'speech': "Navigating to Inventory Dashboard.",
        'action': 'REDIRECT',
        'action_url': '/inventory/'
    }


# ─────────────────────────────────────────────────────────────────────────────
# TWO-STEP CONFIRMATION WORKFLOW: "ADD 50 PARACETAMOL TABLETS"
# ─────────────────────────────────────────────────────────────────────────────

def handle_add_stock_request(user, entities):
    """
    Step 1 of Stock Addition:
    Strictly validates role, disambiguates medicine, ensures an active batch exists,
    and returns a CONFIRMATION_REQUIRED payload with secure token.
    NEVER mutates database stock on speech input alone.
    NEVER invents fictitious batch details.
    """
    # 1. Role Permission Gate (Pharmacist or Admin required)
    is_authorized = (
        user.is_authenticated and (
            getattr(user, 'role', '') in [User.Role.ADMIN, User.Role.PHARMACIST]
            or user.is_superuser
        )
    )
    if not is_authorized:
        return {
            'success': False,
            'intent': 'ADD_STOCK_REQUEST',
            'confidence': 1.0,
            'speech': "Access restricted. Stock updates require Pharmacist or Admin authorization.",
            'action': 'ACCESS_DENIED',
            'error': 'PERMISSION_DENIED'
        }

    # 2. Check Medicine Ambiguity
    if entities.get('is_ambiguous'):
        meds = entities.get('matching_medicines', [])
        options_str = ", ".join([m.medicine_name for m in meds])
        return {
            'success': True,
            'intent': 'ADD_STOCK_REQUEST',
            'confidence': 0.95,
            'is_ambiguous': True,
            'matching_medicines': [{'id': m.id, 'name': m.medicine_name} for m in meds],
            'speech': f"Multiple medicines match '{entities.get('extracted_name')}': {options_str}. Please specify which formulation you wish to update.",
            'action': 'CHOICE_REQUIRED',
            'action_url': '/medicines/'
        }

    med = entities.get('medicine')
    if not med:
        return {
            'success': False,
            'intent': 'ADD_STOCK_REQUEST',
            'confidence': 0.80,
            'speech': "Medicine not found in the catalogue. Please specify a valid medicine name.",
            'action': 'NONE',
            'error': 'MEDICINE_NOT_FOUND'
        }

    # 3. Check Quantity
    qty = entities.get('quantity')
    if not qty or qty <= 0:
        return {
            'success': False,
            'intent': 'ADD_STOCK_REQUEST',
            'confidence': 0.85,
            'speech': f"Please specify a valid positive quantity of {med.medicine_name} to add.",
            'action': 'NONE',
            'error': 'INVALID_QUANTITY'
        }

    # 4. Check Active Batches (Strict Clinical Rule: Never invent fictitious batches)
    today = timezone.localdate()
    valid_batches = med.batches.filter(
        status__in=[MedicineBatch.BatchStatus.ACTIVE, MedicineBatch.BatchStatus.NEAR_EXPIRY],
        expiry_date__gt=today
    ).exclude(
        status__in=[MedicineBatch.BatchStatus.EXPIRED, MedicineBatch.BatchStatus.QUARANTINED, MedicineBatch.BatchStatus.RETURNED]
    ).order_by('expiry_date')

    if not valid_batches.exists():
        return {
            'success': False,
            'intent': 'ADD_STOCK_REQUEST',
            'confidence': 1.0,
            'speech': (
                f"Cannot add stock without an active batch. {med.medicine_name} currently has no active, valid batch. "
                f"Standard operating procedure requires receiving stock through a Goods Receipt Note (GRN) to register "
                f"an official batch number, manufacturer, and expiry date."
            ),
            'action': 'REDIRECT',
            'action_url': '/purchases/grn/',
            'error': 'NO_ACTIVE_BATCH'
        }

    # Select batch: match specified batch if given, otherwise pick earliest expiring valid batch (FEFO)
    batch = None
    if entities.get('batch_number'):
        batch = valid_batches.filter(batch_number__iexact=entities['batch_number']).first()
    if not batch:
        batch = valid_batches.first()

    # 5. Calculate Current & Projected Stock
    from .auto_reorder_service import get_usable_stock
    current_stock = get_usable_stock(med)
    projected_stock = current_stock + qty
    unit = med.dosage_form or med.unit_of_measure or 'units'

    # 6. Generate Secure Confirmation Token (Cached for 5 minutes)
    token = uuid.uuid4().hex
    cache_key = f"voice_confirm_{token}"
    confirmation_payload = {
        'token': token,
        'action_type': 'ADD_STOCK',
        'user_id': user.id,
        'user_username': user.username,
        'medicine_id': med.id,
        'medicine_name': med.medicine_name,
        'batch_id': batch.id,
        'batch_number': batch.batch_number,
        'batch_expiry': batch.expiry_date.strftime('%Y-%m-%d'),
        'quantity': qty,
        'unit': unit,
        'current_stock': current_stock,
        'projected_stock': projected_stock,
        'created_at': timezone.now().isoformat()
    }
    cache.set(cache_key, confirmation_payload, timeout=300)

    speech = (
        f"Please confirm: Add {qty} {unit} of {med.medicine_name} to active batch {batch.batch_number}? "
        f"Current stock is {current_stock}, and new stock will be {projected_stock} {unit}."
    )

    return {
        'success': True,
        'intent': 'ADD_STOCK_REQUEST',
        'confidence': 0.98,
        'action': 'CONFIRMATION_REQUIRED',
        'confirmation_token': token,
        'speech': speech,
        'details': confirmation_payload
    }


def execute_voice_confirmation(user, token):
    """
    Step 2 of Stock Addition:
    Validates the confirmation token, checks permissions, executes atomic add_stock,
    creates StockTransaction and AuditLog, and invalidates token for idempotency.
    """
    cache_key = f"voice_confirm_{token}"
    data = cache.get(cache_key)

    if not data:
        return {
            'success': False,
            'speech': "This confirmation token has expired or has already been processed.",
            'error': 'EXPIRED_OR_INVALID_TOKEN',
            'action': 'NONE'
        }

    # Verify user authorization
    is_authorized = (
        user.is_authenticated and (
            getattr(user, 'role', '') in [User.Role.ADMIN, User.Role.PHARMACIST]
            or user.is_superuser
        )
    )
    if not is_authorized:
        return {
            'success': False,
            'speech': "Access restricted. Stock updates require Pharmacist or Admin authorization.",
            'error': 'PERMISSION_DENIED',
            'action': 'NONE'
        }

    if data.get('action_type') == 'ADD_STOCK':
        medicine_id = data['medicine_id']
        batch_id = data['batch_id']
        quantity = data['quantity']

        med = Medicine.objects.filter(pk=medicine_id).first()
        batch = MedicineBatch.objects.filter(pk=batch_id).first()

        if not med or not batch:
            cache.delete(cache_key)
            return {
                'success': False,
                'speech': "Medicine or batch record is no longer available in the system.",
                'error': 'RECORD_NOT_FOUND',
                'action': 'NONE'
            }

        from inventory.services import add_stock

        with transaction.atomic():
            txn = add_stock(
                medicine=med,
                batch=batch,
                quantity=quantity,
                transaction_type=StockTransaction.TransactionType.ADJUSTMENT,
                performed_by=user,
                reference_number=f"VOICE-{token[:8].upper()}",
                notes=f"Voice Assistant confirmed stock receipt: +{quantity} {data.get('unit')} by {user.username}"
            )

            inv = getattr(med, 'inventory', None)
            updated_stock = inv.current_stock if inv else med.current_stock

            AuditLog.log(
                action_type=AuditLog.ActionType.INVENTORY_ADJUSTED,
                performed_by=user,
                object_type='Medicine',
                object_id=med.id,
                object_repr=med.medicine_name,
                changes={'stock': [data['current_stock'], updated_stock]},
                description=f"Voice Assistant confirmed stock receipt: +{quantity} {data.get('unit')} added to batch {batch.batch_number}"
            )

            # Invalidate confirmation token immediately to ensure idempotency
            cache.delete(cache_key)

        speech = f"Stock successfully updated. {med.medicine_name} stock is now {updated_stock} {data.get('unit')}."
        return {
            'success': True,
            'speech': speech,
            'medicine_name': med.medicine_name,
            'updated_stock': updated_stock,
            'unit': data.get('unit'),
            'batch_number': batch.batch_number,
            'transaction_number': txn.transaction_number,
            'action': 'REDIRECT',
            'action_url': f"/inventory/medicine/{med.id}/"
        }

    return {
        'success': False,
        'speech': "Unknown confirmation action type.",
        'error': 'UNKNOWN_ACTION_TYPE',
        'action': 'NONE'
    }


def cancel_voice_confirmation(user, token):
    """
    Cancels a pending stock confirmation and discards the token.
    Leaves all database inventory untouched.
    """
    cache_key = f"voice_confirm_{token}"
    data = cache.get(cache_key)
    if data:
        cache.delete(cache_key)

    speech = "Stock addition cancelled. No changes were made to inventory."
    return {
        'success': True,
        'speech': speech,
        'action': 'CANCELLED'
    }


# ─────────────────────────────────────────────────────────────────────────────
# PURCHASE REQUEST CREATION VIA VOICE
# ─────────────────────────────────────────────────────────────────────────────

def handle_create_purchase_request(user, entities):
    """
    Creates an official PurchaseRequest via the existing auto-reorder workflow.
    Checks role permissions, deduplicates against active PRs and open POs,
    and returns a PurchaseRequest in PENDING_APPROVAL status.
    """
    is_authorized = (
        user.is_authenticated and (
            getattr(user, 'role', '') in [User.Role.ADMIN, User.Role.PHARMACIST]
            or user.is_superuser
        )
    )
    if not is_authorized:
        return {
            'success': False,
            'intent': 'CREATE_PURCHASE_REQUEST',
            'confidence': 1.0,
            'speech': "Access restricted. Creating purchase requests requires Pharmacist or Admin authorization.",
            'action': 'ACCESS_DENIED',
            'error': 'PERMISSION_DENIED'
        }

    if entities.get('is_ambiguous'):
        meds = entities.get('matching_medicines', [])
        options_str = ", ".join([m.medicine_name for m in meds])
        return {
            'success': True,
            'intent': 'CREATE_PURCHASE_REQUEST',
            'confidence': 0.95,
            'is_ambiguous': True,
            'matching_medicines': [{'id': m.id, 'name': m.medicine_name} for m in meds],
            'speech': f"Multiple medicines match '{entities.get('extracted_name')}': {options_str}. Please choose which medicine to order.",
            'action': 'CHOICE_REQUIRED',
            'action_url': '/medicines/'
        }

    med = entities.get('medicine')
    if not med:
        return {
            'success': False,
            'intent': 'CREATE_PURCHASE_REQUEST',
            'confidence': 0.80,
            'speech': "Medicine not found in catalogue. Please specify which medicine you would like to order.",
            'action': 'REDIRECT',
            'action_url': "/medicines/"
        }

    qty = entities.get('quantity')
    if not qty or qty <= 0:
        from .auto_reorder_service import calculate_auto_reorder_details
        details = calculate_auto_reorder_details(med)
        qty = details.get('calculated_qty') or med.reorder_level

    from .auto_reorder_service import create_auto_reorder_request
    res = create_auto_reorder_request(
        medicine=med,
        requested_qty=qty,
        reason=f"Voice Assistant procurement order requested by {user.username}",
        triggered_by_user=user,
    )

    pr = res.get('purchase_request')
    if pr:
        speech = (
            f"Purchase request {pr.request_id} created for {qty} units of {med.medicine_name}. "
            f"Current status is Pending Approval."
        )
        return {
            'success': True,
            'intent': 'CREATE_PURCHASE_REQUEST',
            'confidence': 0.98,
            'speech': speech,
            'purchase_request_id': pr.request_id,
            'action': 'REDIRECT',
            'action_url': '/purchases/requests/'
        }
    else:
        msg = res.get('message', f"Order request processed for {med.medicine_name}.")
        return {
            'success': True,
            'intent': 'CREATE_PURCHASE_REQUEST',
            'confidence': 0.90,
            'speech': msg,
            'action': 'REDIRECT',
            'action_url': '/purchases/requests/'
        }


def handle_help():
    speech = (
        "Sanalogx AI Voice Assistant is ready. You can say: "
        "'How many Paracetamol tablets are available?', "
        "'Show medicines with low stock', "
        "'Show medicines expiring next month', "
        "'Show today's stock movements', "
        "'Find medicines that are out of stock', "
        "'Show pending purchase requests', "
        "'Navigate to expiry management', "
        "'Create a purchase request for 100 units of Paracetamol', or "
        "'Add 50 Paracetamol tablets'."
    )
    return {
        'success': True,
        'intent': 'HELP',
        'confidence': 1.0,
        'speech': speech,
        'action': 'NONE'
    }


# ─────────────────────────────────────────────────────────────────────────────
# MAIN VOICE COMMAND DISPATCHER
# ─────────────────────────────────────────────────────────────────────────────

def process_voice_command(user, raw_text, session=None):
    """
    Main Entry Point for the Voice Assistant:
    1. Sanitizes spoken input.
    2. Extracts clinical entities (medicine, quantity, batch, horizon).
    3. Matches deterministic regex intent patterns.
    4. Enforces RBAC permissions.
    5. Dispatches to verified workflow services.
    6. Returns structured response with text, speech, and action URLs.
    """
    cleaned_text = clean_query_text(raw_text)
    if not cleaned_text:
        return {
            'success': False,
            'speech': "I didn't catch that. Please speak or type your command.",
            'intent': 'UNKNOWN',
            'confidence': 0.0,
            'action': 'NONE'
        }

    entities = extract_entities(cleaned_text)
    intent, confidence = detect_intent(cleaned_text, entities)

    # 1. Consequential Stock Addition
    if intent == 'ADD_STOCK_REQUEST':
        return handle_add_stock_request(user, entities)

    # 2. Purchase Request Creation
    elif intent == 'CREATE_PURCHASE_REQUEST':
        return handle_create_purchase_request(user, entities)

    # 3. Stock Lookups
    elif intent == 'CHECK_MEDICINE_STOCK':
        return handle_check_medicine_stock(user, entities, cleaned_text)

    # 4. Low Stock
    elif intent == 'CHECK_LOW_STOCK':
        return handle_check_low_stock(user, entities)

    # 5. Out of Stock
    elif intent == 'CHECK_OUT_OF_STOCK':
        return handle_check_out_of_stock(user, entities)

    # 6. Expiry
    elif intent == 'CHECK_EXPIRY':
        return handle_check_expiry(user, entities)

    # 7. Stock Movements
    elif intent == 'STOCK_MOVEMENTS':
        return handle_stock_movements(user, entities)

    # 8. Pending Purchase Requests
    elif intent == 'PENDING_PURCHASE_REQUESTS':
        return handle_pending_purchase_requests(user, entities)

    # 9. Reorder Recommendations
    elif intent == 'REORDER_RECOMMENDATIONS':
        return handle_reorder_recommendations(user, entities)

    # 10. Navigation
    elif intent == 'NAVIGATION':
        return handle_navigation(user, cleaned_text)

    # 11. Help
    elif intent == 'HELP':
        return handle_help()

    # 12. Fallback to AI Chat Engine for General Queries
    else:
        try:
            from . import chat_service
            chat_res = chat_service.process_ai_chat_query(raw_text, user, session=session)
            return {
                'success': True,
                'intent': chat_res.get('intent', 'CHAT_FALLBACK'),
                'confidence': 0.85,
                'speech': chat_res.get('answer', "I couldn't process that command right now."),
                'action': 'REDIRECT' if chat_res.get('action_url') else 'NONE',
                'action_url': chat_res.get('action_url'),
                'follow_up_suggestions': chat_res.get('follow_up_suggestions', [])
            }
        except Exception as exc:
            logger.warning(f"Chat fallback error: {exc}")
            return {
                'success': True,
                'intent': 'UNKNOWN',
                'confidence': 0.0,
                'speech': f"I couldn't understand that command. You can ask about medicine stock, low stock, expiry, or say 'Help'.",
                'action': 'NONE'
            }
