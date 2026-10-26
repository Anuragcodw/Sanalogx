from django.urls import path
from . import views

urlpatterns = [
    path('', views.inventory_dashboard, name='inventory_dashboard'),
    path('batches/', views.batch_stock_view, name='batch_stock'),
    path('history/', views.stock_history_view, name='stock_history'),
    path('expiry/', views.expiry_management_view, name='expiry_management'),
    path('medicine/<int:pk>/', views.medicine_inventory_detail, name='medicine_inventory_detail'),
    path('add-stock/', views.add_stock_view, name='add_stock'),
    path('adjust-stock/', views.adjust_stock_view, name='adjust_stock'),
    path('record-damage/', views.record_damage_view, name='record_damage'),
    # Consumption Analytics
    path('consumption/', views.consumption_analytics_view, name='inventory_consumption_analytics'),
    path('reorder-recommendations/', views.reorder_recommendations_view, name='inventory_reorder_recommendations'),
    # Historical import
    path('import-historical/', views.historical_import_view, name='inventory_historical_import'),
    # AJAX endpoints
    path('ajax/batches/', views.get_medicine_batches, name='get_medicine_batches'),
    path('ajax/medicine-consumption/<int:pk>/', views.medicine_consumption_api, name='medicine_consumption_api'),
    path('ajax/medicine-optimization/<int:pk>/', views.inventory_optimization_api, name='medicine_optimization_api'),
]
