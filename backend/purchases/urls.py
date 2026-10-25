from django.urls import path
from . import views

urlpatterns = [
    # ── Purchase Requests ──
    path('purchases/requests/', views.purchase_request_list_view, name='purchase_request_list'),
    path('purchases/request/new/', views.create_purchase_request_view, name='create_purchase_request'),
    path('purchases/request/<int:pk>/approve/', views.approve_purchase_request_view, name='approve_purchase_request'),

    # ── Purchase Orders ──
    path('purchases/', views.purchase_orders_list_view, name='purchase_orders_list'),
    path('purchases/po-list/', views.purchase_orders_list_view, name='po_list'),
    path('purchases/dashboard/', views.purchase_orders_list_view, name='purchases_dashboard'),
    path('purchases/create/', views.create_po_view, name='create_po'),
    path('purchases/<int:pk>/', views.po_detail_view, name='po_detail'),
    path('purchases/<int:pk>/approve/', views.approve_po_view, name='approve_po'),
    path('purchases/<int:pk>/supplier-confirmation/', views.confirm_supplier_order_view, name='confirm_supplier_order'),
    path('purchases/<int:pk>/cancel/', views.cancel_po_view, name='cancel_po'),

    # ── Goods Receiving (GRN) ──
    path('purchases/grn/', views.grn_list_view, name='grn_list'),
    path('purchases/po/<int:po_pk>/grn/new/', views.create_grn_view, name='create_grn'),
    path('purchases/grn/<int:pk>/', views.grn_detail_view, name='grn_detail'),
    path('purchases/grn/<int:pk>/verify/', views.verify_grn_view, name='verify_grn'),
    path('purchases/grn/barcode-lookup/', views.grn_barcode_lookup_api, name='grn_barcode_lookup'),

    # ── Delivery Tracking ──
    path('purchases/tracking/', views.delivery_tracking_view, name='delivery_tracking'),

    # ── PO Email Dispatch ──
    path('purchases/<int:pk>/send-email/', views.send_po_email_view, name='send_po_email'),
]
