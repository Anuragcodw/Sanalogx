from django.urls import path
from . import views

urlpatterns = [
    # ── Sales POS & Cart Workflows ──
    path('sales/new/', views.new_sale_view, name='new_sale'),
    path('sales/history/', views.sales_history_view, name='sales_history'),
    path('sales/<int:pk>/', views.sale_detail_view, name='sale_detail'),
    path('sales/<int:pk>/invoice/', views.sale_invoice_view, name='sale_invoice'),
    path('sales/<int:pk>/return/', views.sale_return_view, name='sale_return'),
    path('sales/<int:pk>/cancel/', views.cancel_sale_view, name='sale_cancel'),
    path('sales/analytics/', views.sales_analytics_view, name='sales_analytics'),

    # ── AJAX APIs ──
    path('api/sales/barcode-scan/', views.barcode_scan_sale_api, name='api_sales_barcode_scan'),
    path('api/sales/checkout/', views.checkout_sale_api, name='api_sales_checkout'),
    path('api/sales/financial-summary/', views.financial_summary_api, name='api_financial_summary'),
]
