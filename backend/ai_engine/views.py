"""
ai_engine/views.py
Phase 6 — AI Intelligence Dashboard & Database-Connected AI Chatbot APIs
"""

import json
import time
from decimal import Decimal
from datetime import timedelta

from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse
from django.db.models import Sum, Q, Avg
from django.utils import timezone
from django.contrib.auth.decorators import login_required

from inventory.views import admin_or_pharmacist_required, admin_required
from medicines.models import Medicine, MedicineBatch
from inventory.models import Inventory, StockTransaction, StockAlert
from . import intelligence_service, chat_service, voice_service
from django.contrib import messages
from django.views.decorators.http import require_POST
from purchases.models import PurchaseRequest, PurchaseRequestItem, PurchaseOrder, PurchaseOrderItem
from purchases.services import get_best_supplier
from .models import VoiceCommandLog, AIRecommendation, AuditLog


@admin_or_pharmacist_required
def ai_dashboard_view(request):
    """
    Phase 6 + Final Upgrade AI Intelligence Dashboard:
    - Real-time ML demand forecasts
    - Explainable AI reorder recommendations (with Open PO awareness)
    - Expiry risk analysis & unconsumed batch warnings
    - Dead stock & Overstock detection
    - Inventory risk scores
    """
    summary = intelligence_service.get_ai_insights_summary()

    # Pre-generate forecast for top 10 active medicines
    medicines = Medicine.objects.filter(status='ACTIVE')[:10]
    forecast_list = []
    for med in medicines:
        forecast_list.append(intelligence_service.get_demand_forecast(med))

    context = {
        'summary': summary,
        'forecast_list': forecast_list,
        'reorder_list': summary['reorder_list'],
        'dead_stock_list': summary['dead_stock_list'],
        'overstock_list': summary['overstock_list'],
        'expiry_risk_list': summary['expiry_risk_list'],
        'user_role': request.user.role if request.user.is_authenticated else 'GUEST',
    }
    return render(request, 'ai_engine/dashboard.html', context)


@admin_or_pharmacist_required
def recommendation_center_view(request):
    """
    AI Recommendation Center:
    Displays pending, reviewed, accepted, and rejected recommendations.
    Supports filtering by recommendation_type, status, and medicine.
    """
    # Auto-populate / sync recommendations from live intelligence
    intelligence_service.sync_ai_recommendations()

    status_filter = request.GET.get('status', 'PENDING')
    type_filter = request.GET.get('type', '')
    search_q = request.GET.get('search', '').strip()

    recommendations = AIRecommendation.objects.select_related(
        'medicine', 'supplier', 'recommended_supplier', 'reviewed_by'
    ).order_by('-priority_score', '-generated_at')

    if status_filter and status_filter != 'ALL':
        recommendations = recommendations.filter(status=status_filter)
    if type_filter:
        recommendations = recommendations.filter(recommendation_type=type_filter)
    if search_q:
        recommendations = recommendations.filter(
            Q(medicine__medicine_name__icontains=search_q) |
            Q(title__icontains=search_q) |
            Q(message__icontains=search_q)
        )

    # Counts
    pending_count = AIRecommendation.objects.filter(status=AIRecommendation.Status.PENDING).count()
    accepted_count = AIRecommendation.objects.filter(status=AIRecommendation.Status.ACCEPTED).count()
    rejected_count = AIRecommendation.objects.filter(status=AIRecommendation.Status.REJECTED).count()

    # Live recommendations preview if none in DB
    live_reorders = intelligence_service.get_reorder_recommendations()[:10]
    live_dead_stock = intelligence_service.get_dead_stock()[:10]

    context = {
        'recommendations': recommendations[:50],
        'pending_count': pending_count,
        'accepted_count': accepted_count,
        'rejected_count': rejected_count,
        'status_filter': status_filter,
        'type_filter': type_filter,
        'search_q': search_q,
        'live_reorders': live_reorders,
        'live_dead_stock': live_dead_stock,
        'rec_types': AIRecommendation.RecommendationType.choices,
    }
    return render(request, 'ai_engine/recommendation_center.html', context)


@admin_required
@require_POST
def handle_recommendation_action(request, pk):
    """
    Admin workflow: Accept, Reject, or Mark Reviewed for an AI Recommendation.
    """
    rec = get_object_or_404(AIRecommendation, pk=pk)
    action = request.POST.get('action', '').upper()
    reason = request.POST.get('reason', '').strip()

    if action == 'ACCEPT':
        rec.status = AIRecommendation.Status.ACCEPTED
        rec.reviewed_by = request.user
        rec.reviewed_at = timezone.now()
        rec.save(update_fields=['status', 'reviewed_by', 'reviewed_at'])

        pr_info = ""
        if rec.recommendation_type == AIRecommendation.RecommendationType.REORDER and rec.medicine:
            from .auto_reorder_service import create_auto_reorder_request
            qty = rec.recommended_quantity or rec.medicine.reorder_level
            reorder_res = create_auto_reorder_request(
                medicine=rec.medicine,
                requested_qty=qty,
                supplier=rec.recommended_supplier or rec.supplier,
                reason=f"Accepted AI Recommendation #{rec.id}: {rec.title}",
                triggered_by_user=request.user,
            )
            pr = reorder_res.get('purchase_request')
            if pr:
                pr_info = f" Purchase Request {pr.request_id} ({qty} units) is ready for review."

        messages.success(request, f"Recommendation for {rec.medicine.medicine_name if rec.medicine else 'item'} accepted.{pr_info}")
        AuditLog.log(
            action_type=AuditLog.ActionType.REPORT_GENERATED,
            performed_by=request.user,
            object_type='AIRecommendation',
            object_id=rec.id,
            description=f"Accepted AI Recommendation #{rec.id}: {rec.title}"
        )
    elif action == 'REJECT':
        rec.status = AIRecommendation.Status.REJECTED
        rec.reviewed_by = request.user
        rec.reviewed_at = timezone.now()
        rec.rejection_reason = reason
        rec.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'rejection_reason'])
        messages.info(request, f"Recommendation for {rec.medicine.medicine_name if rec.medicine else 'item'} rejected.")
    elif action == 'REVIEWED':
        rec.status = AIRecommendation.Status.REVIEWED
        rec.reviewed_by = request.user
        rec.reviewed_at = timezone.now()
        rec.save(update_fields=['status', 'reviewed_by', 'reviewed_at'])
        messages.info(request, f"Recommendation marked as reviewed.")

    return redirect(request.META.get('HTTP_REFERER', 'ai_recommendation_center'))


@admin_required
@require_POST
def convert_recommendation_to_pr(request):
    """
    Converts an AI Reorder recommendation into an official PurchaseRequest.
    Can be called from the AI Dashboard or Recommendation Center.
    """
    medicine_id = request.POST.get('medicine_id')
    recommended_qty = int(request.POST.get('recommended_qty', 1))
    medicine = get_object_or_404(Medicine, pk=medicine_id)

    from .auto_reorder_service import create_auto_reorder_request
    res = create_auto_reorder_request(
        medicine=medicine,
        requested_qty=recommended_qty,
        reason=f"AI Reorder Recommendation for {medicine.medicine_name}",
        triggered_by_user=request.user,
    )

    pr = res.get('purchase_request')
    if res.get('created'):
        messages.success(request, f"Purchase Request {pr.request_id} created successfully for {medicine.medicine_name} ({recommended_qty} units).")
    else:
        messages.info(request, res.get('message', f"Active Purchase Request {pr.request_id} already exists."))

    if pr:
        return redirect('purchase_request_detail', pk=pr.id)
    return redirect('purchase_request_list')


@admin_or_pharmacist_required
def ai_chat_api(request):
    """
    AJAX API: Sanalogx AI Assistant Database-Connected Chatbot Engine.
    GET: Health check and assistant capabilities.
    POST: Processes user questions with RBAC permissions, queries MySQL ORM,
          maintains safe session context, and returns grounded answers.
    """
    if request.method == 'GET':
        return JsonResponse({
            'success': True,
            'status': 'ready',
            'assistant_name': 'Sanalogx AI Assistant',
            'branding': 'Sanalogx AI Assistant',
            'system': 'Sanalogx',
            'capabilities': [
                'Inventory Overview',
                'Low Stock Medicines',
                'Out-of-Stock Medicines',
                'Expiry and Near-Expiry Batches',
                "Today's Sales",
                'Weekly Sales',
                'Monthly Sales',
                'Dispensing History',
                'Consumption Analytics',
                'AI Reorder Recommendations',
                'Medicine Information & Pharmacology',
                'Stock Discrepancies',
                'Purchase Orders',
                'Help and Supported Questions'
            ],
            'timestamp': timezone.now().isoformat()
        })

    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed.'}, status=405)

    try:
        # ─── Session-based Rate Limiter (30 requests / 60 s) ─────────────────
        RATE_LIMIT = 30    # max requests
        RATE_WINDOW = 60   # sliding window in seconds
        now_ts = time.time()
        rl_key = 'sanalogx_rl_timestamps'
        timestamps = request.session.get(rl_key, [])
        # Prune timestamps outside the current window
        timestamps = [t for t in timestamps if now_ts - t < RATE_WINDOW]
        if len(timestamps) >= RATE_LIMIT:
            retry_after = int(RATE_WINDOW - (now_ts - timestamps[0])) + 1
            return JsonResponse({
                'success': False,
                'intent': 'RATE_LIMITED',
                'answer': (
                    f"⚠️ You've sent {RATE_LIMIT} requests in the last minute. "
                    f"Please wait {retry_after} second(s) before asking again."
                ),
                'timestamp': timezone.now().isoformat(),
            }, status=429)
        timestamps.append(now_ts)
        request.session[rl_key] = timestamps
        request.session.modified = True
        # ─────────────────────────────────────────────────────────────────────

        try:
            data = json.loads(request.body)
        except Exception:
            return JsonResponse({'success': False, 'error': 'Invalid JSON request payload.'}, status=400)

        # Handle Clear Chat Context action
        if data.get('action') == 'clear':
            if 'sanalogx_chat_context' in request.session:
                del request.session['sanalogx_chat_context']
                request.session.modified = True
            return JsonResponse({
                'success': True,
                'cleared': True,
                'answer': 'Conversation memory cleared. What would you like to check next?',
                'timestamp': timezone.now().isoformat()
            })

        message = data.get('message', '').strip()
        if not message:
            return JsonResponse({
                'success': False,
                'intent': 'EMPTY',
                'answer': "Please ask a question about pharmacy stock, sales, dispensing, expiry, reorders, or medicine information.",
                'timestamp': timezone.now().isoformat()
            })

        response_data = chat_service.process_ai_chat_query(message, request.user, session=request.session)
        response_data['timestamp'] = timezone.now().isoformat()
        return JsonResponse(response_data)

    except Exception as exc:
        return JsonResponse({
            'success': False,
            'error': 'Service temporarily unavailable.',
            'answer': "An unexpected error occurred while processing your request. Please try again.",
            'timestamp': timezone.now().isoformat()
        }, status=500)


@admin_or_pharmacist_required
def ai_recommendations_api(request):
    """JSON API: Return structured AI recommendations for external integration."""
    rec_type = request.GET.get('type', '')
    summary = intelligence_service.get_ai_insights_summary()

    if rec_type == 'reorder':
        data = summary['reorder_list']
    elif rec_type == 'dead_stock':
        data = summary['dead_stock_list']
    elif rec_type == 'expiry_risk':
        data = summary['expiry_risk_list']
    else:
        data = summary

    return JsonResponse({'success': True, 'recommendations': data})


@admin_or_pharmacist_required
def voice_assistant_api(request):
    """
    MediVoice AI Assistant Command Processor.
    Uses ML Intent Classifier + Entity Extractor + ORM Queries.
    Logs command execution to VoiceCommandLog.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed.'}, status=405)

    try:
        data = json.loads(request.body)
        raw_query = data.get('query', '').strip()

        if not raw_query:
            return JsonResponse({
                'success': False,
                'speech': "I didn't catch that. Could you please repeat your command?",
            })

        res = voice_service.process_voice_command(request.user, raw_query)

        # Log command execution
        try:
            VoiceCommandLog.objects.create(
                user=request.user,
                raw_text=raw_query,
                intent=res.get('intent', 'UNKNOWN'),
                confidence=res.get('confidence', 0.0),
                response_text=res.get('speech', ''),
                action_url=res.get('action_url')
            )
        except Exception:
            pass

        return JsonResponse({
            'success': True,
            'intent': res.get('intent'),
            'confidence': res.get('confidence'),
            'speech': res.get('speech'),
            'action': 'REDIRECT' if res.get('action_url') else 'NONE',
            'url': res.get('action_url')
        })

    except Exception as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)
