from django.urls import path
from . import views

urlpatterns = [
    path('suppliers/', views.supplier_list_view, name='supplier_list'),
    path('suppliers/add/', views.add_supplier_view, name='add_supplier'),
    path('suppliers/<int:pk>/edit/', views.edit_supplier_view, name='edit_supplier'),
    path('suppliers/<int:pk>/', views.supplier_detail_view, name='supplier_detail'),
    path('suppliers/<int:pk>/map/', views.supplier_medicine_map_view, name='supplier_medicine_map'),
    path('suppliers/price-comparison/', views.price_comparison_view, name='price_comparison'),
    path('suppliers/performance/', views.supplier_performance_view, name='supplier_performance'),
    path('api/suppliers/medicines/', views.ajax_supplier_medicines, name='ajax_supplier_medicines'),
]
