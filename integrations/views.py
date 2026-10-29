"""
integrations/views.py
MediVault — Hospital & EHR Integration UI Views

Provides:
1. Integration Dashboard: Real database statistics, active hospital systems, recent orders, audit logs.
2. Order Review & Verification: Pharmacist clinical review screen to map and approve inbound EHR orders.
3. Mock EHR Simulator: Interactive testing console for simulating inbound FHIR R4 orders from Cerner/Oracle Health.
"""

import json
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.db.models import Count, Max, Q

from accounts.models import User
from medicines.models import Medicine
from dispensing.models import Prescription, PrescriptionItem
from analytics.freshness_service import get_data_freshness_report
from analytics.date_service import resolve_date_range
from .models import ExternalHospitalSystem, HospitalMedicationOrder, IntegrationMessageLog
from . import fhir_service


def admin_or_pharmacist_required(view_func):
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('pharmacist_login')
        user = request.user
        if user.role == User.Role.ADMIN or user.is_superuser:
            return view_func(request, *args, **kwargs)
        if user.role == User.Role.PHARMACIST and user.status == 'APPROVED':
            return view_func(request, *args, **kwargs)
        messages.error(request, "Access restricted to approved pharmacy personnel and administrators.")
        return redirect('home')
    return _wrapped


def admin_required(view_func):
    """Restrict view to ADMIN role only."""
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('pharmacist_login')
        if request.user.role == User.Role.ADMIN or request.user.is_superuser:
            return view_func(request, *args, **kwargs)
        messages.error(request, "This page is restricted to administrators.")
        return redirect('home')
    return _wrapped

@admin_or_pharmacist_required
def integration_dashboard_view(request):
    """
    EHR Integration Dashboard showing actual received records, sync statuses, and review queues.
    """
    period = request.GET.get('period', '30d')
    date_info = resolve_date_range(period)
    freshness = get_data_freshness_report()

    # Active Hospital Systems & Connection Analysis
    systems = ExternalHospitalSystem.objects.all()
    real_systems = systems.filter(is_demo_simulator=False, is_active=True)
    has_real_connection = real_systems.exists()
    real_systems_count = real_systems.count()

    # Real Database Aggregations
    total_orders = HospitalMedicationOrder.objects.count()
    awaiting_review = HospitalMedicationOrder.objects.filter(status=HospitalMedicationOrder.OrderStatus.PENDING_REVIEW).count()
    verified_orders = HospitalMedicationOrder.objects.filter(status=HospitalMedicationOrder.OrderStatus.VERIFIED).count()
    dispensed_orders = HospitalMedicationOrder.objects.filter(status=HospitalMedicationOrder.OrderStatus.DISPENSED).count()
    rejected_orders = HospitalMedicationOrder.objects.filter(status=HospitalMedicationOrder.OrderStatus.REJECTED).count()
    failed_orders = HospitalMedicationOrder.objects.filter(status=HospitalMedicationOrder.OrderStatus.FAILED).count()
    validated_orders = max(0, total_orders - failed_orders)

    total_failures = IntegrationMessageLog.objects.filter(
        status__in=[IntegrationMessageLog.Status.FAILED, IntegrationMessageLog.Status.VALIDATION_ERROR]
    ).count()

    latest_order = HospitalMedicationOrder.objects.order_by('-received_at').first()
    last_sync = ExternalHospitalSystem.objects.aggregate(m=Max('last_sync_at'))['m']

    # Order filtering & Search
    status_filter = request.GET.get('status', '')
    search_q = request.GET.get('q', '').strip()
    orders_qs = HospitalMedicationOrder.objects.select_related('hospital_system', 'mapped_medicine', 'prescription').all()
    if status_filter:
        orders_qs = orders_qs.filter(status=status_filter)
    if search_q:
        orders_qs = orders_qs.filter(
            Q(patient_name__icontains=search_q) |
            Q(patient_uhid__icontains=search_q) |
            Q(raw_medication_name__icontains=search_q) |
            Q(external_order_id__icontains=search_q)
        )

    recent_orders = orders_qs[:35]
    recent_logs = IntegrationMessageLog.objects.select_related('hospital_system').order_by('-created_at')[:20]

    context = {
        'systems': systems,
        'has_real_connection': has_real_connection,
        'real_systems_count': real_systems_count,
        'total_orders': total_orders,
        'awaiting_review': awaiting_review,
        'verified_orders': verified_orders,
        'dispensed_orders': dispensed_orders,
        'rejected_orders': rejected_orders,
        'failed_orders': failed_orders,
        'validated_orders': validated_orders,
        'total_failures': total_failures,
        'latest_order': latest_order,
        'last_sync': last_sync,
        'recent_orders': recent_orders,
        'recent_logs': recent_logs,
        'freshness': freshness,
        'date_info': date_info,
        'status_filter': status_filter,
        'search_q': search_q,
    }
    return render(request, 'integrations/dashboard.html', context)


@admin_or_pharmacist_required
def order_review_view(request, order_id):
    """
    Pharmacist review & verification of an inbound EHR medication order.
    """
    order = get_object_or_404(HospitalMedicationOrder.objects.select_related('hospital_system', 'mapped_medicine', 'prescription'), id=order_id)
    active_medicines = Medicine.objects.filter(status='ACTIVE').select_related('category', 'inventory').order_by('medicine_name')

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'verify':
            med_id = request.POST.get('medicine_id')
            if not med_id:
                messages.error(request, "Please select an internal Sanalogx Medicine to map this order.")
                return redirect('order_review', order_id=order.id)

            medicine = get_object_or_404(Medicine, id=med_id)
            order.mapped_medicine = medicine
            order.status = HospitalMedicationOrder.OrderStatus.VERIFIED
            order.reviewed_by = request.user
            order.reviewed_at = timezone.now()
            order.save(update_fields=['mapped_medicine', 'status', 'reviewed_by', 'reviewed_at', 'updated_at'])

            # Update or create linked prescription item
            if order.prescription:
                order.prescription.status = Prescription.Status.VERIFIED
                order.prescription.verified_by = request.user
                order.prescription.verified_at = timezone.now()
                order.prescription.save(update_fields=['status', 'verified_by', 'verified_at', 'updated_at'])

                item = order.prescription.items.first()
                if item:
                    item.medicine = medicine
                    item.status = PrescriptionItem.Status.VERIFIED
                    item.save(update_fields=['medicine', 'status'])
                else:
                    PrescriptionItem.objects.create(
                        prescription=order.prescription,
                        medicine=medicine,
                        prescribed_quantity=order.prescribed_quantity,
                        requested_quantity=order.prescribed_quantity,
                        dosage_instructions=order.dosage_instruction,
                        status=PrescriptionItem.Status.VERIFIED
                    )

            messages.success(
                request,
                f"Medication Order #{order.external_order_id} verified and mapped to {medicine.medicine_name}. "
                "Order is now ready for dispensing in the Secure Dispensing Workspace."
            )
            return redirect('integration_dashboard')

        elif action == 'reject':
            reason = request.POST.get('rejection_reason', '').strip()
            if not reason:
                messages.error(request, "Please specify a clinical rejection reason.")
                return redirect('order_review', order_id=order.id)

            order.status = HospitalMedicationOrder.OrderStatus.REJECTED
            order.rejection_reason = reason
            order.reviewed_by = request.user
            order.reviewed_at = timezone.now()
            order.save(update_fields=['status', 'rejection_reason', 'reviewed_by', 'reviewed_at', 'updated_at'])

            if order.prescription:
                order.prescription.status = Prescription.Status.REJECTED
                order.prescription.rejection_reason = reason
                order.prescription.save(update_fields=['status', 'rejection_reason', 'updated_at'])

            messages.warning(request, f"Medication Order #{order.external_order_id} has been rejected.")
            return redirect('integration_dashboard')

        elif action == 'hold':
            hold_reason = request.POST.get('hold_reason', '').strip()
            order.rejection_reason = f"[Clinical Hold] {hold_reason}" if hold_reason else "[Clinical Hold] Pending prescriber clarification."
            order.save(update_fields=['rejection_reason', 'updated_at'])
            messages.info(
                request,
                f"Medication Order #{order.external_order_id} placed on Clinical Hold. Order remains awaiting pharmacist review."
            )
            return redirect('integration_dashboard')

    context = {
        'order': order,
        'active_medicines': active_medicines,
    }
    return render(request, 'integrations/order_review.html', context)


@admin_or_pharmacist_required
def integration_orders_view(request):
    """
    Paginated list of all HospitalMedicationOrder records — the Medication Orders Queue.
    Filterable by status. Pharmacists use this to find and review pending orders.
    """
    from django.core.paginator import Paginator

    date_info = resolve_date_range(request.GET.get('period', '30d'))
    status_filter = request.GET.get('status', '')
    search = request.GET.get('q', '').strip()

    orders_qs = HospitalMedicationOrder.objects.select_related(
        'hospital_system', 'mapped_medicine', 'prescription'
    ).order_by('-created_at')

    if status_filter:
        orders_qs = orders_qs.filter(status=status_filter)
    if search:
        orders_qs = orders_qs.filter(
            Q(patient_name__icontains=search) |
            Q(patient_uhid__icontains=search) |
            Q(raw_medication_name__icontains=search) |
            Q(external_order_id__icontains=search)
        )

    # Status counts for filter badges
    all_statuses = HospitalMedicationOrder.objects.values('status').annotate(n=Count('status'))
    status_counts = {row['status']: row['n'] for row in all_statuses}

    paginator = Paginator(orders_qs, 25)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    context = {
        'page_obj': page_obj,
        'status_filter': status_filter,
        'search': search,
        'date_info': date_info,
        'status_counts': status_counts,
        'order_statuses': HospitalMedicationOrder.OrderStatus.choices,
    }
    return render(request, 'integrations/orders_list.html', context)


@admin_required
def mock_ehr_simulator_view(request):
    """
    Interactive test console allowing pharmacists and developers to simulate inbound
    FHIR R4 MedicationRequests from Cerner/Oracle Health without requiring real hospital credentials.
    """
    system, _ = ExternalHospitalSystem.objects.get_or_create(
        system_code='CERNER_MOCK',
        defaults={
            'name': 'Oracle Health / Cerner Millennium (Mock Adapter)',
            'system_type': ExternalHospitalSystem.SystemType.ORACLE_CERNER,
            'is_demo_simulator': True,
        }
    )

    simulation_result = None

    if request.method == 'POST':
        raw_json = request.POST.get('fhir_json', '')
        try:
            payload = json.loads(raw_json)
            order, is_created = fhir_service.ingest_fhir_medication_order(
                payload=payload,
                hospital_system=system,
                ip_address=request.META.get('REMOTE_ADDR')
            )
            simulation_result = {
                'success': True,
                'is_created': is_created,
                'order': order,
                'message': "Order ingested successfully! Awaiting pharmacist clinical review." if is_created else "Duplicate order detected (Idempotency working). Existing order returned."
            }
            messages.success(request, simulation_result['message'])
        except Exception as e:
            simulation_result = {
                'success': False,
                'error': str(e)
            }
            messages.error(request, f"Simulation error: {str(e)}")

    sample_paracetamol = {
        "resourceType": "MedicationRequest",
        "id": f"CERNER-RX-{timezone.now().strftime('%Y%m%d%H%M%S')}",
        "status": "active",
        "intent": "order",
        "priority": "routine",
        "subject": {
            "reference": "Patient/UHID-77291",
            "display": "Sarah Jenkins (Ward 4B Bed 12)"
        },
        "requester": {
            "display": "Dr. Eleanor Vance, MD (Attending Physician)"
        },
        "medicationCodeableConcept": {
            "coding": [
                {
                    "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                    "code": "198440",
                    "display": "Paracetamol 500mg Tablet"
                }
            ],
            "text": "Paracetamol 500mg Tablet"
        },
        "dispenseRequest": {
            "quantity": {
                "value": 30,
                "unit": "tablets"
            }
        },
        "dosageInstruction": [
            {
                "text": "1 tablet orally every 6 hours as needed for fever/pain (Max 4g/day)"
            }
        ],
        "reasonCode": [
            {
                "text": "Post-operative analgesia & pyrexia management"
            }
        ]
    }

    sample_amoxicillin = {
        "resourceType": "MedicationRequest",
        "id": f"CERNER-RX-AMOX-{timezone.now().strftime('%Y%m%d%H%M%S')}",
        "status": "active",
        "intent": "order",
        "priority": "urgent",
        "subject": {
            "reference": "Patient/UHID-55102",
            "display": "Rajesh Kumar (Emergency Observation)"
        },
        "requester": {
            "display": "Dr. Marcus Brody, MD (Emergency Medicine)"
        },
        "medicationCodeableConcept": {
            "coding": [
                {
                    "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                    "code": "723",
                    "display": "Amoxicillin 500mg Capsule"
                }
            ],
            "text": "Amoxicillin 500mg Capsule"
        },
        "dispenseRequest": {
            "quantity": {
                "value": 21,
                "unit": "capsules"
            }
        },
        "dosageInstruction": [
            {
                "text": "1 capsule 3 times daily after meals for 7 days"
            }
        ],
        "reasonCode": [
            {
                "text": "Acute bacterial respiratory tract infection"
            }
        ]
    }

    context = {
        'system': system,
        'sample_paracetamol_json': json.dumps(sample_paracetamol, indent=2),
        'sample_amoxicillin_json': json.dumps(sample_amoxicillin, indent=2),
        'simulation_result': simulation_result,
    }
    return render(request, 'integrations/mock_simulator.html', context)
