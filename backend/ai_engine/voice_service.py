import os
import re
import joblib
import numpy as np
from datetime import timedelta
from django.utils import timezone
from django.db.models import Sum, Q

from medicines.models import Medicine, MedicineBatch
from inventory.models import Inventory, StockAlert, StockTransaction
from accounts.models import User
from suppliers.models import Supplier


_model = None
_vectorizer = None


def get_voice_model():
    """Singleton loader for ML model and vectorizer."""
    global _model, _vectorizer
    if _model is None or _vectorizer is None:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        model_path = os.path.join(base_dir, "ml", "models", "voice_intent_model.joblib")
        vectorizer_path = os.path.join(base_dir, "ml", "models", "voice_vectorizer.joblib")

        if os.path.exists(model_path) and os.path.exists(vectorizer_path):
            _model = joblib.load(model_path)
            _vectorizer = joblib.load(vectorizer_path)
        else:
            raise FileNotFoundError("ML voice model joblib files not found. Please run ml/train_voice_model.py first.")

    return _model, _vectorizer


def clean_query_text(text):
    if not isinstance(text, str):
        return ""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s\-]', '', text)
    return " ".join(text.split())


def extract_entities(query_text):
    """Extract medicine, quantity, and batch number entities from text."""
    entities = {'medicine': None, 'quantity': None, 'batch_number': None}

    # Extract quantity (e.g. "prepare 10 tablets")
    qty_match = re.search(r'\b(\d+)\b', query_text)
    if qty_match:
        entities['quantity'] = int(qty_match.group(1))

    # Extract batch number pattern (e.g. "PARA-2026-A" or "batch 001")
    batch_match = re.search(r'\b([a-z0-9]+\-[a-z0-9\-]+|[a-z0-9]{4,10})\b', query_text)
    if batch_match:
        entities['batch_number'] = batch_match.group(1).upper()

    # Match medicine name against database
    medicines = Medicine.objects.filter(status='ACTIVE')
    best_med = None
    max_len = 0

    for med in medicines:
        med_name_lower = med.medicine_name.lower()
        gen_name_lower = med.generic_name.lower() if med.generic_name else ''

        if med_name_lower in query_text or (gen_name_lower and gen_name_lower in query_text):
            match_len = max(len(med_name_lower), len(gen_name_lower))
            if match_len > max_len:
                max_len = match_len
                best_med = med

    if not best_med:
        # Simple word fuzzy match fallback
        words = query_text.split()
        for word in words:
            if len(word) >= 4:
                matched = Medicine.objects.filter(
                    Q(medicine_name__icontains=word) | Q(generic_name__icontains=word)
                ).first()
                if matched:
                    best_med = matched
                    break

    entities['medicine'] = best_med
    return entities


def process_voice_command(user, raw_text):
    """
    ML Intent Processing Engine:
    1. Preprocesses text
    2. ML Intent prediction & confidence
    3. Entity extraction
    4. Role permission gating
    5. Real ORM/MySQL query
    6. Structured response generation
    """
    cleaned_text = clean_query_text(raw_text)
    if not cleaned_text:
        return {
            'success': False,
            'speech': "I didn't catch that. Please speak or type your command.",
            'intent': 'UNKNOWN',
            'confidence': 0.0
        }

    try:
        model, vectorizer = get_voice_model()
        tfidf_vec = vectorizer.transform([cleaned_text])
        probs = model.predict_proba(tfidf_vec)[0]
        max_idx = np.argmax(probs)
        intent = model.classes_[max_idx]
        confidence = float(probs[max_idx])
    except Exception as e:
        intent = 'UNKNOWN'
        confidence = 0.0

    entities = extract_entities(cleaned_text)
    today = timezone.now().date()
    is_admin = (user.role == User.Role.ADMIN or user.is_superuser)

    # Low confidence fallback threshold
    if confidence < 0.35 and intent not in ['HELP', 'OPEN_INVENTORY', 'OPEN_DASHBOARD']:
        return {
            'success': True,
            'intent': 'LOW_CONFIDENCE',
            'confidence': round(confidence, 2),
            'speech': f"I'm not completely sure what you mean. Did you mean to check medicine stock or view low stock items?",
            'action_url': '/inventory/'
        }

    # Role Permission Gate
    admin_only_intents = ['PHARMACIST_STATUS', 'SUPPLIER_ANALYTICS', 'PURCHASE_STATUS']
    if intent in admin_only_intents and not is_admin:
        return {
            'success': True,
            'intent': intent,
            'confidence': round(confidence, 2),
            'speech': "Access restricted. Supplier analytics and administrative approvals are reserved for System Administrators.",
            'action_url': None
        }

    # Execute Intent Logic with real MySQL data
    if intent == 'CHECK_MEDICINE_STOCK':
        med = entities['medicine']
        if med:
            inv = getattr(med, 'inventory', None)
            stock_qty = inv.current_stock if inv else 0
            status_desc = inv.get_stock_status_display() if inv else 'Active'
            speech = f"{med.medicine_name} currently has {stock_qty} units available. Stock status is {status_desc}."
            return {
                'success': True,
                'intent': intent,
                'confidence': round(confidence, 2),
                'speech': speech,
                'action_url': f"/inventory/medicine/{med.id}/"
            }
        else:
            return {
                'success': True,
                'intent': intent,
                'confidence': round(confidence, 2),
                'speech': "I couldn't locate that specific medicine in the catalogue. Opening Medicine Master catalogue.",
                'action_url': "/medicines/"
            }

    elif intent == 'CHECK_LOW_STOCK':
        low_count = Inventory.objects.filter(stock_status='LOW_STOCK').count()
        speech = f"There are currently {low_count} medicines at or below their reorder level requiring restocking."
        return {
            'success': True,
            'intent': intent,
            'confidence': round(confidence, 2),
            'speech': speech,
            'action_url': "/inventory/?status=LOW_STOCK"
        }

    elif intent == 'CHECK_OUT_OF_STOCK':
        out_count = Inventory.objects.filter(current_stock=0).count()
        speech = f"There are currently {out_count} medicines completely out of stock with zero inventory."
        return {
            'success': True,
            'intent': intent,
            'confidence': round(confidence, 2),
            'speech': speech,
            'action_url': "/inventory/?status=OUT_OF_STOCK"
        }

    elif intent in ['CHECK_EXPIRY', 'CHECK_NEAR_EXPIRY']:
        batches = MedicineBatch.objects.filter(
            expiry_date__gt=today,
            expiry_date__lte=today + timedelta(days=90),
            quantity_available__gt=0
        )
        count = batches.count()
        speech = f"There are {count} active medicine batches expiring within the next 90 days."
        return {
            'success': True,
            'intent': intent,
            'confidence': round(confidence, 2),
            'speech': speech,
            'action_url': "/inventory/expiry/"
        }

    elif intent == 'INVENTORY_SUMMARY':
        total_meds = Medicine.objects.filter(status='ACTIVE').count()
        total_units = Inventory.objects.aggregate(s=Sum('current_stock'))['s'] or 0
        low_stock = Inventory.objects.filter(stock_status='LOW_STOCK').count()
        out_stock = Inventory.objects.filter(current_stock=0).count()
        speech = f"Inventory summary: {total_meds} active medicines totaling {total_units} units. {low_stock} items low in stock, and {out_stock} items out of stock."
        return {
            'success': True,
            'intent': intent,
            'confidence': round(confidence, 2),
            'speech': speech,
            'action_url': "/inventory/"
        }

    elif intent in ['SALES_SUMMARY', 'TODAY_REVENUE', 'SALES_TODAY']:
        from sales.models import Sale
        today_sales = Sale.objects.filter(sale_date__date=today, status=Sale.SaleStatus.COMPLETED)
        count = today_sales.count()
        revenue = today_sales.aggregate(s=Sum('total_amount'))['s'] or 0.00
        speech = f"Today's sales summary: {count} completed pharmacy sales totaling ₹{revenue:.2f} in revenue."
        return {
            'success': True,
            'intent': intent,
            'confidence': round(confidence, 2),
            'speech': speech,
            'action_url': "/sales/history/"
        }

    elif intent == 'CRITICAL_ALERTS':
        alerts_count = StockAlert.objects.filter(is_active=True).count()
        speech = f"You have {alerts_count} active critical notifications requiring attention."
        return {
            'success': True,
            'intent': intent,
            'confidence': round(confidence, 2),
            'speech': speech,
            'action_url': "/notifications/"
        }

    elif intent == 'DISPENSING_PREPARATION':
        med = entities['medicine']
        qty = entities['quantity'] or 1
        if med:
            inv = getattr(med, 'inventory', None)
            available = inv.current_stock if inv else 0
            speech = f"Prepared dispensing workspace for {qty} units of {med.medicine_name}. Available stock is {available} units. Please confirm visually before issuing."
            return {
                'success': True,
                'intent': intent,
                'confidence': round(confidence, 2),
                'speech': speech,
                'action_url': f"/inventory/dispense/direct/?medicine_id={med.id}&quantity={qty}"
            }
        else:
            return {
                'success': True,
                'intent': intent,
                'confidence': round(confidence, 2),
                'speech': "Please select a medicine from the direct dispensing workspace.",
                'action_url': "/inventory/dispense/direct/"
            }

    elif intent == 'OPEN_DASHBOARD':
        target_url = "/admin-dashboard/" if is_admin else "/pharmacist-dashboard/"
        return {
            'success': True,
            'intent': intent,
            'confidence': round(confidence, 2),
            'speech': "Navigating to your dashboard.",
            'action_url': target_url
        }

    elif intent == 'OPEN_INVENTORY':
        return {'success': True, 'intent': intent, 'confidence': round(confidence, 2), 'speech': "Opening Inventory Dashboard.", 'action_url': "/inventory/"}

    elif intent == 'OPEN_MEDICINES':
        return {'success': True, 'intent': intent, 'confidence': round(confidence, 2), 'speech': "Opening Medicine Master catalogue.", 'action_url': "/medicines/"}

    elif intent == 'OPEN_BATCH_STOCK':
        return {'success': True, 'intent': intent, 'confidence': round(confidence, 2), 'speech': "Opening Batch-wise stock workspace.", 'action_url': "/inventory/batches/"}

    elif intent == 'OPEN_EXPIRY':
        return {'success': True, 'intent': intent, 'confidence': round(confidence, 2), 'speech': "Opening Expiry Management workspace.", 'action_url': "/inventory/expiry/"}

    elif intent == 'OPEN_REQUESTS':
        return {'success': True, 'intent': intent, 'confidence': round(confidence, 2), 'speech': "Opening Live Request Queue.", 'action_url': "/inventory/requests/"}

    elif intent == 'OPEN_DISPENSING':
        return {'success': True, 'intent': intent, 'confidence': round(confidence, 2), 'speech': "Opening Direct Pharmacist Dispensing.", 'action_url': "/inventory/dispense/direct/"}

    elif intent == 'OPEN_INVOICES':
        return {'success': True, 'intent': intent, 'confidence': round(confidence, 2), 'speech': "Opening Dispensing Invoices.", 'action_url': "/reports/"}

    elif intent == 'SUPPLIER_ANALYTICS':
        return {'success': True, 'intent': intent, 'confidence': round(confidence, 2), 'speech': "Opening Supplier Price Intelligence Matrix.", 'action_url': "/suppliers/price-comparison/"}

    elif intent == 'PURCHASE_STATUS':
        return {'success': True, 'intent': intent, 'confidence': round(confidence, 2), 'speech': "Opening Procurement and Purchase Orders.", 'action_url': "/purchases/"}

    elif intent == 'PHARMACIST_STATUS':
        return {'success': True, 'intent': intent, 'confidence': round(confidence, 2), 'speech': "Opening Pharmacist Registration Approvals.", 'action_url': "/auth/admin/approvals/"}

    elif intent == 'HELP':
        return {
            'success': True,
            'intent': intent,
            'confidence': round(confidence, 2),
            'speech': "MediVoice AI can help you check stock, list low stock items, view expiring batches, open inventory pages, or prepare dispensing forms.",
            'action_url': None
        }

    else:
        return {
            'success': True,
            'intent': intent,
            'confidence': round(confidence, 2),
            'speech': f"Navigating to Inventory for query '{cleaned_text}'.",
            'action_url': "/inventory/"
        }
