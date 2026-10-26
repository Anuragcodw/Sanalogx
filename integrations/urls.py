from django.urls import path
from . import views

urlpatterns = [
    path('', views.integration_dashboard_view, name='integration_dashboard'),
    path('dashboard/', views.integration_dashboard_view, name='integration_dashboard_alt'),
    path('orders/', views.integration_orders_view, name='integration_orders'),
    path('orders/<int:order_id>/review/', views.order_review_view, name='order_review'),
    path('mock-simulator/', views.mock_ehr_simulator_view, name='mock_ehr_simulator'),
]
