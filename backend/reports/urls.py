from django.urls import path
from . import views

urlpatterns = [
    # Management Dashboard
    path('reports/dashboard/', views.management_dashboard_view, name='management_dashboard'),

    # Report Center & Workflow
    path('reports/', views.reports_center_view, name='reports_center'),
    path('reports/preview/', views.report_preview_view, name='report_preview'),
    path('reports/history/', views.report_history_view, name='report_history'),

    # Download (PDF / Excel / CSV)
    path('reports/download/<str:report_id>/<str:fmt>/', views.download_report_view, name='download_report'),

    # Email Report
    path('reports/email/<str:report_id>/', views.email_report_view, name='email_report'),

    # QR Verification (Public — no login required)
    path('reports/verify/<str:token>/', views.report_verify_view, name='report_verify'),

    # Legacy reports dashboard → redirect
    path('reports/legacy/', views.reports_dashboard_view, name='reports_dashboard'),
]
