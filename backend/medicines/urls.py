from django.urls import path
from . import views

urlpatterns = [
    # Category Management
    path('categories/', views.category_list_view, name='category_list'),

    # Medicine Duplication API
    path('check-duplicate/', views.medicine_duplicate_check_view, name='medicine_duplicate_check'),

    # CSV / Excel Import Workflow (Admin-only)
    # These MUST come before <str:medicine_id>/ to avoid the catch-all swallowing 'import/'
    path('import/', views.medicine_import_upload_view, name='medicine_import_upload'),
    path('import/preview/', views.medicine_import_preview_view, name='medicine_import_preview'),
    path('import/confirm/', views.medicine_import_confirm_view, name='medicine_import_confirm'),
    path('import/logs/', views.medicine_import_log_view, name='medicine_import_log'),
    path('import/logs/<int:log_id>/', views.medicine_import_log_detail_view, name='medicine_import_log_detail'),
    path('import/logs/<int:log_id>/errors/', views.medicine_import_error_report_view, name='medicine_import_error_report'),

    # Draft Review Workflow (Admin-only)
    # These MUST come before <str:medicine_id>/ to avoid the catch-all swallowing 'drafts/'
    path('drafts/', views.medicine_draft_list_view, name='medicine_draft_list'),
    path('drafts/<str:medicine_id>/review/', views.medicine_draft_review_view, name='medicine_draft_review'),
    path('<str:medicine_id>/review/', views.medicine_draft_review_view, name='medicine_review'),

    # Medicine CRUD & Validation
    path('', views.medicine_list_view, name='medicine_list'),
    path('add/', views.medicine_add_view, name='medicine_add'),
    path('<str:medicine_id>/', views.medicine_detail_view, name='medicine_detail'),
    path('<str:medicine_id>/edit/', views.medicine_edit_view, name='medicine_edit'),
    path('<str:medicine_id>/deactivate/', views.medicine_deactivate_view, name='medicine_deactivate'),
    path('<str:medicine_id>/validate/', views.medicine_validate_view, name='medicine_validate'),
    path('<str:medicine_id>/qr/', views.medicine_qr_view, name='medicine_qr'),

    # Batches
    path('<str:medicine_id>/batches/', views.batch_list_view, name='batch_list'),
    path('<str:medicine_id>/batches/add/', views.batch_add_view, name='batch_add'),
    path('<str:medicine_id>/batches/<str:batch_id>/edit/', views.batch_edit_view, name='batch_edit'),
]
