from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [
    # ── Home ──────────────────────────────────────────────────────────────
    path('', views.home_view, name='home'),

    path('auth/login/', views.pharmacist_login_view, name='pharmacist_login'),
    path('auth/register/', views.pharmacist_register_view, name='pharmacist_register'),
    path('login/', views.pharmacist_login_view, name='login'),
    path('register/', views.pharmacist_register_view, name='register'),
    path('auth/logout/', views.logout_view, name='logout'),
    path('auth/change-password/', views.change_password_view, name='change_password'),
    path('auth/setup-password/<str:uidb64>/<str:token>/', views.setup_password_view, name='setup_password'),

    # ── Email Verification ─────────────────────────────────────────────────
    path('auth/verify-email-pending/', views.verify_email_pending_view, name='verify_email_pending'),
    path('auth/verify-email/<str:uidb64>/<str:token>/', views.verify_email_view, name='verify_email'),
    path('auth/resend-verification/', views.resend_verification_view, name='resend_verification'),
    path('auth/registration-success/', views.registration_success_view, name='registration_success'),

    # ── Registration Status Pages ──────────────────────────────────────────
    path('auth/pending/', views.pending_view, name='pending'),
    path('auth/approved/', views.approved_view, name='approved'),
    path('auth/rejected/', views.rejected_view, name='rejected'),
    path('auth/suspended/', views.suspended_view, name='suspended'),

    # ── Forgot Password Verification Code System ──
    path('auth/forgot-password/', views.forgot_password_view, name='forgot_password'),
    path('auth/forgot-password/verify/', views.verify_reset_code_view, name='verify_reset_code'),
    path('auth/forgot-password/new-password/', views.create_new_password_view, name='create_new_password'),

    # ── Django Built-in Password Reset fallback links ──
    path('auth/forgot-password/done/', auth_views.PasswordResetDoneView.as_view(
        template_name='registration/password_reset_done.html',
    ), name='password_reset_done'),
    path('auth/reset/<uidb64>/<token>/', auth_views.PasswordResetConfirmView.as_view(
        template_name='registration/password_reset_confirm.html',
        success_url='/auth/reset/complete/',
    ), name='password_reset_confirm'),
    path('auth/reset/complete/', auth_views.PasswordResetCompleteView.as_view(
        template_name='registration/password_reset_complete.html',
    ), name='password_reset_complete'),
    path('auth/password-reset/', views.forgot_password_view, name='password_reset'),

    # ── Username Recovery System ───────────────────────────────────────────
    path('auth/forgot-username/', views.forgot_username_view, name='forgot_username'),

    # ── Role-based Dashboard Direct Routes ──
    path('admin-dashboard/', views.admin_dashboard_redirect_view, name='admin_dashboard'),
    path('pharmacist-dashboard/', views.pharmacist_dashboard_redirect_view, name='pharmacist_dashboard'),

    # ── Admin Auth & Approval & Username Recovery Actions ──────────────────
    path('auth/login-success/', views.login_success_view, name='login_success'),
    path('auth/admin-login/', views.admin_login_view, name='admin_login'),
    path('auth/admin/approvals/', views.admin_approvals_view, name='admin_approvals'),
    path('auth/admin/approve/<int:user_id>/', views.approve_pharmacist_view, name='approve_pharmacist'),
    path('auth/admin/pharmacist/<int:user_id>/resend-email/', views.resend_approval_email_view, name='resend_approval_email'),
    path('auth/admin/reject/<int:user_id>/', views.reject_pharmacist_view, name='reject_pharmacist'),
    path('auth/admin/pharmacist/<int:user_id>/', views.pharmacist_detail_view, name='pharmacist_detail'),
    path('auth/admin/username-recovery/<int:req_id>/approve/', views.approve_username_recovery_view, name='approve_username_recovery'),
    path('auth/admin/username-recovery/<int:req_id>/reject/', views.reject_username_recovery_view, name='reject_username_recovery'),

    # ── Error Pages ────────────────────────────────────────────────────────
    path('auth/403/', views.unauthorized_403_view, name='unauthorized_403'),
    path('auth/404/', views.not_found_404_view, name='not_found_404'),
]
