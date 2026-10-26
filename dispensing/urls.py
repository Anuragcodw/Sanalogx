from django.urls import path
from . import views

urlpatterns = [
    # ── Pharmacist Module: QR Display ──
    path('inventory/qr/', views.qr_display_view, name='pharmacy_qr_display'),

    # ── Public Patient Mobile Medicine Request ──
    path('medicine-request/<str:token>/', views.mobile_medicine_request_view, name='mobile_medicine_request'),
    path('medicine-request/success/<str:request_id>/', views.patient_request_success_view, name='patient_request_success'),
    path('ajax/categories/medicines/', views.ajax_get_medicines_by_category, name='ajax_get_medicines_by_category'),

    # ── Pharmacist Live Queue & Legacy Processing ──
    path('inventory/requests/', views.live_requests_queue_view, name='live_requests_queue'),
    path('inventory/request/<str:request_id>/', views.request_detail_view, name='request_detail'),
    path('inventory/dispense/direct/', views.direct_dispensing_view, name='direct_dispensing'),
    path('inventory/invoice/<str:request_id>/', views.view_invoice_view, name='view_invoice'),
    path('inventory/prescriptions/', views.prescription_list_view, name='prescription_list'),
    path('inventory/prescription/new/', views.create_prescription_view, name='create_prescription'),
    path('inventory/returns/', views.record_return_view, name='record_return'),

    # ── Enterprise Secure Dispensing & Traceability Engine ──
    path('dispensing/workspace/', views.secure_workspace_view, name='secure_dispensing_workspace'),
    path('dispensing/history/', views.dispensing_history_view, name='dispensing_history'),
    path('dispensing/history/<str:dispensing_id>/', views.dispensing_detail_view, name='dispensing_detail'),
    path('dispensing/controlled-register/', views.controlled_register_view, name='controlled_register'),
    path('dispensing/discrepancies/', views.discrepancy_list_view, name='discrepancy_list'),
    path('dispensing/discrepancies/<str:discrepancy_id>/resolve/', views.discrepancy_resolve_view, name='discrepancy_resolve'),
    path('dispensing/reconciliation/', views.stock_reconciliation_view, name='stock_reconciliation'),

    # ── AJAX Dispensing & Barcode APIs ──
    path('api/dispensing/barcode-scan/', views.barcode_scan_api, name='api_barcode_scan'),
    path('api/dispensing/dispense-item/', views.dispense_item_api, name='api_dispense_item'),
    path('api/dispensing/reject-item/', views.reject_request_item_api, name='api_reject_item'),
    path('api/dispensing/confirm-payment/', views.confirm_payment_api, name='api_confirm_payment'),

    # ── Secure Dispensing Enterprise APIs ──
    path('api/dispensing/secure-scan/', views.api_secure_barcode_scan, name='api_secure_barcode_scan'),
    path('api/dispensing/secure-dispense/', views.api_secure_dispense, name='api_secure_dispense'),
    path('api/dispensing/reverse/', views.api_reverse_dispense, name='api_reverse_dispense'),
    path('api/dispensing/verify-prescription/', views.api_verify_prescription, name='api_verify_prescription'),
    path('api/dispensing/reject-prescription/', views.api_reject_prescription, name='api_reject_prescription'),
    path('api/dispensing/medicine-search/', views.api_medicine_search, name='api_medicine_search'),
    path('api/dispensing/batch-preview/', views.api_batch_preview, name='api_batch_preview'),
    path('api/dispensing/dashboard-stats/', views.api_dispensing_dashboard_stats, name='api_dispensing_dashboard_stats'),
    path('api/dispensing/patient-search/', views.api_patient_search, name='api_patient_search'),
    path('api/dispensing/register-patient/', views.api_register_patient, name='api_register_patient'),
    path('api/dispensing/doctor-search/', views.api_doctor_search, name='api_doctor_search'),
    path('api/dispensing/create-prescription/', views.api_create_prescription, name='api_create_prescription'),
]
