"""
suppliers/views.py
Phase 4 — Full Supplier Intelligence & Management Views
"""
from decimal import Decimal

from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.http import JsonResponse
from django.db.models import Min, Max, Avg, Count, Q, Sum
from django.contrib.auth.decorators import login_required
from django.utils import timezone

from inventory.views import admin_required, admin_or_pharmacist_required
from medicines.models import Medicine
from .models import Supplier, SupplierMedicine, SupplierPriceHistory


# ─────────────────────────────────────────────────────────────────────────────
# SUPPLIER LIST
# ─────────────────────────────────────────────────────────────────────────────

@admin_required
def supplier_list_view(request):
    """Admin: List all suppliers with performance metrics and ratings."""
    suppliers_qs = Supplier.objects.all().order_by('-rating', 'company_name')

    status_filter = request.GET.get('status', '')
    search_q = request.GET.get('search', '').strip()

    if status_filter:
        suppliers_qs = suppliers_qs.filter(status=status_filter)
    if search_q:
        suppliers_qs = suppliers_qs.filter(
            Q(company_name__icontains=search_q) |
            Q(contact_person__icontains=search_q) |
            Q(email__icontains=search_q) |
            Q(gst_number__icontains=search_q)
        )

    context = {
        'suppliers': suppliers_qs,
        'total_suppliers': Supplier.objects.count(),
        'active_suppliers': Supplier.objects.filter(status=Supplier.SupplierStatus.ACTIVE).count(),
        'avg_rating': Supplier.objects.aggregate(avg=Avg('rating'))['avg'] or 0.0,
        'status_filter': status_filter,
        'search_q': search_q,
        'statuses': Supplier.SupplierStatus.choices,
    }
    return render(request, 'suppliers/supplier_list.html', context)


# ─────────────────────────────────────────────────────────────────────────────
# ADD SUPPLIER
# ─────────────────────────────────────────────────────────────────────────────

@admin_required
def add_supplier_view(request):
    """Admin: Create a new Supplier."""
    if request.method == 'POST':
        company_name = request.POST.get('company_name', '').strip()
        contact_person = request.POST.get('contact_person', '').strip()
        email = request.POST.get('email', '').strip()
        phone = request.POST.get('phone', '').strip()
        address_line1 = request.POST.get('address_line1', '').strip()
        city = request.POST.get('city', '').strip()
        state = request.POST.get('state', '').strip()
        gst_number = request.POST.get('gst_number', '').strip()
        drug_license_number = request.POST.get('drug_license_number', '').strip()
        average_delivery_days = request.POST.get('average_delivery_days', 7)
        payment_terms = request.POST.get('payment_terms', Supplier.PaymentTerms.NET_30)
        notes = request.POST.get('notes', '').strip()

        if not company_name:
            messages.error(request, "Company name is required.")
        elif Supplier.objects.filter(company_name__iexact=company_name).exists():
            messages.error(request, f"A supplier named '{company_name}' already exists.")
        else:
            try:
                supplier = Supplier.objects.create(
                    company_name=company_name,
                    contact_person=contact_person,
                    email=email,
                    phone=phone,
                    address_line1=address_line1,
                    city=city,
                    state=state,
                    gst_number=gst_number,
                    drug_license_number=drug_license_number,
                    average_delivery_days=int(average_delivery_days),
                    payment_terms=payment_terms,
                    notes=notes,
                    status=Supplier.SupplierStatus.ACTIVE,
                    created_by=request.user,
                )
                messages.success(request, f"Supplier '{supplier.company_name}' added successfully.")
                return redirect('supplier_detail', pk=supplier.pk)
            except Exception as e:
                messages.error(request, f"Error saving supplier: {e}")

    return render(request, 'suppliers/add_supplier.html', {
        'payment_terms': Supplier.PaymentTerms.choices,
    })


# ─────────────────────────────────────────────────────────────────────────────
# EDIT SUPPLIER
# ─────────────────────────────────────────────────────────────────────────────

@admin_required
def edit_supplier_view(request, pk):
    """Admin: Edit an existing Supplier."""
    supplier = get_object_or_404(Supplier, pk=pk)

    if request.method == 'POST':
        supplier.contact_person = request.POST.get('contact_person', '').strip()
        supplier.email = request.POST.get('email', '').strip()
        supplier.phone = request.POST.get('phone', '').strip()
        supplier.address_line1 = request.POST.get('address_line1', '').strip()
        supplier.city = request.POST.get('city', '').strip()
        supplier.state = request.POST.get('state', '').strip()
        supplier.gst_number = request.POST.get('gst_number', '').strip()
        supplier.drug_license_number = request.POST.get('drug_license_number', '').strip()
        supplier.average_delivery_days = int(request.POST.get('average_delivery_days', 7))
        supplier.payment_terms = request.POST.get('payment_terms', Supplier.PaymentTerms.NET_30)
        supplier.status = request.POST.get('status', Supplier.SupplierStatus.ACTIVE)
        supplier.notes = request.POST.get('notes', '').strip()

        try:
            supplier.save()
            messages.success(request, f"Supplier '{supplier.company_name}' updated successfully.")
            return redirect('supplier_detail', pk=supplier.pk)
        except Exception as e:
            messages.error(request, f"Error updating supplier: {e}")

    return render(request, 'suppliers/edit_supplier.html', {
        'supplier': supplier,
        'payment_terms': Supplier.PaymentTerms.choices,
        'statuses': Supplier.SupplierStatus.choices,
    })


# ─────────────────────────────────────────────────────────────────────────────
# SUPPLIER DETAIL
# ─────────────────────────────────────────────────────────────────────────────

@admin_required
def supplier_detail_view(request, pk):
    """Admin: Single Supplier detail, medicine catalogue, and price history."""
    supplier = get_object_or_404(Supplier, pk=pk)
    catalogue = supplier.supplier_medicines.select_related('medicine').order_by('medicine__medicine_name')
    history = supplier.price_history.select_related('medicine').order_by('-changed_at')[:20]

    # Compute performance from real data
    from purchases.models import PurchaseOrder, GoodsReceipt
    completed_pos = PurchaseOrder.objects.filter(
        supplier=supplier, status=PurchaseOrder.POStatus.COMPLETED
    )
    total_pos = PurchaseOrder.objects.filter(supplier=supplier).count()

    context = {
        'supplier': supplier,
        'catalogue': catalogue,
        'history': history,
        'catalogue_count': catalogue.count(),
        'total_pos': total_pos,
        'completed_pos': completed_pos.count(),
    }
    return render(request, 'suppliers/supplier_detail.html', context)


# ─────────────────────────────────────────────────────────────────────────────
# SUPPLIER-MEDICINE MAPPING
# ─────────────────────────────────────────────────────────────────────────────

@admin_required
def supplier_medicine_map_view(request, pk):
    """Admin: Map medicines to a supplier with price, delivery days, MOQ."""
    supplier = get_object_or_404(Supplier, pk=pk)
    medicines = Medicine.objects.filter(status='ACTIVE').order_by('medicine_name')

    if request.method == 'POST':
        medicine_id = request.POST.get('medicine')
        unit_price_str = request.POST.get('unit_price', '0')
        delivery_days = request.POST.get('delivery_days', 7)
        minimum_order_qty = request.POST.get('minimum_order_quantity', 1)
        notes = request.POST.get('notes', '').strip()

        try:
            medicine = Medicine.objects.get(pk=medicine_id)
            unit_price = Decimal(unit_price_str)

            sm, created = SupplierMedicine.objects.get_or_create(
                supplier=supplier,
                medicine=medicine,
                defaults={
                    'unit_price': unit_price,
                    'delivery_days': int(delivery_days),
                    'minimum_order_quantity': int(minimum_order_qty),
                    'notes': notes,
                    'is_available': True,
                }
            )
            if not created:
                # Record price history if price changed
                if sm.unit_price != unit_price:
                    SupplierPriceHistory.objects.create(
                        supplier=supplier,
                        medicine=medicine,
                        old_price=sm.unit_price,
                        new_price=unit_price,
                        changed_by=request.user,
                        reason="Price updated via Supplier-Medicine Mapping",
                    )
                sm.unit_price = unit_price
                sm.delivery_days = int(delivery_days)
                sm.minimum_order_quantity = int(minimum_order_qty)
                sm.notes = notes
                sm.is_available = True
                sm.save()

            action = "added to" if created else "updated for"
            messages.success(request, f"{medicine.medicine_name} {action} {supplier.company_name} catalogue at ₹{unit_price}/unit.")
            return redirect('supplier_medicine_map', pk=supplier.pk)
        except Medicine.DoesNotExist:
            messages.error(request, "Selected medicine not found.")
        except Exception as e:
            messages.error(request, f"Error: {e}")

    existing_catalogue = supplier.supplier_medicines.select_related('medicine').order_by('medicine__medicine_name')

    return render(request, 'suppliers/supplier_medicine_map.html', {
        'supplier': supplier,
        'medicines': medicines,
        'existing_catalogue': existing_catalogue,
    })


# ─────────────────────────────────────────────────────────────────────────────
# PRICE COMPARISON
# ─────────────────────────────────────────────────────────────────────────────

@admin_required
def price_comparison_view(request):
    """Admin: Supplier Price Comparison Matrix — best price, trend, recommendations."""
    medicines_with_suppliers = Medicine.objects.filter(
        supplier_medicines__isnull=False,
        status='ACTIVE'
    ).distinct().prefetch_related('supplier_medicines__supplier')

    comparison_data = []
    for med in medicines_with_suppliers:
        sup_meds = med.supplier_medicines.select_related('supplier').filter(
            supplier__status=Supplier.SupplierStatus.ACTIVE
        ).order_by('unit_price')

        if not sup_meds.exists():
            continue

        best_offer = sup_meds.first()
        avg_price = sup_meds.aggregate(a=Avg('unit_price'))['a'] or Decimal('0.00')

        recent_history = SupplierPriceHistory.objects.filter(medicine=med).order_by('-changed_at').first()
        trend = 'STABLE'
        if recent_history:
            if recent_history.new_price > recent_history.old_price:
                trend = 'INCREASED'
            elif recent_history.new_price < recent_history.old_price:
                trend = 'DECREASED'

        comparison_data.append({
            'medicine': med,
            'suppliers_count': sup_meds.count(),
            'best_offer': best_offer,
            'avg_price': avg_price,
            'all_offers': sup_meds,
            'price_trend': trend,
            'recent_history': recent_history,
        })

    context = {
        'comparison_data': comparison_data,
        'total_compared_medicines': len(comparison_data),
    }
    return render(request, 'suppliers/price_comparison.html', context)


# ─────────────────────────────────────────────────────────────────────────────
# SUPPLIER PERFORMANCE
# ─────────────────────────────────────────────────────────────────────────────

@admin_required
def supplier_performance_view(request):
    """Admin: Supplier performance dashboard calculated from real PO/GRN data."""
    from purchases.models import PurchaseOrder
    from purchases.services import recalculate_supplier_performance

    suppliers = Supplier.objects.filter(status=Supplier.SupplierStatus.ACTIVE)
    performance_data = []

    for sup in suppliers:
        # Trigger recalculation from real data
        recalculate_supplier_performance(sup)
        sup.refresh_from_db()

        total_pos = PurchaseOrder.objects.filter(supplier=sup).count()
        completed_pos = PurchaseOrder.objects.filter(
            supplier=sup, status=PurchaseOrder.POStatus.COMPLETED
        ).count()
        medicines_count = sup.supplier_medicines.count()

        performance_data.append({
            'supplier': sup,
            'total_pos': total_pos,
            'completed_pos': completed_pos,
            'medicines_count': medicines_count,
            'has_data': total_pos > 0,
        })

    performance_data.sort(key=lambda x: float(x['supplier'].on_time_delivery_rate), reverse=True)

    return render(request, 'suppliers/supplier_performance.html', {
        'performance_data': performance_data,
    })


# ─────────────────────────────────────────────────────────────────────────────
# AJAX — Get supplier medicines for dynamic PO building
# ─────────────────────────────────────────────────────────────────────────────

@admin_required
def ajax_supplier_medicines(request):
    """AJAX: Return medicines catalogue for a supplier."""
    supplier_id = request.GET.get('supplier_id')
    if not supplier_id:
        return JsonResponse({'medicines': []})

    try:
        supplier = Supplier.objects.get(pk=supplier_id)
        meds = []
        for sm in supplier.supplier_medicines.select_related('medicine').filter(is_available=True):
            inv = getattr(sm.medicine, 'inventory', None)
            meds.append({
                'id': sm.medicine.id,
                'name': str(sm.medicine),
                'unit_price': str(sm.unit_price),
                'moq': sm.minimum_order_quantity,
                'delivery_days': sm.delivery_days,
                'current_stock': inv.current_stock if inv else 0,
                'reorder_level': inv.reorder_level if inv else 0,
            })
        return JsonResponse({'medicines': meds})
    except Supplier.DoesNotExist:
        return JsonResponse({'medicines': []})
