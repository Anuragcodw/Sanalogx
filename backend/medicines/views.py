import io
import json
from decimal import Decimal
from datetime import timedelta
import qrcode
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import HttpResponse, JsonResponse, Http404
from django.utils import timezone
from django.db.models import Q, Sum
from django.conf import settings

from .models import Medicine, MedicineBatch, Category, MedicineImportLog
from .forms import (
    MedicineForm, BatchForm, BatchEditForm, CategoryForm,
    MedicineValidationForm, MedicineDeactivateForm, MedicineReviewForm,
)
from inventory import services as inventory_services
from inventory.models import StockTransaction



# ── Role Guards ────────────────────────────────────────────────────────────

def admin_required(view_func):
    @login_required
    def _wrapped_view(request, *args, **kwargs):
        if request.user.role != 'ADMIN':
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return _wrapped_view

def pharmacist_or_admin_required(view_func):
    @login_required
    def _wrapped_view(request, *args, **kwargs):
        user = request.user
        if user.role == 'ADMIN':
            return view_func(request, *args, **kwargs)
        if user.role == 'PHARMACIST' and user.status == 'APPROVED':
            return view_func(request, *args, **kwargs)
        # PENDING or REJECTED pharmacist — redirect to their status page
        from django.contrib import messages as _msgs
        _msgs.error(request, "Your account must be approved before accessing this page.")
        if user.role == 'PHARMACIST' and user.status == 'PENDING':
            return redirect('pending')
        raise PermissionDenied
    return _wrapped_view


# ── Medicine Views ──────────────────────────────────────────────────────────

@pharmacist_or_admin_required
def medicine_list_view(request):
    """View and search medicines in the Medicine Master."""
    queryset = Medicine.objects.all().select_related('category')

    # Search
    search_query = request.GET.get('search', '').strip()
    if search_query:
        queryset = queryset.filter(
            Q(medicine_name__icontains=search_query) |
            Q(generic_name__icontains=search_query) |
            Q(brand_name__icontains=search_query) |
            Q(manufacturer__icontains=search_query) |
            Q(barcode__icontains=search_query) |
            Q(internal_barcode__icontains=search_query)
        )

    # Filters
    category_id = request.GET.get('category')
    if category_id:
        queryset = queryset.filter(category_id=category_id)

    dosage_form_id = request.GET.get('dosage_form', '').strip()
    if dosage_form_id:
        queryset = queryset.filter(dosage_form__icontains=dosage_form_id)

    manufacturer = request.GET.get('manufacturer', '').strip()
    if manufacturer:
        queryset = queryset.filter(manufacturer__iexact=manufacturer)

    validation_status = request.GET.get('validation_status', '').strip()
    if validation_status == 'VALIDATED':
        queryset = queryset.filter(status='ACTIVE', verification_status='VERIFIED')
    elif validation_status in ('PENDING', 'NEEDS_CORRECTION', 'PENDING_REVIEW'):
        queryset = queryset.filter(Q(status='PENDING_REVIEW') | Q(verification_status='PENDING_REVIEW') | Q(status='INACTIVE'))
    elif validation_status == 'REJECTED':
        queryset = queryset.filter(verification_status='REJECTED')

    requires_prescription = request.GET.get('requires_prescription', '').strip()
    if requires_prescription in ('yes', '1', 'true'):
        queryset = queryset.exclude(regulatory_category=Medicine.RegulatoryCategory.NORMAL)

    is_controlled = request.GET.get('is_controlled', '').strip()
    if is_controlled in ('yes', '1', 'true'):
        queryset = queryset.filter(regulatory_category__in=[
            Medicine.RegulatoryCategory.SCHEDULE_H1,
            Medicine.RegulatoryCategory.SCHEDULE_X,
            Medicine.RegulatoryCategory.NARCOTIC,
            Medicine.RegulatoryCategory.CONTROLLED_OTHER,
        ])

    status = request.GET.get('status', 'active')
    if not validation_status:
        if status == 'active':
            queryset = queryset.filter(status='ACTIVE')
        elif status == 'inactive':
            queryset = queryset.filter(status='INACTIVE')
        elif status in ('pending', 'draft'):
            queryset = queryset.filter(Q(status='PENDING_REVIEW') | Q(verification_status='PENDING_REVIEW'))
        elif status == 'all':
            pass

    # Sorting
    sort = request.GET.get('sort', 'name_asc')
    if sort == 'name_desc':
        queryset = queryset.order_by('-medicine_name')
    elif sort == 'recent_added':
        queryset = queryset.order_by('-created_at')
    elif sort == 'recent_updated':
        queryset = queryset.order_by('-updated_at')
    else:  # name_asc
        queryset = queryset.order_by('medicine_name')

    # Distinct lists for filters
    manufacturers = Medicine.objects.values_list('manufacturer', flat=True).distinct().order_by('manufacturer')
    manufacturers = [m for m in manufacturers if m]

    categories = Category.objects.filter(is_active=True).order_by('name')
    dosage_forms = Medicine.objects.values_list('dosage_form', flat=True).distinct().order_by('dosage_form')
    dosage_forms = [df for df in dosage_forms if df]

    # Dashboard metrics summary
    today = timezone.now().date()
    validated_count = Medicine.objects.filter(status='ACTIVE', verification_status='VERIFIED').count()
    pending_count = Medicine.objects.filter(Q(status='PENDING_REVIEW') | Q(verification_status='PENDING_REVIEW')).count()
    draft_count = Medicine.objects.filter(status='PENDING_REVIEW').count()
    near_expiry_batches = MedicineBatch.objects.filter(
        expiry_date__gt=today,
        expiry_date__lte=today + timedelta(days=90),
        quantity_available__gt=0
    ).count()

    try:
        from inventory.models import Inventory
        low_or_out_stock = Inventory.objects.filter(
            stock_status__in=[
                Inventory.StockStatus.LOW_STOCK,
                Inventory.StockStatus.CRITICAL,
                Inventory.StockStatus.OUT_OF_STOCK
            ]
        ).count()
    except Exception:
        low_or_out_stock = 0

    metrics = {
        'total': Medicine.objects.count(),
        'validated': validated_count,
        'pending': pending_count,
        'draft': draft_count,
        'active': Medicine.objects.filter(status='ACTIVE').count(),
        'inactive': Medicine.objects.filter(status='INACTIVE').count(),
        'total_batches': MedicineBatch.objects.count(),
        'near_expiry_batches': near_expiry_batches,
        'low_or_out_stock': low_or_out_stock,
        'pending_review': pending_count,
    }

    regulatory_category = request.GET.get('regulatory_category', '').strip()
    if regulatory_category:
        queryset = queryset.filter(regulatory_category=regulatory_category)

    # Pagination
    paginator = Paginator(queryset, 15)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        'page_obj': page_obj,
        'categories': categories,
        'dosage_forms': dosage_forms,
        'manufacturers': manufacturers,
        'regulatory_categories': Medicine.RegulatoryCategory.choices,
        'metrics': metrics,
        'selected_filters': {
            'search': search_query,
            'category': category_id,
            'dosage_form': dosage_form_id,
            'manufacturer': manufacturer,
            'regulatory_category': regulatory_category,
            'validation_status': validation_status,
            'requires_prescription': requires_prescription,
            'is_controlled': is_controlled,
            'status': status,
            'sort': sort,
        }
    }
    return render(request, 'medicines/medicine_list.html', context)


def _clean_medicine_pk(medicine_id):
    """Normalize medicine_id string into integer PK if prefixed with MED-."""
    if isinstance(medicine_id, str) and medicine_id.startswith('MED-'):
        try:
            return int(medicine_id.replace('MED-', ''))
        except ValueError:
            pass
    return medicine_id


def _clean_batch_pk(batch_id):
    """Normalize batch_id string into integer PK if prefixed with BAT-."""
    if isinstance(batch_id, str) and (batch_id.startswith('BAT-') or batch_id.startswith('BATCH-')):
        try:
            return int(batch_id.split('-')[-1])
        except ValueError:
            pass
    return batch_id


@pharmacist_or_admin_required
def medicine_detail_view(request, medicine_id):
    """View details of a single medicine, inventory, batches, suppliers, and procurement history."""
    clean_pk = _clean_medicine_pk(medicine_id)
    medicine = get_object_or_404(
        Medicine.objects.select_related('category'),
        pk=clean_pk
    )
    batches = medicine.batches.all().order_by('expiry_date')
    inventory = getattr(medicine, 'inventory', None)

    # Supplier catalogue for this medicine
    supplier_medicines = medicine.supplier_medicines.select_related('supplier').all()

    # Recent procurement order items
    try:
        from purchases.models import PurchaseOrderItem
        po_items = (
            PurchaseOrderItem.objects.filter(medicine=medicine)
            .select_related('purchase_order', 'purchase_order__supplier')
            .order_by('-created_at')[:6]
        )
    except Exception:
        po_items = []

    # Price history
    try:
        price_histories = (
            medicine.batch_price_histories.select_related('batch', 'changed_by')
            .order_by('-changed_at')[:6]
        )
    except Exception:
        price_histories = []

    validation_form = MedicineValidationForm(instance=medicine)
    deactivate_form = MedicineDeactivateForm()

    context = {
        'medicine': medicine,
        'batches': batches,
        'inventory': inventory,
        'supplier_medicines': supplier_medicines,
        'po_items': po_items,
        'price_histories': price_histories,
        'validation_form': validation_form,
        'deactivate_form': deactivate_form,
    }
    return render(request, 'medicines/medicine_detail.html', context)


@pharmacist_or_admin_required
def medicine_add_view(request):
    """Add a new medicine record to the Master."""
    if request.method == 'POST':
        form = MedicineForm(request.POST, request.FILES)
        if form.is_valid():
            medicine = form.save()
            messages.success(request, f"Medicine '{medicine.medicine_name}' added successfully.")
            return redirect('medicine_detail', medicine_id=medicine.id)
        else:
            messages.error(request, "Please correct the highlighted errors.")
    else:
        form = MedicineForm()

    return render(request, 'medicines/medicine_add.html', {'form': form})


@pharmacist_or_admin_required
def medicine_edit_view(request, medicine_id):
    """Edit an existing medicine."""
    medicine = get_object_or_404(Medicine, pk=_clean_medicine_pk(medicine_id))
    
    if request.method == 'POST':
        form = MedicineForm(request.POST, request.FILES, instance=medicine)
        if form.is_valid():
            edited_med = form.save()
            messages.success(request, f"Medicine '{edited_med.medicine_name}' updated successfully.")
            return redirect('medicine_detail', medicine_id=edited_med.id)
        else:
            messages.error(request, "Please correct the highlighted errors.")
    else:
        form = MedicineForm(instance=medicine)

    context = {
        'form': form,
        'medicine': medicine,
    }
    return render(request, 'medicines/medicine_edit.html', context)


@admin_required
def medicine_validate_view(request, medicine_id):
    """Validate a medicine record."""
    medicine = get_object_or_404(Medicine, pk=_clean_medicine_pk(medicine_id))
    if request.method == 'POST':
        form = MedicineValidationForm(request.POST, instance=medicine)
        if form.is_valid():
            validated_med = form.save()
            messages.success(request, f"Medicine status updated to: {validated_med.get_status_display()}")
            return redirect('medicine_detail', medicine_id=validated_med.id)
    return redirect('medicine_detail', medicine_id=medicine.id)


@admin_required
def medicine_deactivate_view(request, medicine_id):
    """Soft delete/deactivate a medicine."""
    medicine = get_object_or_404(Medicine, pk=_clean_medicine_pk(medicine_id))
    if request.method == 'POST':
        form = MedicineDeactivateForm(request.POST)
        if form.is_valid():
            if medicine.batches.exists():
                medicine.status = Medicine.Status.INACTIVE
                medicine.save()
                messages.warning(request, f"Medicine '{medicine.medicine_name}' has batches and was deactivated.")
            else:
                name = medicine.medicine_name
                medicine.delete()
                messages.success(request, f"Medicine '{name}' has been permanently deleted.")
                return redirect('medicine_list')
            
            return redirect('medicine_detail', medicine_id=medicine.id)
    return redirect('medicine_detail', medicine_id=medicine.id)


@pharmacist_or_admin_required
def medicine_qr_view(request, medicine_id):
    """Generate and return a clean QR code identifying the medicine securely."""
    medicine = get_object_or_404(Medicine, pk=_clean_medicine_pk(medicine_id))
    qr_data = f"Sanalogx-Medicine:{medicine.id}"
    
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(qr_data)
    qr.make(fit=True)

    img = qr.make_image(fill_color="#0F2744", back_color="#FFFFFF")
    
    response = HttpResponse(content_type="image/png")
    img.save(response, "PNG")
    return response


# ── Batch Views ────────────────────────────────────────────────────────────

@pharmacist_or_admin_required
def batch_list_view(request, medicine_id):
    """Display all batches for a single medicine."""
    medicine = get_object_or_404(Medicine, pk=_clean_medicine_pk(medicine_id))
    batches = medicine.batches.all().order_by('expiry_date')
    return render(request, 'medicines/batch_list.html', {'medicine': medicine, 'batches': batches})


@pharmacist_or_admin_required
def batch_add_view(request, medicine_id):
    """Add a new batch to a medicine."""
    medicine = get_object_or_404(Medicine, pk=_clean_medicine_pk(medicine_id))

    if request.method == 'POST':
        form = BatchForm(request.POST)
        if form.is_valid():
            batch = form.save(commit=False)
            batch.medicine = medicine
            # Do NOT set quantity_available here — add_stock() will set it
            batch.quantity_available = 0

            # Simple validation if batch is expired
            if batch.expiry_date < timezone.now().date():
                messages.error(request, "Cannot add an expired batch to active inventory.")
                return render(request, 'medicines/batch_add.html', {'form': form, 'medicine': medicine})

            batch.save()

            # Route through centralized StockService so a StockTransaction is created
            try:
                inventory_services.add_stock(
                    medicine=medicine,
                    batch=batch,
                    quantity=batch.quantity_received,
                    transaction_type=StockTransaction.TransactionType.PURCHASE,
                    performed_by=request.user,
                    reference_number=f"MANUAL-BATCH-{batch.batch_number}",
                    notes="Manual batch entry via Medicine Management",
                )
                messages.success(request, f"Batch '{batch.batch_number}' added successfully.")
            except Exception as exc:
                messages.error(request, f"Batch saved but stock update failed: {exc}")

            return redirect('medicine_detail', medicine_id=medicine.id)
        else:
            messages.error(request, "Please correct the highlighted errors.")
    else:
        form = BatchForm()

    return render(request, 'medicines/batch_add.html', {'form': form, 'medicine': medicine})


@pharmacist_or_admin_required
def batch_edit_view(request, medicine_id, batch_id):
    """
    Edit batch metadata (prices, storage condition, status, dates).
    quantity_received and quantity_available are intentionally excluded:
    - quantity_received is immutable historical receipt data.
    - quantity_available must only change through StockService (NEVER direct save).
    Use the Stock Adjustment workflow for physical count corrections.
    """
    clean_med_pk = _clean_medicine_pk(medicine_id)
    clean_bat_pk = _clean_batch_pk(batch_id)
    medicine = get_object_or_404(Medicine, pk=clean_med_pk)
    batch = get_object_or_404(MedicineBatch, medicine=medicine, pk=clean_bat_pk)

    if request.method == 'POST':
        old_purchase = batch.purchase_price
        old_selling = batch.selling_price
        old_mrp = getattr(batch, 'mrp', Decimal('0.00')) or Decimal('0.00')
        old_unit_cost = getattr(batch, 'unit_cost', Decimal('0.00')) or Decimal('0.00')

        form = BatchEditForm(request.POST, instance=batch)
        if form.is_valid():
            updated_batch = form.save()
            # If prices changed, record history
            if (old_purchase != updated_batch.purchase_price or
                old_selling != updated_batch.selling_price or
                old_mrp != updated_batch.mrp):
                from .models import BatchPriceHistory
                reason = request.POST.get('price_change_reason', 'Manual batch price adjustment').strip() or 'Manual batch price adjustment'
                BatchPriceHistory.objects.create(
                    batch=updated_batch,
                    medicine=medicine,
                    old_purchase_price=old_purchase,
                    new_purchase_price=updated_batch.purchase_price,
                    old_selling_price=old_selling,
                    new_selling_price=updated_batch.selling_price,
                    old_mrp=old_mrp,
                    new_mrp=updated_batch.mrp,
                    old_unit_cost=old_unit_cost,
                    new_unit_cost=updated_batch.unit_cost,
                    changed_by=request.user,
                    reason=reason
                )
            messages.success(request, f"Batch '{batch.batch_number}' updated successfully.")
            return redirect('medicine_detail', medicine_id=medicine.id)
        else:
            messages.error(request, "Please correct the highlighted errors.")
    else:
        form = BatchEditForm(instance=batch)

    context = {
        'form': form,
        'medicine': medicine,
        'batch': batch,
    }
    return render(request, 'medicines/batch_edit.html', context)


# ── Category Management ─────────────────────────────────────────────────────

@admin_required
def category_list_view(request):
    """List and manage Category Master list."""
    categories = Category.objects.all().order_by('name')
    if request.method == 'POST':
        form = CategoryForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Category added successfully.")
            return redirect('category_list')
        else:
            messages.error(request, "Could not add category. Check for duplicate names.")
    else:
        form = CategoryForm()

    return render(request, 'medicines/category_list.html', {'categories': categories, 'form': form})


# ── Smart Form Behaviors & Duplication API ─────────────────────────────────

@pharmacist_or_admin_required
def medicine_duplicate_check_view(request):
    """AJAX endpoint to verify if a similar medicine exists."""
    generic_name = request.GET.get('generic_name', '').strip()
    strength = request.GET.get('strength', '').strip()
    dosage_form = request.GET.get('dosage_form', '').strip()
    brand_name = request.GET.get('brand_name', '').strip()
    manufacturer = request.GET.get('manufacturer', '').strip()

    if not (generic_name and strength and dosage_form):
        return JsonResponse({'exists': False})

    duplicates = Medicine.objects.filter(
        generic_name__iexact=generic_name,
        strength__iexact=strength,
        dosage_form__iexact=dosage_form
    )

    if brand_name:
        duplicates = duplicates.filter(brand_name__iexact=brand_name)
    if manufacturer:
        duplicates = duplicates.filter(manufacturer__iexact=manufacturer)

    if duplicates.exists():
        match = duplicates.first()
        return JsonResponse({
            'exists': True,
            'medicine_name': match.medicine_name,
            'medicine_id': match.id,
            'detail_url': f"/medicines/{match.id}/"
        })

    return JsonResponse({'exists': False})


# ── CSV / Excel Import Workflow ─────────────────────────────────────────────

@admin_required
def medicine_import_upload_view(request):
    """
    Step 1 of import workflow: file upload + sheet selection.
    Accepts CSV or XLSX. Parses file, stores preview in session.
    """
    from .import_service import (
        parse_import_file, validate_and_preview_categories,
        validate_and_preview_medicines, classify_sheet, _read_file_sha256,
        KNOWN_SHEETS,
    )

    if request.method == 'POST':
        uploaded_file = request.FILES.get('import_file')
        if not uploaded_file:
            messages.error(request, "No file uploaded. Please choose a CSV or Excel file.")
            return redirect('medicine_import_upload')

        filename = uploaded_file.name
        if not (filename.lower().endswith('.csv') or filename.lower().endswith(('.xlsx', '.xls'))):
            messages.error(request, "Unsupported format. Please upload a .csv or .xlsx file.")
            return redirect('medicine_import_upload')

        try:
            sha256 = _read_file_sha256(uploaded_file)
            sheet_map = parse_import_file(uploaded_file, filename)
        except ValidationError as exc:
            messages.error(request, str(exc))
            return redirect('medicine_import_upload')
        except Exception as exc:
            messages.error(request, f"File could not be parsed: {exc}")
            return redirect('medicine_import_upload')

        # Classify each sheet
        previews = {}
        for sheet_name, rows in sheet_map.items():
            import_type = classify_sheet(sheet_name)
            if not rows:
                continue
            if import_type == MedicineImportLog.ImportType.CATEGORY_MASTER:
                previews[sheet_name] = {
                    'import_type': import_type,
                    'data': validate_and_preview_categories(rows),
                }
            elif import_type in (
                MedicineImportLog.ImportType.MEDICINE_IMPORT,
                MedicineImportLog.ImportType.STARTER_EXAMPLES,
            ):
                previews[sheet_name] = {
                    'import_type': import_type,
                    'data': validate_and_preview_medicines(rows, sheet_name),
                }
            elif sheet_name == 'default':
                # Auto-detect: single CSV — try as medicine import
                if any('generic_name' in r or 'name' in r or 'category_name' in r for r in rows):
                    if any('category_name' in r or ('name' in r and 'dosage_form' not in r) for r in rows):
                        previews[sheet_name] = {
                            'import_type': MedicineImportLog.ImportType.CATEGORY_MASTER,
                            'data': validate_and_preview_categories(rows),
                        }
                    else:
                        previews[sheet_name] = {
                            'import_type': MedicineImportLog.ImportType.MEDICINE_IMPORT,
                            'data': validate_and_preview_medicines(rows, sheet_name),
                        }
            else:
                # Unknown sheet — skip silently (e.g., Instructions, Notes)
                continue

        if not previews:
            messages.warning(request, "No recognised sheets found (expected: Category_Master, Medicine_Import, or Starter_Examples).")
            return redirect('medicine_import_upload')

        # Store preview in session (serialisable)
        request.session['medicine_import_previews'] = json.dumps(previews, default=str)
        request.session['medicine_import_filename'] = filename
        request.session['medicine_import_sha256'] = sha256

        return redirect('medicine_import_preview')

    return render(request, 'medicines/import_upload.html', {
        'recent_logs': MedicineImportLog.objects.all()[:10],
    })


@admin_required
def medicine_import_preview_view(request):
    """
    Step 2: display per-sheet preview tables with row-level errors.
    Admin confirms which sheets to import.
    """
    raw = request.session.get('medicine_import_previews')
    if not raw:
        messages.warning(request, "No import session found. Please upload a file first.")
        return redirect('medicine_import_upload')

    previews = json.loads(raw)
    filename = request.session.get('medicine_import_filename', 'unknown')

    return render(request, 'medicines/import_preview.html', {
        'previews': previews,
        'filename': filename,
    })


@admin_required
def medicine_import_confirm_view(request):
    """
    Step 3: execute the approved import sheets.
    POST with 'sheets' list (sheet names to import).
    """
    from .import_service import (
        execute_category_import, execute_medicine_import,
        validate_and_preview_categories, validate_and_preview_medicines, classify_sheet,
    )

    if request.method != 'POST':
        return redirect('medicine_import_preview')

    raw = request.session.get('medicine_import_previews')
    if not raw:
        messages.error(request, "Import session expired. Please re-upload the file.")
        return redirect('medicine_import_upload')

    previews = json.loads(raw)
    filename = request.session.get('medicine_import_filename', 'unknown')
    sha256 = request.session.get('medicine_import_sha256', '')
    sheets_to_import = request.POST.getlist('sheets')

    if not sheets_to_import:
        messages.warning(request, "No sheets selected. Please tick at least one sheet to import.")
        return redirect('medicine_import_preview')

    logs = []
    for sheet_name in sheets_to_import:
        if sheet_name not in previews:
            continue
        sheet = previews[sheet_name]
        import_type = sheet['import_type']
        preview_data = sheet['data']

        try:
            if import_type == MedicineImportLog.ImportType.CATEGORY_MASTER:
                log = execute_category_import(
                    preview_data=preview_data,
                    uploaded_by=request.user,
                    filename=filename,
                    file_sha256=sha256,
                )
            else:
                log = execute_medicine_import(
                    preview_data=preview_data,
                    uploaded_by=request.user,
                    filename=filename,
                    import_type=import_type,
                    file_sha256=sha256,
                )
            logs.append(log)
        except Exception as exc:
            messages.error(request, f"Import failed for sheet '{sheet_name}': {exc}")

    # Clear session
    for key in ('medicine_import_previews', 'medicine_import_filename', 'medicine_import_sha256'):
        request.session.pop(key, None)

    if logs:
        total_created = sum(l.created_count for l in logs)
        total_errors = sum(l.error_count for l in logs)
        messages.success(
            request,
            f"Import complete: {total_created} records created/updated. "
            f"{total_errors} row errors. See Import Logs for details."
        )
        return redirect('medicine_import_log_detail', log_id=logs[0].id)

    return redirect('medicine_import_log')


@admin_required
def medicine_import_log_view(request):
    """List all past import logs."""
    logs = MedicineImportLog.objects.select_related('uploaded_by').order_by('-created_at')
    paginator = Paginator(logs, 20)
    page_obj = paginator.get_page(request.GET.get('page'))
    return render(request, 'medicines/import_log.html', {'page_obj': page_obj})


@admin_required
def medicine_import_log_detail_view(request, log_id):
    """Detail view for a single import log with row errors."""
    log = get_object_or_404(MedicineImportLog, pk=log_id)
    return render(request, 'medicines/import_log_detail.html', {'log': log})


@admin_required
def medicine_import_error_report_view(request, log_id):
    """Download row-level errors from an import log as CSV."""
    from .import_service import build_error_report_csv
    log = get_object_or_404(MedicineImportLog, pk=log_id)
    csv_content = build_error_report_csv(log)
    response = HttpResponse(csv_content, content_type='text/csv')
    safe_name = log.filename.replace(' ', '_')
    response['Content-Disposition'] = f'attachment; filename="error_report_{safe_name}.csv"'
    return response


# ── Draft Medicine Review Workflow ──────────────────────────────────────────

@admin_required
def medicine_draft_list_view(request):
    """List all PENDING_REVIEW (draft) medicines awaiting pharmacy review."""
    drafts = Medicine.objects.filter(
        verification_status=Medicine.VerificationStatus.PENDING_REVIEW
    ).select_related('category').order_by('-created_at')
    paginator = Paginator(drafts, 20)
    page_obj = paginator.get_page(request.GET.get('page'))
    return render(request, 'medicines/draft_review_list.html', {
        'page_obj': page_obj,
        'total_drafts': drafts.count(),
    })


@admin_required
def medicine_draft_review_view(request, medicine_id):
    """
    Review and approve/reject a PENDING_REVIEW (draft) medicine.
    Approve: sets verification_status=VERIFIED, status=ACTIVE.
    Reject: sets verification_status=REJECTED, status=INACTIVE.
    """
    medicine = get_object_or_404(Medicine, pk=_clean_medicine_pk(medicine_id))

    action = request.POST.get('action') if request.method == 'POST' else None

    if action == 'approve':
        form = MedicineReviewForm(request.POST, instance=medicine)
        if form.is_valid():
            med = form.save(commit=False)
            med.verification_status = Medicine.VerificationStatus.VERIFIED
            med.status = Medicine.Status.ACTIVE
            med.save()
            try:
                from accounts.models import log_audit
                log_audit(
                    user=None,
                    action='MEDICINE_APPROVED',
                    description=f"Draft medicine '{med.medicine_name}' (ID: {med.id}) approved into active catalogue by {request.user}.",
                    performed_by=request.user,
                )
            except Exception:
                pass
            messages.success(request, f"'{medicine.medicine_name}' approved and added to the active catalogue.")
            return redirect('medicine_draft_list')
        else:
            messages.error(request, "Please correct the highlighted errors before approving.")
    elif action == 'reject':
        medicine.verification_status = Medicine.VerificationStatus.REJECTED
        medicine.status = Medicine.Status.INACTIVE
        rejection_note = request.POST.get('rejection_note', '').strip()
        if rejection_note:
            medicine.review_note = (medicine.review_note + f"\n[REJECTED: {rejection_note}]").strip()
        medicine.save()
        try:
            from accounts.models import log_audit
            log_audit(
                user=None,
                action='MEDICINE_REJECTED',
                description=f"Draft medicine '{medicine.medicine_name}' (ID: {medicine.id}) rejected: {rejection_note}",
                performed_by=request.user,
            )
        except Exception:
            pass
        messages.warning(request, f"'{medicine.medicine_name}' has been rejected.")
        return redirect('medicine_draft_list')
    else:
        form = MedicineReviewForm(instance=medicine)

    return render(request, 'medicines/draft_review.html', {
        'medicine': medicine,
        'form': form,
    })


# ── Medicine Barcode Registry Views (Phase 1) ────────────────────────────────

@pharmacist_or_admin_required
def medicine_barcode_registry_view(request):
    """
    Medicine Barcode Registry View:
    - Lists all medicines with their unique Code 128 internal barcode (SAN-MED-000123).
    - Supports search by name, generic name, strength, internal barcode, and manufacturer barcode.
    - Supports category and dosage form filtering.
    - Generates live SVG barcode preview for table display.
    - Provides View Barcode, Download PNG, Download PDF, and Print actions.
    - Pagination support with state preservation.
    """
    from . import barcode_service

    queryset = Medicine.objects.all().select_related('category').order_by('pk')

    # Search filter
    q = request.GET.get('q', '').strip()
    if q:
        queryset = queryset.filter(
            Q(medicine_name__icontains=q) |
            Q(generic_name__icontains=q) |
            Q(brand_name__icontains=q) |
            Q(strength__icontains=q) |
            Q(internal_barcode__icontains=q) |
            Q(barcode__icontains=q)
        )

    # Category filter
    category_id = request.GET.get('category', '').strip()
    if category_id:
        queryset = queryset.filter(category_id=category_id)

    # Dosage form filter
    dosage_form = request.GET.get('dosage_form', '').strip()
    if dosage_form:
        queryset = queryset.filter(dosage_form__iexact=dosage_form)

    # Status filter
    status = request.GET.get('status', '').strip()
    if status and status != 'ALL':
        queryset = queryset.filter(status=status)

    # Metrics
    total_meds = Medicine.objects.count()
    with_internal = Medicine.objects.exclude(internal_barcode__isnull=True).exclude(internal_barcode='').count()
    with_mfr = Medicine.objects.exclude(barcode__isnull=True).exclude(barcode='').count()
    categories_count = Category.objects.count()

    # Pagination: 20 per page
    paginator = Paginator(queryset, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # Attach SVG previews to each medicine on current page
    for med in page_obj:
        if not med.internal_barcode:
            barcode_service.get_or_create_internal_barcode(med)
        med.barcode_svg = barcode_service.generate_barcode_svg(med.internal_barcode)

    categories = Category.objects.all().order_by('name')
    dosage_forms = (
        Medicine.objects.values_list('dosage_form', flat=True)
        .exclude(dosage_form__isnull=True)
        .exclude(dosage_form='')
        .distinct()
        .order_by('dosage_form')
    )

    context = {
        'page_obj': page_obj,
        'medicines': page_obj,
        'search_query': q,
        'selected_category': category_id,
        'selected_dosage': dosage_form,
        'selected_status': status,
        'categories': categories,
        'dosage_forms': dosage_forms,
        'total_count': paginator.count,
        'metrics': {
            'total': total_meds,
            'with_internal': with_internal,
            'with_mfr': with_mfr,
            'categories': categories_count,
        }
    }
    return render(request, 'medicines/barcode_registry.html', context)


@pharmacist_or_admin_required
def medicine_barcode_preview_api(request, pk):
    """
    JSON API for the 'View Barcode' modal.
    Returns medicine metadata, internal barcode identifier, and SVG data.
    """
    from . import barcode_service
    medicine = get_object_or_404(Medicine, pk=pk)
    code = barcode_service.get_or_create_internal_barcode(medicine)
    svg_data = barcode_service.generate_barcode_svg(code)

    return JsonResponse({
        'success': True,
        'id': medicine.id,
        'medicine_name': medicine.medicine_name,
        'generic_name': medicine.generic_name or '',
        'strength': medicine.strength or '',
        'dosage_form': medicine.dosage_form or '',
        'category': medicine.category.name if medicine.category else '',
        'internal_barcode': code,
        'manufacturer_barcode': medicine.barcode or '',
        'barcode_svg': svg_data,
        'png_url': f"/medicines/barcodes/{medicine.pk}/png/",
        'pdf_url': f"/medicines/barcodes/{medicine.pk}/pdf/",
        'print_url': f"/medicines/barcodes/{medicine.pk}/print/",
    })


@pharmacist_or_admin_required
def medicine_barcode_png_view(request, pk):
    """
    Downloads high-resolution (300 DPI) Code 128 PNG barcode image.
    Filename: SAN-MED-000123.png
    """
    from . import barcode_service
    medicine = get_object_or_404(Medicine, pk=pk)
    code = barcode_service.get_or_create_internal_barcode(medicine)
    png_bytes = barcode_service.generate_barcode_png_bytes(code)

    response = HttpResponse(png_bytes, content_type='image/png')
    response['Content-Disposition'] = f'attachment; filename="{code}.png"'
    return response


@pharmacist_or_admin_required
def medicine_barcode_pdf_view(request, pk):
    """
    Downloads printable hospital label PDF (3.5" x 2.0").
    Filename: SAN-MED-000123_label.pdf
    """
    from . import barcode_service
    medicine = get_object_or_404(Medicine, pk=pk)
    code = barcode_service.get_or_create_internal_barcode(medicine)
    pdf_bytes = barcode_service.generate_barcode_label_pdf_bytes(medicine)

    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{code}_label.pdf"'
    return response


@pharmacist_or_admin_required
def medicine_barcode_print_view(request, pk):
    """
    Displays a print-ready standalone label template formatted for
    standard thermal barcode printer or standard paper.
    Auto-triggers window.print() if ?autoprint=1 is passed.
    """
    from . import barcode_service
    medicine = get_object_or_404(Medicine, pk=pk)
    code = barcode_service.get_or_create_internal_barcode(medicine)
    svg_data = barcode_service.generate_barcode_svg(code)

    context = {
        'medicine': medicine,
        'code': code,
        'barcode_svg': svg_data,
        'autoprint': request.GET.get('autoprint', '1') == '1',
    }
    return render(request, 'medicines/barcode_print.html', context)


