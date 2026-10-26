"""
ai_engine/chat_service.py
MediVault — Sanalogx AI Assistant Unified Chatbot & MediVoice Service Engine

Hybrid Architecture:
1. Normalization & Entity Extraction (Medicines, Batches, Dates, Metrics)
2. ML Intent Classification (Tfidf + Calibrated Logistic Regression)
3. Deterministic / Pattern Validation (100% precision for critical pharmacy queries)
4. Session-Based Conversation Context (Follow-up questions like "What about last month?", "Show its batches")
5. Authorized ORM Service Routing (Live MySQL database queries with RBAC)
6. Structured Response Formatting (Markdown tables, metric cards, action URLs, follow-up chips)
"""

import os
import re
import joblib
from decimal import Decimal
from datetime import timedelta, date, datetime

from django.db.models import Sum, Count, Q, Avg, F
from django.utils import timezone

from medicines.models import Medicine, MedicineBatch
from inventory.models import Inventory, StockTransaction, StockAlert
from sales.models import Sale, SaleItem
from dispensing.models import DispensingRecord, DispensingAllocation, ControlledMedicineRegister, StockDiscrepancy
from suppliers.models import Supplier, SupplierMedicine
from purchases.models import PurchaseOrder, PurchaseOrderItem
from . import intelligence_service
from .medicine_knowledge import get_medicine_knowledge, CLINICAL_KNOWLEDGE_BASE

# ─── ML Intent Model Singleton ───────────────────────────────────────────────
_CHAT_MODEL = None
_CHAT_VECTORIZER = None
_MODEL_LOAD_ATTEMPTED = False


def _get_chat_ml_model():
    """Singleton loader for chat intent classifier."""
    global _CHAT_MODEL, _CHAT_VECTORIZER, _MODEL_LOAD_ATTEMPTED
    if not _MODEL_LOAD_ATTEMPTED:
        _MODEL_LOAD_ATTEMPTED = True
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        model_path = os.path.join(base_dir, "ml", "models", "chat_intent_model.joblib")
        vec_path = os.path.join(base_dir, "ml", "models", "chat_vectorizer.joblib")
        if os.path.exists(model_path) and os.path.exists(vec_path):
            try:
                _CHAT_MODEL = joblib.load(model_path)
                _CHAT_VECTORIZER = joblib.load(vec_path)
            except Exception:
                _CHAT_MODEL = None
                _CHAT_VECTORIZER = None
    return _CHAT_MODEL, _CHAT_VECTORIZER


# ─── Brand & Alias Lookup Dictionary ─────────────────────────────────────────
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
    'morphine': 'Morphine Sulfate',
}


def extract_medicine_entity(user_text, context_medicine=None):
    """
    Extracts medicine model instance from query string with:
    1. Alias / Brand name matching (e.g. 'dolo 650' -> Paracetamol)
    2. Exact and substring match against active database medicines
    3. Pronoun / Context reference ("it", "this medicine", "that product", "its")
    """
    clean_text = user_text.lower().strip()

    # 1. Check brand aliases
    for alias, canonical in MEDICINE_ALIASES.items():
        if re.search(r'\b' + re.escape(alias) + r'\b', clean_text):
            med = Medicine.objects.filter(medicine_name__icontains=canonical, status='ACTIVE').first()
            if med:
                return med

    # 2. Match directly against database medicines (longer names first for specificity)
    active_meds = list(Medicine.objects.filter(status='ACTIVE').order_by('-id'))
    # Sort by descending length of medicine_name to match "Diclofenac Sodium" before "Diclofenac"
    active_meds.sort(key=lambda m: len(m.medicine_name), reverse=True)

    for med in active_meds:
        med_name = med.medicine_name.lower()
        gen_name = (med.generic_name or '').lower()

        # Word boundary or exact substring check
        if len(med_name) >= 3 and med_name in clean_text:
            return med
        if gen_name and len(gen_name) >= 3 and gen_name in clean_text:
            return med

        # Check primary token (e.g. "Paracetamol" in "Paracetamol 500mg")
        primary_token = med_name.split()[0]
        if len(primary_token) >= 4 and re.search(r'\b' + re.escape(primary_token) + r'\b', clean_text):
            return med

    # 3. Pronoun / context resolution ("it", "this medicine", "its", "that drug")
    pronoun_pattern = r'\b(it|its|this|that|this medicine|this drug|the medicine|the drug|same medicine)\b'
    if context_medicine and re.search(pronoun_pattern, clean_text):
        return context_medicine

    return None


def extract_batch_entity(user_text):
    """Extracts batch number string pattern from user text."""
    # Matches patterns like BATCH-001, PARA-2026-A, B-101, LOT-9982, etc.
    match = re.search(r'\b([A-Z0-9]{2,8}(?:-[A-Z0-9]{2,8})+)\b', user_text.upper())
    if match:
        return match.group(1)
    # Match "batch 001", "batch A12"
    match2 = re.search(r'\bbatch\s+([A-Za-z0-9\-]+)\b', user_text, re.IGNORECASE)
    if match2:
        return match2.group(1).upper()
    return None


def extract_date_range_entity(user_text, context_range=None):
    """
    Extracts standardized date range category from user text:
    'today', 'week', 'month', '7d', '30d', '90d', 'year'
    """
    t = user_text.lower()
    if any(k in t for k in ['today', 'aaj', 'current day']):
        return 'today'
    if any(k in t for k in ['this week', 'weekly', 'current week', 'is hafte', 'past week', 'last 7 days', '7 days', '7d']):
        return 'week'
    if any(k in t for k in ['this month', 'monthly', 'current month', 'is mahine', 'last 30 days', '30 days', '30d', 'last month']):
        return 'month'
    if any(k in t for k in ['90 days', '90d', 'quarter', 'last 3 months']):
        return '90d'
    if any(k in t for k in ['this year', 'yearly', 'annual']):
        return 'year'
    return context_range or 'today'


# ─── Intent Predictor (Hybrid ML + Deterministic) ─────────────────────────────

def predict_intent(user_text):
    """
    Determines intent using pattern matching + ML classification.
    Returns (intent_name, confidence_float).
    """
    text_lower = user_text.lower().strip()

    # 1. Greetings
    if re.match(r'^(hi|hello|hey|namaste|greetings|good morning|good afternoon|good evening|salaam)\b', text_lower) and len(text_lower.split()) <= 4:
        return 'GREETING', 1.0

    # 2. Help & Capabilities
    if any(k in text_lower for k in ['help', 'what can you do', 'supported questions', 'features', 'commands', 'kya kar sakte ho', 'options', 'menu']):
        if len(text_lower.split()) <= 6 or 'what can you do' in text_lower or 'help' in text_lower:
            return 'HELP', 1.0

    # 3. 14 Explicit Quick Action Keywords
    if text_lower in ['inventory overview', 'stock overview', 'total stock']:
        return 'INVENTORY_OVERVIEW', 1.0
    if text_lower in ['low stock medicines', 'low stock', 'low stock items', 'reorder level']:
        return 'CHECK_LOW_STOCK', 1.0
    if text_lower in ['out-of-stock medicines', 'out of stock medicines', 'out of stock', 'zero stock']:
        return 'CHECK_OUT_OF_STOCK', 1.0
    if text_lower in ['expiry and near-expiry batches', 'near expiry', 'expiry risk', 'expiring batches', 'expired batches']:
        return 'CHECK_EXPIRY_RISK', 1.0
    if text_lower in ['today\'s sales', 'today sales', 'todays sales']:
        return 'SALES_TODAY', 1.0
    if text_lower in ['weekly sales', 'sales this week', 'week sales']:
        return 'SALES_WEEK', 1.0
    if text_lower in ['monthly sales', 'sales this month', 'month sales']:
        return 'SALES_MONTH', 1.0
    if text_lower in ['dispensing history', 'recent dispensing', 'dispense history']:
        return 'DISPENSING_HISTORY', 1.0
    if text_lower in ['consumption analytics', 'usage analytics', 'consumption']:
        return 'CONSUMPTION_ANALYTICS', 1.0
    if text_lower in ['ai reorder recommendations', 'ai reorder', 'reorder recommendations', 'what should i reorder']:
        return 'RECOMMEND_REORDER', 1.0
    if text_lower in ['medicine information', 'medicine info', 'drug info']:
        return 'MEDICINE_INFO', 1.0
    if text_lower in ['stock discrepancies', 'discrepancies', 'stock discrepancy']:
        return 'STOCK_DISCREPANCY', 1.0
    if text_lower in ['purchase orders', 'po status', 'procurement orders']:
        return 'PURCHASE_ORDERS', 1.0
    if text_lower in ['help and supported questions', 'help menu']:
        return 'HELP', 1.0

    # 4. Medicine Knowledge & Monograph Queries ("What is Paracetamol?", "Tell me about Amoxicillin")
    #    Require a medicine-related word so generic queries like "What is the weather?" fall through to UNKNOWN.
    if re.search(r'\b(what is|what are|info on|information about|tell me about|details of|uses of|side effects of|precautions for|storage of|dosage of|dose of)\b', text_lower):
        _is_system_feature = any(term in text_lower for term in ['controlled', 'schedule h1', 'schedule x', 'narcotic', 'discrepanc', 'purchase order', 'dispens', 'register'])
        _med_context = ['tablet', 'capsule', 'injection', 'syrup', 'drug', 'medicine', 'medication',
                        'antibiotic', 'analgesic', 'antifungal', 'antiviral', 'dosage', 'mg', 'ml',
                        'paracetamol', 'amoxicillin', 'metformin', 'ibuprofen', 'ciprofloxacin',
                        'tramadol', 'morphine', 'insulin', 'aspirin', 'amlodipine', 'omeprazole',
                        'metronidazole', 'azithromycin', 'diclofenac', 'cetirizine', 'salbutamol',
                        'atorvastatin', 'glibenclamide', 'pantoprazole', 'dolo', 'crocin', 'augmentin']
        if not _is_system_feature and (any(w in text_lower for w in _med_context) or any(k in text_lower for k in MEDICINE_ALIASES)):
            return 'MEDICINE_INFO', 0.95

    # 5. Batches for a medicine ("Which batches are available?", "Show batches for it")
    if any(k in text_lower for k in ['batch', 'batches', 'lot number', 'expiry date for this', 'nearest expiry']):
        return 'CHECK_BATCHES', 0.95

    # 6. Specific Stock Query
    if any(k in text_lower for k in ['how much', 'stock level', 'available units', 'current stock of', 'quantity of', 'stock of']):
        return 'CHECK_MEDICINE_STOCK', 0.95

    # 7. Low / Out of Stock
    if any(k in text_lower for k in ['low stock', 'running low', 'kam stock', 'threshold', 'below minimum']):
        return 'CHECK_LOW_STOCK', 0.95
    if any(k in text_lower for k in ['out of stock', 'depleted', 'zero stock', 'khatam', 'unavailable']):
        return 'CHECK_OUT_OF_STOCK', 0.95

    # 8. Expiry queries
    if any(k in text_lower for k in ['expired', 'expiring', 'near expiry', 'expiry date', 'expire hone wali']):
        return 'CHECK_EXPIRY_RISK', 0.95

    # 9. Sales & Revenue
    if any(k in text_lower for k in ['profit', 'gross profit', 'margin', 'financial margin']):
        return 'PROFIT_SUMMARY', 0.95
    if any(k in text_lower for k in ['top selling', 'most sold', 'best selling', 'popular medicine']):
        return 'TOP_SELLING_MEDICINES', 0.95
    if any(k in text_lower for k in ['payment method', 'upi sales', 'cash sales', 'card sales']):
        return 'SALES_PAYMENT_METHOD', 0.95
    if any(k in text_lower for k in ['weekly sales', 'this week', 'past 7 days', 'week revenue']):
        return 'SALES_WEEK', 0.95
    if any(k in text_lower for k in ['monthly sales', 'this month', 'month revenue', 'last 30 days']):
        return 'SALES_MONTH', 0.95
    if any(k in text_lower for k in ['today\'s sales', 'sales today', 'revenue today', 'how much did we sell', 'todays sales']):
        return 'SALES_TODAY', 0.95
    if any(k in text_lower for k in ['sales', 'revenue', 'invoices', 'billing']):
        return 'SALES_TODAY', 0.85

    # 10. Dispensing
    if any(k in text_lower for k in ['dispensed', 'dispensing', 'prescription dispensed', 'dispense count']):
        return 'DISPENSING_HISTORY', 0.95

    # 11. Consumption
    if any(k in text_lower for k in ['consumption', 'consumed', 'daily velocity', 'burn rate', 'usage rate']):
        return 'CONSUMPTION_ANALYTICS', 0.95

    # 12. Reorders & Recommendations
    if any(k in text_lower for k in ['reorder', 'what should i order', 'restock', 'procurement suggestion', 'replenishment']):
        return 'RECOMMEND_REORDER', 0.95

    # 13. Discrepancies
    if any(k in text_lower for k in ['discrepancy', 'discrepancies', 'mismatch', 'variance', 'reconciliation']):
        return 'STOCK_DISCREPANCY', 0.95

    # 14. Purchase Orders
    if any(k in text_lower for k in ['purchase order', 'purchase orders', 'po list', 'pending po', 'supplier order', 'incoming stock']):
        return 'PURCHASE_ORDERS', 0.95

    # 15. Controlled drug register
    if any(k in text_lower for k in ['controlled', 'schedule h1', 'schedule x', 'narcotic']):
        return 'CONTROLLED_REGISTER', 0.95

    # 16. Supplier comparison
    if any(k in text_lower for k in ['supplier', 'vendor', 'cheapest price', 'who supplies']):
        return 'SUPPLIER_INTELLIGENCE', 0.90

    # 17. Forecast
    if any(k in text_lower for k in ['forecast', 'prediction', 'future demand']):
        return 'DEMAND_FORECAST', 0.90

    # 18. Dead stock / Overstock
    if any(k in text_lower for k in ['dead stock', 'inactive medicine', 'no movement']):
        return 'DEAD_STOCK', 0.90
    if any(k in text_lower for k in ['overstock', 'surplus', 'excess stock']):
        return 'OVERSTOCK', 0.90

    # 19. Try ML Intent Model if available
    model, vectorizer = _get_chat_ml_model()
    if model is not None and vectorizer is not None:
        try:
            X = vectorizer.transform([text_lower])
            pred_intent = model.predict(X)[0]
            if hasattr(model, 'predict_proba'):
                proba = float(max(model.predict_proba(X)[0]))
            else:
                proba = 0.85
            if proba >= 0.45:
                return pred_intent, round(proba, 2)
        except Exception:
            pass

    return 'UNKNOWN_FALLBACK', 0.30


# ─── Main Dispatcher & Query Processor ───────────────────────────────────────

def process_ai_chat_query(user_text, user, session=None):
    """
    Main Chatbot Execution Engine.
    Enforces role-based permissions, queries MySQL ORM, resolves session context,
    and returns a structured JSON payload.
    """
    if not user_text or not user_text.strip():
        return {
            'success': False,
            'intent': 'EMPTY',
            'answer': "Please ask a question about pharmacy stock, sales, dispensing, expiry, reorders, or medicine information.",
            'action_url': None,
            'data': {}
        }

    raw_text = user_text.strip()
    text_lower = raw_text.lower()
    user_role = getattr(user, 'role', 'PHARMACIST')
    today = timezone.localdate()

    # Load session context if available
    context = {}
    context_med = None
    if session is not None and isinstance(session, dict) or hasattr(session, 'get'):
        context = session.get('sanalogx_chat_context', {})
        if context.get('last_medicine_id'):
            context_med = Medicine.objects.filter(id=context['last_medicine_id'], status='ACTIVE').first()

    # 1. Intent Detection
    intent, confidence = predict_intent(raw_text)

    # 2. Entity Extraction
    med = extract_medicine_entity(raw_text, context_medicine=context_med)
    batch_num = extract_batch_entity(raw_text)
    date_range = extract_date_range_entity(raw_text, context.get('last_date_range'))

    # If user explicitly asks "Stock of Paracetamol" or "Paracetamol batches", refine intent
    if med and intent in ['UNKNOWN_FALLBACK', 'INVENTORY_OVERVIEW']:
        if any(k in text_lower for k in ['batch', 'batches', 'expiry']):
            intent = 'CHECK_BATCHES'
        elif any(k in text_lower for k in ['what is', 'tell me about', 'info', 'uses', 'side effect']):
            intent = 'MEDICINE_INFO'
        else:
            intent = 'CHECK_MEDICINE_STOCK'

    # Save medicine to session context for subsequent follow-ups
    if med and session is not None and hasattr(session, '__setitem__'):
        context['last_medicine_id'] = med.id
        context['last_medicine_name'] = med.medicine_name
        context['last_intent'] = intent
        context['last_date_range'] = date_range
        session['sanalogx_chat_context'] = context

    # ─────────────────────────────────────────────────────────────────────────
    # A. GREETING & HELP
    # ─────────────────────────────────────────────────────────────────────────

    if intent == 'GREETING':
        username = getattr(user, 'username', 'Pharmacist')
        greeting_text = (
            f"Hello **{username}**! Welcome to **Sanalogx AI Assistant**.\n\n"
            f"I analyze live database records to help you monitor inventory, track sales, evaluate expiry risks, "
            f"review clinical medicine monographs, and inspect AI reorder recommendations.\n\n"
            f"**What would you like to check?** You can select a quick action below or type any question."
        )
        return {
            'success': True,
            'intent': 'GREETING',
            'confidence': confidence,
            'answer': greeting_text,
            'action_url': '/inventory/',
            'action_label': 'Open Inventory Dashboard',
            'follow_up_suggestions': [
                'Inventory Overview',
                'Low Stock Medicines',
                "Today's Sales",
                'AI Reorder Recommendations',
                'What is Paracetamol?'
            ],
            'data': {}
        }

    if intent == 'HELP':
        help_text = (
            f"### Sanalogx AI Assistant — Capabilities & Commands\n\n"
            f"I am connected to live MediVault database records. Here are common questions you can ask:\n\n"
            f"1. **Inventory:** *\"Show total stock units\"*, *\"Which medicines have low stock?\"*, *\"Show stock for Paracetamol\"*\n"
            f"2. **Medicine Information:** *\"What is Paracetamol?\"*, *\"Uses and side effects of Amoxicillin\"*, *\"Storage for Insulin\"*\n"
            f"3. **Sales & Billing:** *\"Show today's sales\"*, *\"Weekly sales summary\"*, *\"Monthly sales\"*, *\"Top selling medicines\"*\n"
            f"4. **Expiry Management:** *\"Which batches expire in 30 days?\"*, *\"Show expired stock\"*\n"
            f"5. **Consumption Analytics:** *\"How much Paracetamol was consumed in 30 days?\"*, *\"Daily velocity\"*\n"
            f"6. **AI Reorder:** *\"What should I reorder?\"*, *\"AI replenishment recommendations\"*\n"
            f"7. **Clinical Dispensing:** *\"Dispensing history today\"*, *\"Check controlled drugs\"*\n"
            f"8. **Discrepancies & POs:** *\"Open stock discrepancies\"*, *\"Status of purchase orders\"*\n\n"
            f"*Tip: You can also use the 14 interactive Quick Action buttons at any time.*"
        )
        return {
            'success': True,
            'intent': 'HELP',
            'confidence': confidence,
            'answer': help_text,
            'action_url': '/inventory/',
            'action_label': 'Explore Workspace',
            'follow_up_suggestions': [
                'Inventory Overview',
                'Low Stock Medicines',
                'Expiry and Near-Expiry Batches',
                "Today's Sales",
                'Dispensing History'
            ],
            'data': {}
        }

    # ─────────────────────────────────────────────────────────────────────────
    # B. INVENTORY OPERATIONS (Quick Actions 1, 2, 3)
    # ─────────────────────────────────────────────────────────────────────────

    if intent == 'INVENTORY_OVERVIEW':
        total_meds = Medicine.objects.filter(status='ACTIVE').count()
        inv_agg = Inventory.objects.aggregate(
            curr=Sum('current_stock'),
            avail=Sum('available_stock')
        )
        total_curr = inv_agg['curr'] or 0
        total_avail = inv_agg['avail'] or 0
        low_stk = Inventory.objects.filter(stock_status='LOW_STOCK').count()
        out_stk = Inventory.objects.filter(current_stock=0).count()
        near_exp = MedicineBatch.objects.filter(status=MedicineBatch.BatchStatus.NEAR_EXPIRY).count()
        expired_batches = MedicineBatch.objects.filter(status=MedicineBatch.BatchStatus.EXPIRED).count()

        answer = (
            f"### Live Inventory Overview\n\n"
            f"- **Active Medicines Catalogue:** **{total_meds} medicines**\n"
            f"- **Total Physical Stock:** **{total_curr:,} units**\n"
            f"- **Sellable / Dispensing Available Stock:** **{total_avail:,} units** *(excludes expired/quarantined)*\n"
            f"- **Low Stock Alert:** **{low_stk} medicines** at or below reorder threshold\n"
            f"- **Out of Stock:** **{out_stk} medicines** completely depleted\n"
            f"- **Batch Expiry Watch:** **{near_exp} batches** near expiry (90d), **{expired_batches} expired batches**"
        )
        return {
            'success': True,
            'intent': 'INVENTORY_OVERVIEW',
            'confidence': confidence,
            'answer': answer,
            'action_url': '/inventory/',
            'action_label': 'View Full Inventory',
            'follow_up_suggestions': ['Low Stock Medicines', 'Out-of-Stock Medicines', 'Expiry and Near-Expiry Batches'],
            'data': {
                'total_medicines': total_meds,
                'total_physical_stock': total_curr,
                'available_stock': total_avail,
                'low_stock_count': low_stk,
                'out_of_stock_count': out_stk,
                'near_expiry_count': near_exp
            }
        }

    if intent == 'CHECK_LOW_STOCK':
        low_items = Inventory.objects.filter(
            Q(stock_status='LOW_STOCK') | Q(current_stock__lte=F('reorder_level'), current_stock__gt=0)
        ).select_related('medicine').order_by('current_stock')
        count = low_items.count()

        if count == 0:
            answer = "All active medicines currently have healthy stock levels above their reorder thresholds."
        else:
            top_items = low_items[:8]
            item_lines = [
                f"- **{inv.medicine.medicine_name}**: Available **{inv.available_stock}** / Physical **{inv.current_stock} units** (Reorder level: {inv.reorder_level})"
                for inv in top_items
            ]
            answer = (
                f"### Low Stock Medicines Queue ({count} requiring replenishment)\n\n"
                + "\n".join(item_lines) +
                (f"\n\n*...plus {count - 8} more medicines.*" if count > 8 else "")
            )

        return {
            'success': True,
            'intent': 'CHECK_LOW_STOCK',
            'confidence': confidence,
            'answer': answer,
            'action_url': '/inventory/?status=LOW_STOCK',
            'action_label': 'Open Low Stock Management',
            'follow_up_suggestions': ['AI Reorder Recommendations', 'Out-of-Stock Medicines', 'Purchase Orders'],
            'data': {'low_stock_count': count}
        }

    if intent == 'CHECK_OUT_OF_STOCK':
        out_items = Inventory.objects.filter(current_stock=0).select_related('medicine').order_by('medicine__medicine_name')
        count = out_items.count()

        if count == 0:
            answer = "No medicines are currently out of stock. All active catalogue items have physical inventory on hand."
        else:
            names = [f"- **{inv.medicine.medicine_name}** ({inv.medicine.dosage_form} {inv.medicine.strength})" for inv in out_items[:8]]
            answer = (
                f"### Out of Stock Alert ({count} depleted medicines)\n\n"
                f"The following medicines have **0 physical stock** and cannot be dispensed:\n\n"
                + "\n".join(names) +
                (f"\n\n*...plus {count - 8} additional items.*" if count > 8 else "")
            )

        return {
            'success': True,
            'intent': 'CHECK_OUT_OF_STOCK',
            'confidence': confidence,
            'answer': answer,
            'action_url': '/inventory/?status=OUT_OF_STOCK',
            'action_label': 'View Out of Stock Queue',
            'follow_up_suggestions': ['AI Reorder Recommendations', 'Purchase Orders', 'Inventory Overview'],
            'data': {'out_of_stock_count': count}
        }

    # ─────────────────────────────────────────────────────────────────────────
    # C. SPECIFIC MEDICINE STOCK & BATCHES
    # ─────────────────────────────────────────────────────────────────────────

    if intent == 'CHECK_MEDICINE_STOCK' and med:
        inv = getattr(med, 'inventory', None)
        avail = inv.available_stock if inv else 0
        total = inv.current_stock if inv else 0
        reorder_lvl = inv.reorder_level if inv else med.reorder_level
        safety = inv.safety_stock if inv else med.safety_stock

        batches = med.batches.filter(quantity_available__gt=0).order_by('expiry_date')
        batch_count = batches.count()
        nearest = batches.first()
        nearest_str = f"Batch **{nearest.batch_number}** expires on **{nearest.expiry_date}** ({(nearest.expiry_date - today).days} days)" if nearest else "No active batches"

        answer = (
            f"### Stock Status: **{med.medicine_name}** ({med.dosage_form} {med.strength})\n\n"
            f"- **Available for Dispensing:** **{avail} units**\n"
            f"- **Total Physical Stock:** **{total} units**\n"
            f"- **Reorder Threshold:** {reorder_lvl} units | **Safety Stock:** {safety} units\n"
            f"- **Active Batches:** {batch_count} in inventory\n"
            f"- **Nearest Expiry:** {nearest_str}\n"
            f"- **Regulatory Classification:** `{med.get_regulatory_category_display()}`"
        )
        return {
            'success': True,
            'intent': 'CHECK_MEDICINE_STOCK',
            'confidence': confidence,
            'answer': answer,
            'action_url': f"/inventory/medicine/{med.id}/",
            'action_label': f"View {med.medicine_name} Inventory",
            'follow_up_suggestions': [
                f"Which batches are available for {med.medicine_name}?",
                f"What is {med.medicine_name}?",
                f"Consumption of {med.medicine_name}"
            ],
            'data': {'medicine': str(med), 'available': avail, 'total': total, 'batch_count': batch_count}
        }

    if intent == 'CHECK_BATCHES' and med:
        batches = med.batches.all().order_by('expiry_date')
        count = batches.count()

        if count == 0:
            answer = f"No batch records found in the database for **{med.medicine_name}**."
        else:
            rows = []
            for b in batches[:8]:
                days = (b.expiry_date - today).days
                status_tag = f"⚠️ Expired ({abs(days)}d ago)" if days <= 0 else (f"⚡ Near Expiry ({days}d)" if days <= 90 else f"✅ Active ({days}d)")
                rows.append(f"- Batch **{b.batch_number}**: **{b.quantity_available} units** available (Exp: {b.expiry_date} · {status_tag})")

            answer = (
                f"### Batches for **{med.medicine_name}** ({count} registered)\n\n"
                + "\n".join(rows) +
                "\n\n*FEFO Rule applied: Batches expiring earliest are prioritized automatically during dispensing.*"
            )

        return {
            'success': True,
            'intent': 'CHECK_BATCHES',
            'confidence': confidence,
            'answer': answer,
            'action_url': f"/inventory/medicine/{med.id}/",
            'action_label': 'View Batch Details',
            'follow_up_suggestions': [f"Show stock for {med.medicine_name}", f"What is {med.medicine_name}?"],
            'data': {'medicine': str(med), 'batch_count': count}
        }

    # ─────────────────────────────────────────────────────────────────────────
    # D. GENUINE MEDICINE KNOWLEDGE ("What is Paracetamol?")
    # ─────────────────────────────────────────────────────────────────────────

    if intent == 'MEDICINE_INFO':
        if not med:
            return {
                'success': True,
                'intent': 'MEDICINE_INFO_PROMPT',
                'confidence': confidence,
                'answer': (
                    "Which medicine would you like clinical and inventory details for?\n\n"
                    "For example, ask: *\"What is Paracetamol?\"*, *\"Tell me about Amoxicillin\"*, "
                    "or *\"Information on Tramadol\"*."
                ),
                'action_url': '/medicines/',
                'action_label': 'Browse Medicines Catalogue',
                'follow_up_suggestions': [
                    'What is Paracetamol?',
                    'What is Amoxicillin?',
                    'What is Tramadol?',
                    'What is Metformin?'
                ],
                'data': {}
            }

        # Lookup verified pharmacology knowledge
        search_key = med.medicine_name.lower().split()[0]
        knowledge = get_medicine_knowledge(search_key) or get_medicine_knowledge(med.generic_name.lower())

        # Live database inventory details
        inv = getattr(med, 'inventory', None)
        avail = inv.available_stock if inv else 0
        total = inv.current_stock if inv else 0
        nearest_b = med.batches.filter(quantity_available__gt=0).order_by('expiry_date').first()
        exp_info = f"Batch **{nearest_b.batch_number}** (Exp: {nearest_b.expiry_date})" if nearest_b else "No active batches"

        if knowledge:
            brands = ", ".join(knowledge.get('brand_names', []))
            answer = (
                f"### Clinical Monograph: **{knowledge.get('display_name', med.medicine_name)}**\n\n"
                f"- **Generic Name:** {knowledge.get('generic_name', med.generic_name)}\n"
                f"- **Common Brand Names:** {brands or 'Standard Formulations'}\n"
                f"- **Therapeutic Category:** {knowledge.get('category', med.category.name)}\n"
                f"- **Catalogue Specification:** {med.dosage_form} {med.strength} ({med.unit_of_measure})\n"
                f"- **Regulatory Classification:** `{med.get_regulatory_category_display()}`\n\n"
                f"**Clinical Uses & Indications:**\n{knowledge.get('common_uses')}\n\n"
                f"**Important Precautions & Warnings:**\n{knowledge.get('precautions')}\n\n"
                f"**Contraindications:**\n{knowledge.get('contraindications')}\n\n"
                f"**Common Adverse Effects:**\n{knowledge.get('side_effects')}\n\n"
                f"**Recommended Storage:**\n{knowledge.get('storage_requirements')}\n\n"
                f"---\n"
                f"**Live MediVault Inventory Status:**\n"
                f"Available: **{avail} units** (Total physical: {total} units) | Nearest Expiry: {exp_info}\n\n"
                f"> **Clinical Advisory:** *This reference information is for pharmacist and clinical staff guidance only. "
                f"Not intended for direct patient self-medication, individual dosing, or diagnosis. Escalate clinical inquiries to a licensed physician or clinical pharmacist.*"
            )
        else:
            answer = (
                f"### Medicine Catalogue Profile: **{med.medicine_name}**\n\n"
                f"- **Generic Name:** {med.generic_name}\n"
                f"- **Therapeutic Category:** {med.category.name}\n"
                f"- **Dosage Form & Strength:** {med.dosage_form} {med.strength}\n"
                f"- **Manufacturer:** {med.manufacturer or 'Registered Supplier'}\n"
                f"- **Regulatory Schedule:** `{med.get_regulatory_category_display()}`\n"
                f"- **Storage Guideline:** {med.storage_condition or 'Store in cool, dry place below 25°C'}\n"
                f"- **Live Available Stock:** **{avail} units** across active batches\n\n"
                f"*Note: Detailed extended pharmacology monograph is being verified in catalogue review notes.*"
            )

        return {
            'success': True,
            'intent': 'MEDICINE_INFO',
            'confidence': confidence,
            'answer': answer,
            'action_url': f"/inventory/medicine/{med.id}/",
            'action_label': f"View {med.medicine_name} in Catalogue",
            'follow_up_suggestions': [
                f"Show stock for {med.medicine_name}",
                f"Which batches are available for {med.medicine_name}?",
                f"Consumption of {med.medicine_name}"
            ],
            'data': {'medicine_id': med.id, 'medicine_name': med.medicine_name, 'available': avail}
        }

    # ─────────────────────────────────────────────────────────────────────────
    # E. EXPIRY & NEAR-EXPIRY BATCHES (Quick Action 4)
    # ─────────────────────────────────────────────────────────────────────────

    if intent == 'CHECK_EXPIRY_RISK':
        date_30d = today + timedelta(days=30)
        date_60d = today + timedelta(days=60)
        date_90d = today + timedelta(days=90)

        expired_qs = MedicineBatch.objects.filter(expiry_date__lte=today, quantity_available__gt=0).select_related('medicine')
        exp_30_qs = MedicineBatch.objects.filter(expiry_date__gt=today, expiry_date__lte=date_30d, quantity_available__gt=0).select_related('medicine')
        exp_60_qs = MedicineBatch.objects.filter(expiry_date__gt=date_30d, expiry_date__lte=date_60d, quantity_available__gt=0).select_related('medicine')
        exp_90_qs = MedicineBatch.objects.filter(expiry_date__gt=date_60d, expiry_date__lte=date_90d, quantity_available__gt=0).select_related('medicine')

        n_exp = expired_qs.count()
        n_30 = exp_30_qs.count()
        n_60 = exp_60_qs.count()
        n_90 = exp_90_qs.count()
        total_risk = n_exp + n_30 + n_60 + n_90

        if total_risk == 0:
            answer = "Excellent! There are **0 batches** expired or nearing expiry within the next 90 days."
        else:
            critical_samples = []
            for b in list(expired_qs[:3]) + list(exp_30_qs[:3]):
                days = (b.expiry_date - today).days
                lbl = f"EXPIRED ({abs(days)}d ago)" if days <= 0 else f"{days} days remaining"
                critical_samples.append(f"- **{b.medicine.medicine_name}** (Batch {b.batch_number}): **{b.quantity_available} units** · Exp: {b.expiry_date} ({lbl})")

            answer = (
                f"### Expiry & Near-Expiry Batch Watch\n\n"
                f"- **Already Expired Batches:** **{n_exp} batches** *(must be quarantined immediately)*\n"
                f"- **Expiring within 30 Days:** **{n_30} batches** *(high risk)*\n"
                f"- **Expiring within 31–60 Days:** **{n_60} batches**\n"
                f"- **Expiring within 61–90 Days:** **{n_90} batches**\n\n"
                f"**Critical Attention Batches:**\n" + "\n".join(critical_samples or ['None in 30d window'])
            )

        return {
            'success': True,
            'intent': 'CHECK_EXPIRY_RISK',
            'confidence': confidence,
            'answer': answer,
            'action_url': '/inventory/expiry/',
            'action_label': 'Open Expiry Management',
            'follow_up_suggestions': ['Low Stock Medicines', 'AI Reorder Recommendations', 'Inventory Overview'],
            'data': {'expired_count': n_exp, 'exp_30d': n_30, 'exp_60d': n_60, 'exp_90d': n_90}
        }

    # ─────────────────────────────────────────────────────────────────────────
    # F. SALES & REVENUE (Quick Actions 5, 6, 7)
    # ─────────────────────────────────────────────────────────────────────────

    if intent == 'SALES_TODAY':
        today_sales = Sale.objects.filter(sale_date__date=today, status=Sale.SaleStatus.COMPLETED)
        count = today_sales.count()
        rev_agg = today_sales.aggregate(s=Sum('total_amount'), p=Sum('amount_paid'), o=Sum('outstanding_amount'))
        rev = rev_agg['s'] or Decimal('0.00')
        paid = rev_agg['p'] or Decimal('0.00')
        unpaid = rev_agg['o'] or Decimal('0.00')

        # Payment methods breakdown
        pm_breakdown = (
            today_sales.values('payment_method')
            .annotate(cnt=Count('id'), amt=Sum('total_amount'))
            .order_by('-amt')
        )
        pm_lines = [f"- {pm['payment_method']}: **₹{pm['amt']:.2f}** ({pm['cnt']} sales)" for pm in pm_breakdown]

        answer = (
            f"### Today's Sales Summary ({today.strftime('%d %b %Y')})\n\n"
            f"- **Completed Transactions:** **{count} sales**\n"
            f"- **Total Invoiced Revenue:** **₹{rev:,.2f}**\n"
            f"- **Amount Collected:** **₹{paid:,.2f}**\n"
            f"- **Outstanding / Balance:** **₹{unpaid:,.2f}**\n\n"
            f"**Payment Breakdown:**\n" + ("\n".join(pm_lines) if pm_lines else "- No completed sales today yet.")
        )
        return {
            'success': True,
            'intent': 'SALES_TODAY',
            'confidence': confidence,
            'answer': answer,
            'action_url': '/sales/history/?date_range=today',
            'action_label': 'View Today Sales Invoices',
            'follow_up_suggestions': ['Weekly Sales', 'Monthly Sales', 'Top Selling Medicines'],
            'data': {'count': count, 'revenue': float(rev), 'amount_paid': float(paid)}
        }

    if intent == 'SALES_WEEK':
        # Calendar week: Monday to Today
        week_start = today - timedelta(days=today.weekday())
        week_sales = Sale.objects.filter(
            sale_date__date__gte=week_start,
            sale_date__date__lte=today,
            status=Sale.SaleStatus.COMPLETED
        )
        count = week_sales.count()
        rev = week_sales.aggregate(s=Sum('total_amount'))['s'] or Decimal('0.00')
        days_in_week = (today - week_start).days + 1
        daily_avg = rev / Decimal(days_in_week) if days_in_week > 0 else Decimal('0.00')

        # Top medicine this week
        top_item = (
            SaleItem.objects.filter(sale__in=week_sales)
            .values('medicine__medicine_name')
            .annotate(qty=Sum('quantity'), amt=Sum('total_price'))
            .order_by('-qty')
            .first()
        )
        top_str = f"**{top_item['medicine__medicine_name']}** ({top_item['qty']} units, ₹{top_item['amt']:.2f})" if top_item else "No items sold yet"

        answer = (
            f"### Weekly Sales Summary (Calendar Week: {week_start.strftime('%d %b')} – {today.strftime('%d %b %Y')})\n\n"
            f"- **Completed Transactions:** **{count} sales**\n"
            f"- **Weekly Revenue:** **₹{rev:,.2f}**\n"
            f"- **Daily Average:** **₹{daily_avg:,.2f}/day** across {days_in_week} days\n"
            f"- **Leading Medicine:** {top_str}"
        )
        return {
            'success': True,
            'intent': 'SALES_WEEK',
            'confidence': confidence,
            'answer': answer,
            'action_url': '/sales/history/?date_range=week',
            'action_label': 'View Weekly Sales',
            'follow_up_suggestions': ["Today's Sales", 'Monthly Sales', 'Top Selling Medicines'],
            'data': {'count': count, 'revenue': float(rev), 'week_start': str(week_start)}
        }

    if intent == 'SALES_MONTH':
        # Calendar month: 1st of month to Today
        month_start = today.replace(day=1)
        month_sales = Sale.objects.filter(
            sale_date__date__gte=month_start,
            sale_date__date__lte=today,
            status=Sale.SaleStatus.COMPLETED
        )
        count = month_sales.count()
        rev = month_sales.aggregate(s=Sum('total_amount'))['s'] or Decimal('0.00')
        units_sold = SaleItem.objects.filter(sale__in=month_sales).aggregate(q=Sum('quantity'))['q'] or 0

        answer = (
            f"### Monthly Sales Summary (Current Month: {month_start.strftime('%d %b')} – {today.strftime('%d %b %Y')})\n\n"
            f"- **Completed Transactions:** **{count} sales**\n"
            f"- **Month-to-Date Revenue:** **₹{rev:,.2f}**\n"
            f"- **Total Medicine Units Sold:** **{units_sold:,} units**"
        )
        return {
            'success': True,
            'intent': 'SALES_MONTH',
            'confidence': confidence,
            'answer': answer,
            'action_url': '/sales/history/?date_range=month',
            'action_label': 'View Monthly Invoices',
            'follow_up_suggestions': ["Today's Sales", 'Weekly Sales', 'AI Reorder Recommendations'],
            'data': {'count': count, 'revenue': float(rev), 'units_sold': units_sold}
        }

    if intent == 'TOP_SELLING_MEDICINES':
        top_meds = (
            SaleItem.objects.filter(sale__status=Sale.SaleStatus.COMPLETED)
            .values('medicine__medicine_name')
            .annotate(total_qty=Sum('quantity'), total_rev=Sum('total_price'))
            .order_by('-total_qty')[:5]
        )
        if not top_meds:
            answer = "No completed sales records recorded yet in MediVault."
        else:
            lines = [f"{idx+1}. **{m['medicine__medicine_name']}**: **{m['total_qty']} units** sold (₹{m['total_rev']:,.2f})" for idx, m in enumerate(top_meds)]
            answer = "### Top Selling Medicines (by Volume)\n\n" + "\n".join(lines)

        return {
            'success': True,
            'intent': 'TOP_SELLING_MEDICINES',
            'confidence': confidence,
            'answer': answer,
            'action_url': '/sales/analytics/',
            'action_label': 'Open Sales Analytics',
            'follow_up_suggestions': ["Today's Sales", 'Consumption Analytics', 'AI Reorder Recommendations'],
            'data': {'top_medicines': list(top_meds)}
        }

    if intent == 'PROFIT_SUMMARY':
        if user_role != 'ADMIN':
            return {
                'success': True,
                'intent': 'PROFIT_RESTRICTED',
                'confidence': confidence,
                'answer': (
                    "🔒 **Access Restricted:** Financial gross profit and cost margin data are reserved for "
                    "Administrator accounts to maintain commercial pricing confidentiality. "
                    "As a Pharmacist, you can view transaction counts, sales revenue, and units dispensed."
                ),
                'action_url': '/sales/history/',
                'action_label': 'View Permitted Sales History',
                'follow_up_suggestions': ["Today's Sales", 'Weekly Sales', 'Monthly Sales'],
                'data': {}
            }

        thirty_days_ago = today - timedelta(days=30)
        sales_qs = Sale.objects.filter(sale_date__date__gte=thirty_days_ago, status__in=['COMPLETED', 'PARTIAL_RETURN'])
        rev = sales_qs.aggregate(s=Sum('total_amount'))['s'] or Decimal('0.00')

        items_qs = SaleItem.objects.filter(sale__in=sales_qs)
        cost = sum((item.purchase_unit_price or Decimal('0.00')) * item.quantity for item in items_qs)
        gross_profit = rev - cost
        margin_pct = round((gross_profit / rev) * Decimal('100.00'), 2) if rev > 0 else Decimal('0.00')

        answer = (
            f"### 30-Day Financial & Profit Analytics (Admin Only)\n\n"
            f"- **Gross Invoiced Revenue:** **₹{rev:,.2f}**\n"
            f"- **Historical Cost of Goods (COGS):** **₹{cost:,.2f}**\n"
            f"- **Gross Profit:** **₹{gross_profit:,.2f}**\n"
            f"- **Gross Margin:** **{margin_pct}%**"
        )
        return {
            'success': True,
            'intent': 'PROFIT_SUMMARY',
            'confidence': confidence,
            'answer': answer,
            'action_url': '/sales/analytics/',
            'action_label': 'View Financial Analytics',
            'follow_up_suggestions': ['Monthly Sales', 'Top Selling Medicines', 'Purchase Orders'],
            'data': {'revenue': float(rev), 'cost': float(cost), 'gross_profit': float(gross_profit), 'margin_pct': float(margin_pct)}
        }

    # ─────────────────────────────────────────────────────────────────────────
    # G. DISPENSING HISTORY (Quick Action 8)
    # ─────────────────────────────────────────────────────────────────────────

    if intent == 'DISPENSING_HISTORY':
        today_disp = DispensingRecord.objects.filter(created_at__date=today)
        count_today = today_disp.count()
        units_today = today_disp.aggregate(s=Sum('total_quantity'))['s'] or 0

        recent_records = (
            DispensingRecord.objects.select_related('medicine', 'pharmacist')
            .order_by('-created_at')[:6]
        )
        if not recent_records:
            answer = "No dispensing transactions recorded yet."
        else:
            lines = []
            for r in recent_records:
                # Mask patient name / UHID for privacy preservation
                raw_uhid = r.patient_id or ''
                masked_uhid = raw_uhid[:4] + '***' + raw_uhid[-2:] if len(raw_uhid) > 6 else 'UHID-***'
                lines.append(
                    f"- **{r.dispensing_id}**: {r.medicine.medicine_name} (**{r.total_quantity} units**) "
                    f"to `{masked_uhid}` by Pharmacist {r.pharmacist.username} ({r.created_at.strftime('%H:%M, %d %b')})"
                )

            answer = (
                f"### Secure Dispensing History\n\n"
                f"- **Today's Dispenses:** **{count_today} transactions** ({units_today} total units)\n\n"
                f"**Recent Completed Dispensing Events:**\n" + "\n".join(lines)
            )

        return {
            'success': True,
            'intent': 'DISPENSING_HISTORY',
            'confidence': confidence,
            'answer': answer,
            'action_url': '/dispensing/history/',
            'action_label': 'Open Dispensing History',
            'follow_up_suggestions': ['Controlled Drugs Register', 'Stock Discrepancies', 'Consumption Analytics'],
            'data': {'today_count': count_today, 'today_units': units_today}
        }

    # ─────────────────────────────────────────────────────────────────────────
    # H. CONSUMPTION ANALYTICS (Quick Action 9)
    # ─────────────────────────────────────────────────────────────────────────

    if intent == 'CONSUMPTION_ANALYTICS':
        from inventory.consumption_service import (
            get_consumption_for_period,
            get_consumption_transaction_count,
            get_data_quality_indicator,
            calculate_net_consumption_in_range
        )

        if med:
            c7 = get_consumption_for_period(med, 7)
            c30 = get_consumption_for_period(med, 30)
            c90 = get_consumption_for_period(med, 90)
            dq = get_data_quality_indicator(med)
            daily_vel = round(c30 / 30.0, 2) if c30 > 0 else 0.0

            answer = (
                f"### Consumption Analytics: **{med.medicine_name}**\n\n"
                f"- **7-Day Net Consumption:** **{c7} units**\n"
                f"- **30-Day Net Consumption:** **{c30} units**\n"
                f"- **90-Day Net Consumption:** **{c90} units**\n"
                f"- **Average Daily Velocity:** **{daily_vel} units/day**\n"
                f"- **Data Quality Indicator:** `{dq}` *(based on clinical transaction frequency)*\n\n"
                f"*Note: Calculations use deduplicated SALE and DISPENSE events minus customer returns. "
                f"Non-demand movements (DAMAGE, EXPIRY) are strictly excluded.*"
            )
            return {
                'success': True,
                'intent': 'CONSUMPTION_ANALYTICS',
                'confidence': confidence,
                'answer': answer,
                'action_url': f"/inventory/consumption/?search={med.medicine_name}",
                'action_label': f"View {med.medicine_name} Consumption Chart",
                'follow_up_suggestions': [
                    f"Show stock for {med.medicine_name}",
                    f"AI Reorder Recommendations",
                    f"What is {med.medicine_name}?"
                ],
                'data': {'medicine': str(med), 'consumed_7d': c7, 'consumed_30d': c30, 'consumed_90d': c90, 'daily_avg': daily_vel}
            }
        else:
            # General consumption overview: Top 5 consumed items in 30 days
            top_tx = (
                StockTransaction.objects.filter(
                    transaction_type__in=[StockTransaction.TransactionType.SALE, StockTransaction.TransactionType.DISPENSE],
                    created_at__gte=timezone.now() - timedelta(days=30)
                )
                .values('medicine__medicine_name')
                .annotate(total=Sum('quantity'))
                .order_by('-total')[:5]
            )
            if not top_tx:
                answer = "Insufficient consumption transactions recorded over the last 30 days."
            else:
                lines = [f"{idx+1}. **{t['medicine__medicine_name']}**: **{t['total']} units** consumed" for idx, t in enumerate(top_tx)]
                answer = "### 30-Day Hospital Pharmacy Consumption Leaders\n\n" + "\n".join(lines)

            return {
                'success': True,
                'intent': 'CONSUMPTION_ANALYTICS',
                'confidence': confidence,
                'answer': answer,
                'action_url': '/inventory/consumption/',
                'action_label': 'Open Consumption Analytics',
                'follow_up_suggestions': ['AI Reorder Recommendations', 'Inventory Overview', "Today's Sales"],
                'data': {'top_consumed': list(top_tx)}
            }

    # ─────────────────────────────────────────────────────────────────────────
    # I. AI REORDER RECOMMENDATIONS (Quick Action 10)
    # ─────────────────────────────────────────────────────────────────────────

    if intent == 'RECOMMEND_REORDER':
        reorders = intelligence_service.get_reorder_recommendations()
        count = len(reorders)

        if count == 0:
            answer = (
                "### AI Reorder Status: All Clear ✅\n\n"
                "Automated demand forecasting and lead-time analysis indicate no critical replenishment orders "
                "are required right now. Stock levels and open purchase orders sufficiently cover projected demand."
            )
        else:
            lines = []
            for r in reorders[:5]:
                sup_name = r['recommended_supplier'].company_name if r['recommended_supplier'] else 'Primary Vendor'
                lines.append(
                    f"- **{r['medicine'].medicine_name}** ({r['priority']} Priority):\n"
                    f"  Current stock: **{r['current_stock']} units** | 30d Forecast: {r['forecast_30d']} units | "
                    f"Open PO: {r['open_po_qty']} units\n"
                    f"  >> **Recommended Order: {r['recommended_qty']} units** from *{sup_name}* "
                    f"(Reason: {r.get('reason_tag', r.get('reason', 'Restock needed'))})"
                )

            answer = (
                f"### AI Reorder Recommendations ({count} medicines requiring restocking)\n\n"
                f"*Reorder quantities account for outstanding purchase orders, lead time, and safety buffers:*\n\n"
                + "\n\n".join(lines) +
                (f"\n\n*...plus {count - 5} more recommendations.*" if count > 5 else "")
            )

        return {
            'success': True,
            'intent': 'RECOMMEND_REORDER',
            'confidence': confidence,
            'answer': answer,
            'action_url': '/ai-intelligence/recommendations/',
            'action_label': 'Open Reorder Recommendation Center',
            'follow_up_suggestions': ['Low Stock Medicines', 'Purchase Orders', 'Consumption Analytics'],
            'data': {'reorder_count': count}
        }

    # ─────────────────────────────────────────────────────────────────────────
    # J. STOCK DISCREPANCIES (Quick Action 12)
    # ─────────────────────────────────────────────────────────────────────────

    if intent == 'STOCK_DISCREPANCY':
        open_disc = StockDiscrepancy.objects.filter(status=StockDiscrepancy.Status.OPEN)
        inv_disc = StockDiscrepancy.objects.filter(status__in=[StockDiscrepancy.Status.UNDER_REVIEW, 'INVESTIGATING'])
        res_disc = StockDiscrepancy.objects.filter(status=StockDiscrepancy.Status.RESOLVED)

        open_cnt = open_disc.count()
        inv_cnt = inv_disc.count()
        res_cnt = res_disc.count()

        if open_cnt == 0 and inv_cnt == 0:
            answer = (
                "### Stock Discrepancy Status: Reconciled ✅\n\n"
                f"Great news! There are **0 open stock discrepancies** across batches. "
                f"System physical counts match recorded batch ledgers ({res_cnt} historical discrepancies resolved)."
            )
        else:
            sample_lines = []
            for d in open_disc[:4]:
                sample_lines.append(
                    f"- **{d.medicine.medicine_name}** (Batch {d.batch.batch_number if d.batch else 'General'}): "
                    f"System: {d.expected_quantity} vs Counted: {d.actual_quantity} "
                    f"(Variance: **{d.difference:+d} units**, Type: `{d.get_discrepancy_type_display()}`)"
                )

            answer = (
                f"### Stock Discrepancy Queue\n\n"
                f"- **Open Discrepancies:** **{open_cnt} pending investigation**\n"
                f"- **Under Active Investigation:** **{inv_cnt} records**\n"
                f"- **Resolved Historically:** **{res_cnt} records**\n\n"
                f"**Pending Items:**\n" + "\n".join(sample_lines) +
                f"\n\n*Note: Chatbot is read-only. Pharmacists must reconcile discrepancies through the Physical Reconciliation workspace.*"
            )

        return {
            'success': True,
            'intent': 'STOCK_DISCREPANCY',
            'confidence': confidence,
            'answer': answer,
            'action_url': '/dispensing/discrepancies/',
            'action_label': 'Open Discrepancy Investigation Queue',
            'follow_up_suggestions': ['Inventory Overview', 'Low Stock Medicines', 'Dispensing History'],
            'data': {'open_count': open_cnt, 'investigating_count': inv_cnt, 'resolved_count': res_cnt}
        }

    # ─────────────────────────────────────────────────────────────────────────
    # K. PURCHASE ORDERS & PROCUREMENT (Quick Action 13)
    # ─────────────────────────────────────────────────────────────────────────

    if intent == 'PURCHASE_ORDERS':
        po_counts = (
            PurchaseOrder.objects.values('status')
            .annotate(cnt=Count('id'))
        )
        status_dict = {item['status']: item['cnt'] for item in po_counts}

        drafts = status_dict.get('DRAFT', 0)
        approved = status_dict.get('APPROVED', 0)
        ordered = status_dict.get('ORDERED', 0)
        partial = status_dict.get('PARTIAL_RECEIPT', 0)
        completed = status_dict.get('COMPLETED', 0)

        # Pending quantity across open POs
        pending_items = PurchaseOrderItem.objects.filter(
            purchase_order__status__in=['APPROVED', 'ORDERED', 'PARTIAL_RECEIPT']
        )
        incoming_units = pending_items.aggregate(s=Sum('quantity_pending'))['s'] or 0

        # Recent open purchase orders
        recent_pos = (
            PurchaseOrder.objects.filter(status__in=['APPROVED', 'ORDERED', 'PARTIAL_RECEIPT'])
            .select_related('supplier')
            .order_by('-order_date')[:4]
        )
        po_lines = []
        for po in recent_pos:
            sup = po.supplier.company_name if po.supplier else 'Vendor'
            po_lines.append(f"- **{po.po_number}** ({sup}): **₹{po.total_amount:,.2f}** · Status: `{po.get_status_display()}`")

        answer = (
            f"### Purchase Orders & Procurement Status\n\n"
            f"- **Active Incoming Orders:** **{ordered + approved + partial} POs** ({incoming_units:,} units pending delivery)\n"
            f"- **Draft / Unapproved:** **{drafts} POs**\n"
            f"- **Completed Orders:** **{completed} POs**\n\n"
            f"**Recent Open Orders:**\n" + ("\n".join(po_lines) if po_lines else "- No open orders currently pending delivery.")
        )
        return {
            'success': True,
            'intent': 'PURCHASE_ORDERS',
            'confidence': confidence,
            'answer': answer,
            'action_url': '/purchases/orders/',
            'action_label': 'View Purchase Orders',
            'follow_up_suggestions': ['AI Reorder Recommendations', 'Low Stock Medicines', 'Inventory Overview'],
            'data': {'incoming_units': incoming_units, 'open_pos': ordered + approved + partial}
        }

    # ─────────────────────────────────────────────────────────────────────────
    # L. CONTROLLED DRUGS REGISTER
    # ─────────────────────────────────────────────────────────────────────────

    if intent == 'CONTROLLED_REGISTER':
        controlled_meds = Medicine.objects.filter(
            regulatory_category__in=[
                Medicine.RegulatoryCategory.SCHEDULE_H1,
                Medicine.RegulatoryCategory.SCHEDULE_X,
                Medicine.RegulatoryCategory.NARCOTIC
            ]
        )
        count = controlled_meds.count()
        ledger_count = ControlledMedicineRegister.objects.count()
        names = ", ".join([m.medicine_name for m in controlled_meds[:6]])

        answer = (
            f"### Controlled Medicines Running Register\n\n"
            f"- **Registered Controlled SKUs:** **{count} medicines** (Schedule H1, Schedule X, NDPS Narcotics)\n"
            f"- **Running Ledger Entries:** **{ledger_count} verified transactions**\n"
            f"- **Regulated Drugs:** {names or 'None'}\n\n"
            f"*Mandatory Audit Rule: Every dispensing event requires patient identity verification, doctor registration number, and pharmacist signoff.*"
        )
        return {
            'success': True,
            'intent': 'CONTROLLED_REGISTER',
            'confidence': confidence,
            'answer': answer,
            'action_url': '/dispensing/controlled-register/',
            'action_label': 'Open Controlled Drug Register',
            'follow_up_suggestions': ['Dispensing History', 'Stock Discrepancies', 'Inventory Overview'],
            'data': {'controlled_count': count, 'ledger_count': ledger_count}
        }

    # ─────────────────────────────────────────────────────────────────────────
    # M. DEAD STOCK & OVERSTOCK INTELLIGENCE
    # ─────────────────────────────────────────────────────────────────────────

    if intent == 'DEAD_STOCK':
        dead_list = intelligence_service.get_dead_stock()
        count = len(dead_list)

        if count == 0:
            answer = "No dead stock detected. All active medicines have recorded stock movement within the last 90 days."
        else:
            names = [f"- **{d['medicine'].medicine_name}**: {d['current_stock']} units stagnant (0 movement > 90d)" for d in dead_list[:6]]
            answer = (
                f"### Dead Stock Warning ({count} inactive medicines)\n\n"
                f"The following medicines have recorded zero sales or dispensing for over 90 days:\n\n"
                + "\n".join(names)
            )

        return {
            'success': True,
            'intent': 'DEAD_STOCK',
            'confidence': confidence,
            'answer': answer,
            'action_url': '/ai-intelligence/',
            'action_label': 'View Dead Stock Analysis',
            'follow_up_suggestions': ['AI Reorder Recommendations', 'Inventory Overview', 'Expiry and Near-Expiry Batches'],
            'data': {'dead_stock_count': count}
        }

    if intent == 'OVERSTOCK':
        over_list = intelligence_service.get_overstock()
        count = len(over_list)

        if count == 0:
            answer = "No medicines are currently overstocked beyond their configured maximum stock capacity."
        else:
            names = [f"- **{o['medicine'].medicine_name}**: Current stock {o['current_stock']} units (Max limit: {o['max_stock']})" for o in over_list[:6]]
            answer = (
                f"### Overstock Alert ({count} surplus medicines)\n\n"
                f"The following medicines exceed maximum capacity and risk capital lockup:\n\n"
                + "\n".join(names)
            )

        return {
            'success': True,
            'intent': 'OVERSTOCK',
            'confidence': confidence,
            'answer': answer,
            'action_url': '/ai-intelligence/',
            'action_label': 'View Overstock Alerts',
            'follow_up_suggestions': ['Dead Stock Detection', 'AI Reorder Recommendations', 'Inventory Overview'],
            'data': {'overstock_count': count}
        }

    # ─────────────────────────────────────────────────────────────────────────
    # N. UNKNOWN / CLARIFICATION FALLBACK
    # ─────────────────────────────────────────────────────────────────────────

    total_meds = Medicine.objects.filter(status='ACTIVE').count()
    low_stk = Inventory.objects.filter(stock_status='LOW_STOCK').count()

    answer = (
        f"I am the **Sanalogx AI Assistant**. I couldn't match your query to a specific command, but I can help you with live pharmacy data.\n\n"
        f"Currently, MediVault tracks **{total_meds} active medicines** ({low_stk} low in stock).\n\n"
        f"**Here are questions you can try:**\n"
        f"- *\"What is Paracetamol?\"*\n"
        f"- *\"Show low stock medicines\"*\n"
        f"- *\"Show today's sales\"*\n"
        f"- *\"Which batches expire in 30 days?\"*\n"
        f"- *\"What should I reorder?\"*\n\n"
        f"Or choose one of the quick actions below:"
    )

    return {
        'success': True,
        'intent': 'UNKNOWN_FALLBACK',
        'confidence': confidence,
        'answer': answer,
        'action_url': '/inventory/',
        'action_label': 'Open Inventory Workspace',
        'follow_up_suggestions': [
            'Inventory Overview',
            'Low Stock Medicines',
            "Today's Sales",
            'Expiry and Near-Expiry Batches',
            'Help and Supported Questions'
        ],
        'data': {'total_medicines': total_meds, 'low_stock': low_stk}
    }
