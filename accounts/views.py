import re
import secrets
import string
import logging
from datetime import timedelta
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.contrib.auth import login, logout, authenticate, update_session_auth_hash
from django.contrib.auth.hashers import make_password, check_password
from django.contrib.auth.decorators import login_required
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.contrib import messages
from django.http import HttpResponseForbidden, JsonResponse
from django.db import transaction
from django.utils import timezone
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode, url_has_allowed_host_and_scheme
from django.utils.encoding import force_bytes, force_str

import phonenumbers

from .models import (
    User, EmailVerification, PharmacistProfile,
    PasswordResetRequest, UsernameRecoveryRequest, AuditLog, log_audit
)
from .tokens import (
    email_verification_token_generator,
    pharmacist_password_setup_token_generator,
)
from .email_service import (
    send_registration_email,
    send_verification_email,
    send_approval_email,
    send_approval_email_with_diagnostics,
    send_rejection_email,
    send_login_notification_email,
    send_password_changed_email,
    send_password_reset_code_email,
    send_password_reset_success_email,
    send_username_recovery_email,
)

logger = logging.getLogger(__name__)


def normalize_phone(phone_input, dial_code='', default_region='IN'):
    """
    Normalize a phone number to E.164 format using the phonenumbers library.

    Args:
        phone_input:    The national number typed by the user (may already contain '+' prefix).
        dial_code:      Dial code selected in the UI, e.g. '+91' (may be empty for legacy API calls).
        default_region: BCP-47 region code fallback when no dial code is supplied (default: India).

    Returns:
        (e164_string, None)  on success
        (None, error_message) on failure
    """
    phone_input = (phone_input or '').strip()
    if not phone_input:
        return None, 'Phone Number is required.'

    # Build the raw string to parse
    raw = phone_input
    if not raw.startswith('+') and dial_code:
        # Prepend the selected dial code from the frontend widget
        raw = dial_code.strip() + phone_input

    # Fix duplicate dial code like +91+919876543210 (defensive)
    m = re.match(r'^(\+\d{1,4})\1(.*)$', raw)
    if m:
        raw = m.group(1) + m.group(2)

    try:
        if raw.startswith('+'):
            parsed = phonenumbers.parse(raw, None)
        else:
            parsed = phonenumbers.parse(raw, default_region)

        if not phonenumbers.is_possible_number(parsed):
            reason = phonenumbers.is_possible_number_with_reason(parsed)
            if reason == phonenumbers.ValidationResult.TOO_SHORT:
                return None, 'Phone number is too short for the selected country.'
            if reason == phonenumbers.ValidationResult.TOO_LONG:
                return None, 'Phone number is too long for the selected country.'
            return None, 'Please enter a valid phone number.'

        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164), None

    except phonenumbers.NumberParseException:
        return None, 'Please enter a valid international phone number.'



# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_verification_url(request, user):
    """Build the absolute email verification URL for a user."""
    EmailVerification.objects.filter(user=user, is_verified=False).delete()
    
    verification = EmailVerification.objects.create(
        user=user,
        expires_at=timezone.now() + timedelta(hours=24)
    )
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    return request.build_absolute_uri(
        f"/auth/verify-email/{uid}/{verification.token}/"
    )


def _generate_registration_id(user):
    """Generate a unique, server-side Registration ID in the format MV-PH-YYYY-NNNNN."""
    year = timezone.now().year
    return f"MV-PH-{year}-{user.id:05d}"


def _generate_system_username():
    """Generate a unique system username in the format MEDI-PH-XXXX."""
    for _ in range(50):
        suffix = ''.join(secrets.choice(string.digits) for _ in range(4))
        candidate = f"MEDI-PH-{suffix}"
        if not User.objects.filter(username=candidate).exists():
            return candidate
    suffix = ''.join(secrets.choice(string.digits) for _ in range(6))
    return f"MEDI-PH-{suffix}"


def _generate_temp_password():
    """Generate a cryptographically strong temporary password."""
    upper = [secrets.choice(string.ascii_uppercase) for _ in range(3)]
    lower = [secrets.choice(string.ascii_lowercase) for _ in range(3)]
    digits = [secrets.choice(string.digits) for _ in range(2)]
    special = [secrets.choice('!@#$%^&*') for _ in range(2)]
    extra = [secrets.choice(string.ascii_letters + string.digits) for _ in range(2)]
    pool = upper + lower + digits + special + extra
    secrets.SystemRandom().shuffle(pool)
    return ''.join(pool)


# ---------------------------------------------------------------------------
# Public Views
# ---------------------------------------------------------------------------

def home_view(request):
    """Public Home Landing Page for MediVault"""
    return render(request, 'home/index.html')


def pharmacist_login_view(request):
    """
    Pharmacist Login View.
    Authenticate using Username or Email + Password.
    Checks Account Status: PENDING / REJECTED / APPROVED.
    First-login detection & generic invalid credentials handling.
    """
    if request.user.is_authenticated:
        if request.user.role == User.Role.ADMIN:
            return redirect('admin_approvals')
        elif request.user.must_change_password:
            return redirect('change_password')
        elif request.user.status == User.Status.PENDING:
            return redirect('pending')
        elif request.user.status == User.Status.REJECTED:
            return redirect('rejected')
        else:
            return redirect('approved')

    if request.method == 'POST':
        identifier = request.POST.get('identifier', '').strip()
        password = request.POST.get('password', '')

        if not identifier or not password:
            messages.error(request, "Invalid username or password.")
            return render(request, 'auth/login.html')

        target_user = None
        if '@' in identifier:
            try:
                target_user = User.objects.get(email__iexact=identifier)
            except User.DoesNotExist:
                target_user = None
        else:
            try:
                target_user = User.objects.get(username__iexact=identifier)
            except User.DoesNotExist:
                target_user = None

        if target_user is not None and target_user.check_password(password):
            # Email verification gate
            if not target_user.email_verified and target_user.role == User.Role.PHARMACIST:
                messages.warning(request, "Please verify your email address before logging in. Check your inbox for the verification link.")
                return render(request, 'auth/login.html')

            # Status gates
            if target_user.role == User.Role.PHARMACIST:
                if target_user.status == User.Status.PENDING:
                    messages.warning(request, "Your account is currently pending administrator approval.")
                    return redirect('pending')
                elif target_user.status == User.Status.REJECTED:
                    messages.error(request, "Your account is not approved. Please contact the administrator.")
                    return redirect('rejected')

            # Authenticate & log in
            user = authenticate(request, username=target_user.username, password=password)
            if user is not None:
                login(request, user)
                log_audit(user=user, action='LOGIN', description='User logged in successfully.')

                # Send login notification email (throttled per session)
                if not request.session.get('login_notification_sent'):
                    send_login_notification_email(user, request=request)
                    request.session['login_notification_sent'] = True

                # Track first login
                if not user.first_login_completed:
                    user.first_login_completed = True
                    user.save(update_fields=['first_login_completed'])
                    log_audit(user=user, action='FIRST_LOGIN', description='User completed first login.')
                    if user.role == User.Role.ADMIN or user.is_superuser:
                        messages.success(request, f"Welcome to Sanalogx, {user.get_full_name() or user.username}. Administrator portal active.")
                    else:
                        messages.success(request, "Welcome to Sanalogx. Your pharmacist account is now active.")

                if user.must_change_password:
                    return redirect('change_password')

                # Server-side immediate redirect (0-1s delay, bypass intermediate countdown)
                next_url = request.POST.get('next') or request.GET.get('next')
                if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
                    is_admin_user = (user.role == User.Role.ADMIN or user.is_superuser)
                    is_admin_path = '/admin' in next_url or 'admin_approvals' in next_url
                    if not is_admin_path or is_admin_user:
                        return redirect(next_url)

                if user.role == User.Role.ADMIN or user.is_superuser:
                    return redirect('admin_approvals')
                return redirect('approved')

        messages.error(request, "Invalid username or password.")

    return render(request, 'auth/login.html')


def pharmacist_register_view(request):
    """
    Public Pharmacist Registration Flow.
    Collects Full Name, Email, Phone, License Number, Qualification, Username, Password.
    Status = PENDING, Role = PHARMACIST, is_active = False until Admin approval.
    Redirects already authenticated users to their correct role-based dashboard.
    """
    if request.user.is_authenticated:
        if request.user.role == User.Role.ADMIN or request.user.is_superuser:
            return redirect('admin_approvals')
        elif request.user.must_change_password:
            return redirect('change_password')
        elif request.user.status == User.Status.PENDING:
            return redirect('pending')
        elif request.user.status == User.Status.REJECTED:
            return redirect('rejected')
        else:
            return redirect('approved')

    if request.method == 'POST':
        full_name = request.POST.get('full_name', '').strip()
        email = request.POST.get('email', '').strip().lower()

        # Support both the new intl-tel-input fields and the legacy plain 'phone' field
        # (legacy field is used by the existing test suite which submits a plain string)
        phone_national = request.POST.get('phone_national', '').strip()
        phone_dial_code = request.POST.get('phone_dial_code', '').strip()
        phone_legacy = request.POST.get('phone', '').strip()

        # Decide which raw value to normalize
        # If the new widget fields are present, use them; otherwise fall back to legacy
        if phone_national:
            phone_raw = phone_national
            phone_dc = phone_dial_code
        else:
            # Legacy call (tests, API): raw may already contain '+' or just digits
            phone_raw = phone_legacy
            phone_dc = ''

        license_number = request.POST.get('license_number', '').strip().upper()
        qualification = request.POST.get('qualification', '').strip()
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        confirm_password = request.POST.get('confirm_password', '')

        # Normalize phone to E.164
        phone_e164, phone_error_msg = normalize_phone(phone_raw, dial_code=phone_dc)

        form_data = {
            'full_name': full_name,
            'email': email,
            # Repopulate the widget on error
            'phone': phone_e164 or phone_raw,       # E.164 or raw fallback
            'phone_national': phone_raw,
            'phone_dial_code': phone_dc or '+91',
            'license_number': license_number,
            'qualification': qualification,
            'username': username,
        }

        # --- Validation ---
        errors = []

        if not full_name:
            errors.append("Full Name is required.")
        if not email:
            errors.append("Email Address is required.")
        else:
            try:
                validate_email(email)
            except ValidationError:
                errors.append("Please enter a valid email address.")
        if phone_error_msg:
            errors.append(phone_error_msg)
        # --- License Number Validation ---
        if not license_number:
            errors.append("Pharmacy License / Registration Number is required.")
        else:
            from django.conf import settings
            is_demo_mode = getattr(settings, 'DEMO_REGISTRATION_MODE', settings.DEBUG)
            demo_pattern = r'^PH-[0-9]{4}$'

            if is_demo_mode:
                if not re.match(demo_pattern, license_number):
                    errors.append("Pharmacy License Number must follow demo format: PH- followed by 4 digits (e.g. PH-1234).")
            else:
                if re.match(demo_pattern, license_number):
                    errors.append("Demo license references are disabled in production. Please provide an official State Pharmacy Council registration number.")
                elif not re.match(r'^[A-Z0-9\-\/]{6,30}$', license_number):
                    errors.append("Please enter a valid State Pharmacy Council license number.")

        if not qualification:
            errors.append("Qualification is required.")

        # Require Username and Password if supplied
        if username:
            if User.objects.filter(username__iexact=username).exists():
                errors.append(f"Username '{username}' is already taken.")
        else:
            # Fallback for legacy test calls missing username field
            username = f"pending_{secrets.token_hex(8)}"

        if password:
            if password != confirm_password:
                errors.append("Password and confirmation password do not match.")
            else:
                try:
                    validate_password(password)
                except ValidationError as err:
                    for e in err.messages:
                        errors.append(e)

        if User.objects.filter(email__iexact=email).exists():
            errors.append(f"An account with email '{email}' already exists.")

        # Unique License Check (prevent duplicates across registrations)
        if license_number and not any("license" in err.lower() for err in errors):
            if PharmacistProfile.objects.filter(
                registration_number__iexact=license_number
            ).exists():
                errors.append("This pharmacy license number is already registered.")

        if errors:
            for err in errors:
                messages.error(request, err)
            from django.conf import settings
            is_demo_mode = getattr(settings, 'DEMO_REGISTRATION_MODE', settings.DEBUG)
            return render(request, 'auth/register.html', {
                'form_data': form_data,
                'demo_mode': is_demo_mode,
                'is_demo_mode': is_demo_mode,
            })

        # --- Create Pharmacist Account ---
        from django.conf import settings
        is_demo_mode = getattr(settings, 'DEMO_REGISTRATION_MODE', settings.DEBUG)
        is_demo_reg = is_demo_mode and bool(re.match(r'^PH-[0-9]{4}$', license_number))

        user = User(
            username=username,
            email=email,
            full_name=full_name,
            phone=phone_e164,   # Store normalized E.164
            role=User.Role.PHARMACIST,
            status=User.Status.PENDING,
            email_verified=False,
            is_active=False,
        )
        user.license_number = license_number
        user.qualification = qualification
        user._temp_is_demo = is_demo_reg
        user._temp_verification_status = 'DEMO_UNVERIFIED' if is_demo_reg else 'UNVERIFIED'

        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()

        user.save()

        # Generate unique Registration ID
        year = timezone.now().year
        user.registration_id = f"MV-PH-{year}-{user.id:05d}"
        user.save(update_fields=['registration_id'])

        # Audit log & emails
        audit_desc = (
            f"Pharmacist account registered with demo license {license_number} (DEMO / UNVERIFIED — pending admin approval)."
            if is_demo_reg else
            f"Pharmacist account registered, pending admin approval."
        )
        # In-app notification for admin staff
        try:
            from notifications.service import notify_pharmacist_registered
            notify_pharmacist_registered(user)
        except Exception as notif_err:
            logger.warning(f"Could not dispatch in-app registration notification: {notif_err}")

        verification_url = _build_verification_url(request, user)
        email_sent = send_registration_email(user)
        send_verification_email(user, verification_url)

        if email_sent:
            messages.success(
                request,
                f"Registration submitted successfully. Pharmacist ID: {user.registration_id}. "
                "Your account is pending administrator approval. A confirmation email has been dispatched."
            )
        else:
            messages.warning(
                request,
                f"Registration account created successfully. Pharmacist ID: {user.registration_id}. "
                "Your account is pending administrator approval. Note: Automated email delivery was not accepted by the SMTP provider at this time."
            )
        return redirect(f'/auth/registration-success/?email={user.email}')

    from django.conf import settings
    is_demo_mode = getattr(settings, 'DEMO_REGISTRATION_MODE', settings.DEBUG)
    return render(request, 'auth/register.html', {
        'demo_mode': is_demo_mode,
        'is_demo_mode': is_demo_mode,
    })



def verify_email_view(request, uidb64, token):
    """Handle email verification link clicks."""
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None

    if user is None:
        return render(request, 'auth/verify_email.html', {
            'success': False,
            'error': 'invalid_link',
            'message': 'This verification link is invalid.'
        })

    if user.email_verified:
        return redirect(f'/auth/registration-success/?email={user.email}')

    try:
        verification = EmailVerification.objects.get(user=user, token=token, is_verified=False)
    except (EmailVerification.DoesNotExist, ValidationError):
        verification = None

    if verification is None:
        return render(request, 'auth/verify_email.html', {
            'success': False,
            'error': 'expired_token',
            'user': user,
            'message': 'This verification link has expired or is invalid. Please request a new one.'
        })

    if verification.expires_at < timezone.now():
        return render(request, 'auth/verify_email.html', {
            'success': False,
            'error': 'expired_token',
            'user': user,
            'message': 'This verification link has expired (valid for 24 hours). Please request a new one.'
        })

    verification.is_verified = True
    verification.save(update_fields=['is_verified'])

    user.email_verified = True
    user.save(update_fields=['email_verified'])

    messages.success(request, "Email verified successfully! Your application is now under review.")
    return redirect(f'/auth/registration-success/?email={user.email}')


def resend_verification_view(request):
    """Allow users to request a new verification email."""
    email_param = ""
    if request.method == 'POST':
        email = request.POST.get('email', '').strip().lower()
        email_param = email
        try:
            user = User.objects.get(email__iexact=email, role=User.Role.PHARMACIST, email_verified=False)
            verification_url = _build_verification_url(request, user)
            send_verification_email(user, verification_url)
            messages.success(request, f"A new verification email has been sent to {email}.")
        except User.DoesNotExist:
            messages.info(request, "If that email is registered and unverified, a new link has been sent.")

    if email_param:
        return redirect(f'/auth/registration-success/?email={email_param}')
    return redirect('verify_email_pending')


def registration_success_view(request):
    """Registration Success Page showing status & verification progress."""
    email = request.GET.get('email', '').strip()
    user = None
    if email:
        try:
            user = User.objects.get(email__iexact=email, role=User.Role.PHARMACIST)
        except User.DoesNotExist:
            user = None

    if not user and request.user.is_authenticated and request.user.role == User.Role.PHARMACIST:
        user = request.user

    if not user:
        messages.error(request, "No registration session found.")
        return redirect('pharmacist_login')

    return render(request, 'auth/registration_success.html', {
        'pharmacist': user,
        'registration_id': user.registration_id,
    })


def verify_email_pending_view(request):
    """Shown after registration — tells user to check their email."""
    return render(request, 'auth/verify_email_pending.html')


def admin_login_view(request):
    """Dedicated Admin Login."""
    if request.user.is_authenticated:
        if request.user.role == User.Role.ADMIN or request.user.is_superuser:
            return redirect('admin_approvals')
        elif request.user.status == User.Status.PENDING:
            return redirect('pending')
        elif request.user.status == User.Status.REJECTED:
            return redirect('rejected')
        else:
            return redirect('approved')

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')

        user = authenticate(request, username=username, password=password)
        if user is not None and (user.role == User.Role.ADMIN or user.is_superuser or user.is_staff):
            login(request, user)
            log_audit(user=user, action='ADMIN_LOGIN', description='Admin logged into portal.')
            messages.success(request, f"Admin portal authenticated. Welcome, {user.username}!")
            return redirect('admin_approvals')
        else:
            messages.error(request, "Invalid Administrator credentials or unauthorized role.")

    return render(request, 'auth/admin_login.html')


def forgot_password_view(request):
    """
    Forgot Password Flow Step 1: User enters email.
    Sends a 6-digit one-time verification code to the registered email.
    """
    if request.method == 'POST':
        email = request.POST.get('email', '').strip().lower()
        if email:
            try:
                user = User.objects.get(email__iexact=email)
                code = str(secrets.randbelow(900000) + 100000)
                PasswordResetRequest.objects.filter(email=email, is_used=False).delete()
                
                PasswordResetRequest.objects.create(
                    user=user,
                    email=email,
                    hashed_code=make_password(code),
                    expires_at=timezone.now() + timedelta(minutes=10)
                )
                send_password_reset_code_email(email, code)
                log_audit(user=user, action='PASSWORD_RESET_REQUESTED', description=f'Password reset code requested for {email}.')
            except User.DoesNotExist:
                pass
        
        request.session['reset_email'] = email
        messages.info(request, "If an account exists with this email, a verification code has been sent.")
        return redirect('verify_reset_code')

    return render(request, 'registration/password_reset_form.html')


def verify_reset_code_view(request):
    """
    Forgot Password Flow Step 2: Enter 6-digit verification code.
    Validates code expiration, attempt limits, and correctness.
    """
    email = request.session.get('reset_email', '')
    if request.method == 'POST':
        code = request.POST.get('code', '').strip()
        email = request.POST.get('email', email).strip().lower()

        try:
            prr = PasswordResetRequest.objects.filter(
                email__iexact=email, is_used=False, expires_at__gt=timezone.now()
            ).latest('created_at')

            if prr.attempt_count >= 5:
                messages.error(request, "Maximum verification attempts exceeded. Please request a new code.")
                return render(request, 'registration/verify_reset_code.html', {'email': email})

            prr.attempt_count += 1
            prr.save(update_fields=['attempt_count'])

            if check_password(code, prr.hashed_code):
                request.session['reset_prr_id'] = prr.id
                return redirect('create_new_password')
            else:
                messages.error(request, "Invalid verification code. Please try again.")
        except PasswordResetRequest.DoesNotExist:
            messages.error(request, "Verification code has expired or is invalid. Please request a new one.")

    return render(request, 'registration/verify_reset_code.html', {'email': email})


def create_new_password_view(request):
    """
    Forgot Password Flow Step 3: Enter and confirm new password after valid code verification.
    """
    prr_id = request.session.get('reset_prr_id')
    if not prr_id:
        messages.error(request, "Password reset session expired. Please start over.")
        return redirect('forgot_password')

    try:
        prr = PasswordResetRequest.objects.get(pk=prr_id, is_used=False)
    except PasswordResetRequest.DoesNotExist:
        messages.error(request, "Invalid or expired password reset session.")
        return redirect('forgot_password')

    if request.method == 'POST':
        new_password = request.POST.get('new_password', '')
        confirm_password = request.POST.get('confirm_password', '')

        if new_password != confirm_password:
            messages.error(request, "New password and confirmation do not match.")
            return render(request, 'registration/create_new_password.html')

        try:
            validate_password(new_password, user=prr.user)
        except ValidationError as err:
            for e in err.messages:
                messages.error(request, e)
            return render(request, 'registration/create_new_password.html')

        user = prr.user
        if user:
            user.set_password(new_password)
            user.must_change_password = False
            user.save()

            prr.is_used = True
            prr.save(update_fields=['is_used'])

            send_password_reset_success_email(user)
            log_audit(user=user, action='PASSWORD_RESET_SUCCESS', description='Password reset successfully via email verification code.')

            # Clear session
            request.session.pop('reset_prr_id', None)
            request.session.pop('reset_email', None)

            messages.success(request, "Your password has been changed successfully. You can now log in using your new password.")
            return redirect('pharmacist_login')

    return render(request, 'registration/create_new_password.html')


def forgot_username_view(request):
    """
    Pharmacist Username Recovery Request.
    User submits email -> UsernameRecoveryRequest created -> Admin review.
    """
    if request.method == 'POST':
        email = request.POST.get('email', '').strip().lower()
        if email:
            user = User.objects.filter(email__iexact=email, role=User.Role.PHARMACIST).first()
            UsernameRecoveryRequest.objects.create(
                email=email,
                user=user,
                status=UsernameRecoveryRequest.Status.PENDING
            )
            log_audit(user=user, action='USERNAME_RECOVERY_REQUESTED', description=f'Username recovery requested for {email}.')

        messages.info(request, "If an account is associated with this email, your recovery request has been submitted for review.")
        return render(request, 'auth/forgot_username.html', {'submitted': True})

    return render(request, 'auth/forgot_username.html')


def pending_view(request):
    """Registration Pending Notice Page."""
    return render(request, 'auth/pending.html')


def approved_view(request):
    """Pharmacist Dashboard / Approved State — with live KPI context."""
    if not request.user.is_authenticated:
        return redirect('pharmacist_login')
    if request.user.role == User.Role.ADMIN:
        return redirect('admin_approvals')
    if request.user.must_change_password:
        return redirect('change_password')
    # Block non-approved pharmacists from reaching the dashboard
    if request.user.role == User.Role.PHARMACIST and request.user.status == User.Status.PENDING:
        return redirect('pending')
    if request.user.role == User.Role.PHARMACIST and request.user.status == User.Status.REJECTED:
        return redirect('rejected')

    # Build live KPI context for pharmacist dashboard
    context = {}
    try:
        from django.utils import timezone as tz
        from datetime import timedelta
        from inventory.models import Inventory, StockAlert
        from medicines.models import MedicineBatch
        today = tz.now().date()

        context['low_stock_count'] = Inventory.objects.filter(stock_status__in=['LOW_STOCK', 'CRITICAL']).count()
        context['out_of_stock_count'] = Inventory.objects.filter(stock_status='OUT_OF_STOCK').count()
        context['near_expiry_count'] = MedicineBatch.objects.filter(
            expiry_date__gt=today,
            expiry_date__lte=today + timedelta(days=90),
            quantity_available__gt=0
        ).count()
        context['active_alerts'] = StockAlert.objects.filter(is_active=True).count()
    except Exception:
        pass

    try:
        from sales.models import Sale
        from django.db.models import Sum
        from datetime import timedelta
        from django.utils import timezone as tz
        today_start = tz.now().replace(hour=0, minute=0, second=0, microsecond=0)
        today_sales_qs = Sale.objects.filter(
            sale_date__gte=today_start,
            status='COMPLETED'
        )
        context['today_sales'] = today_sales_qs.count()
        context['today_sales_count'] = context['today_sales']
        context['today_sales_amount'] = today_sales_qs.aggregate(total=Sum('total_amount'))['total'] or 0
    except Exception:
        pass

    try:
        from dispensing.models import DispensingRecord
        from django.utils import timezone as tz
        today_start = tz.now().replace(hour=0, minute=0, second=0, microsecond=0)
        context['today_dispensed'] = DispensingRecord.objects.filter(
            dispensed_at__gte=today_start
        ).count()
    except Exception:
        context['today_dispensed'] = 0

    try:
        from notifications.models import Notification
        from django.db.models import Q
        context['unread_notifications'] = Notification.objects.filter(
            Q(recipient=request.user) | Q(recipient__isnull=True),
            is_read=False,
            is_archived=False
        ).count()
    except Exception:
        context['unread_notifications'] = 0

    return render(request, 'auth/approved.html', context)


def rejected_view(request):
    """Registration Rejected Notice Page."""
    rejection_reason = ''
    if request.user.is_authenticated and request.user.role == User.Role.PHARMACIST:
        rejection_reason = request.user.rejection_reason
    return render(request, 'auth/rejected.html', {'rejection_reason': rejection_reason})


def suspended_view(request):
    """Account Suspended Notice Page."""
    return render(request, 'auth/suspended.html')


@login_required
def change_password_view(request):
    """
    Password Change View (for authenticated users).
    Requires current password verification, validates new password, updates session hash,
    logs audit event, and dispatches security notification email.
    """
    user = request.user

    if request.method == 'POST':
        current_password = request.POST.get('current_password', '')
        new_password = request.POST.get('new_password', '')
        confirm_password = request.POST.get('confirm_password', '')

        if not user.check_password(current_password):
            messages.error(request, "The current password you entered is incorrect.")
            return render(request, 'auth/change_password.html')

        if new_password != confirm_password:
            messages.error(request, "Your new password and confirmation do not match.")
            return render(request, 'auth/change_password.html')

        if user.check_password(new_password):
            messages.error(request, "Your new password must be different from your current password.")
            return render(request, 'auth/change_password.html')

        try:
            validate_password(new_password, user=user)
        except ValidationError as err:
            for error_msg in err.messages:
                messages.error(request, error_msg)
            return render(request, 'auth/change_password.html')

        user.set_password(new_password)
        user.must_change_password = False
        user.save(update_fields=['password', 'must_change_password'])

        update_session_auth_hash(request, user)
        send_password_changed_email(user)
        log_audit(user=user, action='PASSWORD_CHANGE', description='User successfully changed their password.')

        messages.success(request, "Your Sanalogx account password was successfully changed.")
        return redirect('approved')

    return render(request, 'auth/change_password.html')


def setup_password_view(request, uidb64, token):
    """
    Secure Single-Use Password Setup & Onboarding for Approved Pharmacists.
    HMAC-signed token is cryptographically validated and automatically invalidated once password is saved.
    Ensures that any active session in the browser (such as an Admin testing the link) is cleanly terminated,
    so this onboarding link always operates as a dedicated public MediVault page and never exposes the Admin Dashboard.
    """
    # Terminate any active session so this view is strictly public
    if request.user.is_authenticated:
        logout(request)

    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None

    # Server-side validation: Account identity, approval status, and token validity
    # Reject non-existent users, unapproved/rejected pharmacists, or expired/reused tokens
    if user is None or user.status != User.Status.APPROVED or not pharmacist_password_setup_token_generator.check_token(user, token):
        return render(request, 'auth/setup_password.html', {
            'validlink': False,
            'no_admin_sidebar': True,
            'no_pharmacist_sidebar': True,
            'error_message': 'This password setup link is invalid, expired, or the account is not approved. Please contact hospital administration.'
        })

    if request.method == 'POST':
        password = request.POST.get('password', '')
        confirm_password = request.POST.get('confirm_password', '')

        if not password or not confirm_password:
            messages.error(request, "Please enter and confirm your new password.")
            return render(request, 'auth/setup_password.html', {
                'validlink': True,
                'target_user': user,
                'no_admin_sidebar': True,
                'no_pharmacist_sidebar': True,
            })

        if password != confirm_password:
            messages.error(request, "Password and confirmation password do not match.")
            return render(request, 'auth/setup_password.html', {
                'validlink': True,
                'target_user': user,
                'no_admin_sidebar': True,
                'no_pharmacist_sidebar': True,
            })

        try:
            validate_password(password, user=user)
        except ValidationError as err:
            for error_msg in err.messages:
                messages.error(request, error_msg)
            return render(request, 'auth/setup_password.html', {
                'validlink': True,
                'target_user': user,
                'no_admin_sidebar': True,
                'no_pharmacist_sidebar': True,
            })

        user.set_password(password)
        user.must_change_password = False
        user.is_active = True
        user.status = User.Status.APPROVED
        user.save()

        # Token is automatically invalidated by virtue of password hash update
        log_audit(user=user, action='PASSWORD_SETUP', description='Initial password configured via secure approval link.')
        send_password_changed_email(user)

        # Clear any session state so redirect to login always displays clean public login form
        logout(request)
        request.session.flush()

        messages.success(request, "Your password has been configured successfully. Please sign in with your credentials to access your workspace.")
        return redirect('pharmacist_login')

    return render(request, 'auth/setup_password.html', {
        'validlink': True,
        'target_user': user,
        'no_admin_sidebar': True,
        'no_pharmacist_sidebar': True,
    })


# ---------------------------------------------------------------------------
# Admin Management Views
# ---------------------------------------------------------------------------

@login_required
def admin_approvals_view(request):
    """Admin Dashboard — Pharmacist Registrations, Username Recovery Requests, Audit Logs."""
    if request.user.role != User.Role.ADMIN and not request.user.is_superuser:
        return redirect('unauthorized_403')

    pending_pharmacists = User.objects.filter(
        role=User.Role.PHARMACIST, status=User.Status.PENDING
    ).order_by('-created_at')
    approved_pharmacists = User.objects.filter(
        role=User.Role.PHARMACIST, status=User.Status.APPROVED
    ).order_by('-updated_at')
    rejected_pharmacists = User.objects.filter(
        role=User.Role.PHARMACIST, status=User.Status.REJECTED
    ).order_by('-updated_at')

    username_recoveries = UsernameRecoveryRequest.objects.select_related('user', 'reviewed_by').order_by('-requested_at')
    audit_logs = AuditLog.objects.select_related('user', 'performed_by').order_by('-created_at')[:100]

    context = {
        'pending_pharmacists': pending_pharmacists,
        'approved_pharmacists': approved_pharmacists,
        'rejected_pharmacists': rejected_pharmacists,
        'username_recoveries': username_recoveries,
        'audit_logs': audit_logs,
    }
    return render(request, 'auth/admin_approvals.html', context)


@login_required
def approve_pharmacist_view(request, user_id):
    """
    Admin Action: Approve a pending pharmacist.
    Activates user (is_active = True), records approval_date & approved_by within
    an atomic transaction, prevents duplicate approvals and duplicate emails,
    generates secure single-use onboarding setup link, dispatches email with diagnostics,
    and logs immutable audit trail.
    """
    if request.user.role != User.Role.ADMIN and not request.user.is_superuser:
        return redirect('unauthorized_403')

    if request.method == 'POST':
        pharmacist = get_object_or_404(User, id=user_id, role=User.Role.PHARMACIST)

        # 1. Prevent duplicate approval / duplicate email dispatch on repeated clicks
        if pharmacist.status == User.Status.APPROVED and pharmacist.is_active:
            messages.info(
                request,
                f"Pharmacist '{pharmacist.full_name or pharmacist.username}' is already approved. "
                "Use 'Resend Email' to dispatch another notification without re-approving."
            )
            return redirect('admin_approvals')

        # 2. Assign unique Registration ID if missing
        if not pharmacist.registration_id:
            pharmacist.registration_id = _generate_registration_id(pharmacist)

        temp_password = ''
        if pharmacist.username.startswith('pending_'):
            system_username = _generate_system_username()
            pharmacist.username = system_username
            temp_password = _generate_temp_password()
            pharmacist.set_password(temp_password)
            pharmacist.must_change_password = True

        pharmacist.status = User.Status.APPROVED
        pharmacist.is_active = True
        pharmacist.email_verified = True
        pharmacist.approval_date = timezone.now()
        pharmacist.approved_by = request.user

        # 3. Database transaction: atomic save and audit log
        with transaction.atomic():
            pharmacist.save()
            log_audit(
                user=pharmacist,
                action='ADMIN_APPROVAL',
                description=f"Pharmacist '{pharmacist.username}' approved by admin {request.user.username}.",
                performed_by=request.user
            )

        # 4. Generate cryptographically signed single-use setup token
        uidb64 = urlsafe_base64_encode(force_bytes(pharmacist.pk))
        token = pharmacist_password_setup_token_generator.make_token(pharmacist)
        setup_url = request.build_absolute_uri(reverse('setup_password', kwargs={'uidb64': uidb64, 'token': token}))

        # 5. Dispatch official approval email with diagnostics
        email_sent, diag = send_approval_email_with_diagnostics(
            pharmacist, temp_password=temp_password, setup_url=setup_url
        )

        if email_sent:
            log_audit(
                user=pharmacist,
                action='APPROVAL_EMAIL_DISPATCHED',
                description=f"Approval credential email accepted by mail provider for {pharmacist.email}.",
                performed_by=request.user
            )
            messages.success(
                request,
                f"Pharmacist '{pharmacist.full_name or pharmacist.username}' has been APPROVED. "
                f"Registration ID: {pharmacist.registration_id} | Username: {pharmacist.username}. "
                "Approval email sent."
            )
        else:
            cat = diag.get('category', 'UNKNOWN')
            safe_desc = diag.get('error_message', 'SMTP delivery error')
            log_audit(
                user=pharmacist,
                action='APPROVAL_EMAIL_FAILED',
                description=f"Approval email dispatch failed for {pharmacist.email}. Category: {cat}. Details: {safe_desc}",
                performed_by=request.user
            )
            messages.warning(
                request,
                f"Pharmacist '{pharmacist.full_name or pharmacist.username}' has been APPROVED. "
                f"Registration ID: {pharmacist.registration_id} | Username: {pharmacist.username}. "
                f"Notice: Approval notification could not be delivered by the SMTP provider ({cat}). "
                "You can retry sending the email from the Approved Pharmacists list."
            )

        # 6. Dispatch in-app notification to approved pharmacist
        try:
            from notifications.service import notify_pharmacist_approved
            notify_pharmacist_approved(pharmacist)
        except Exception as notif_err:
            logger.warning(f"Could not dispatch in-app approval notification: {notif_err}")

        return redirect('admin_approvals')

    return redirect('admin_approvals')


@login_required
def resend_approval_email_view(request, user_id):
    """
    Admin Action: Safely retry sending the approval email to an already-approved pharmacist.
    Does NOT approve the account twice or overwrite approval_date / approved_by.
    Preserves existing username and passwords.
    Generates a fresh single-use password setup token only if the pharmacist has not configured a password.
    """
    if request.user.role != User.Role.ADMIN and not request.user.is_superuser:
        return redirect('unauthorized_403')

    if request.method == 'POST':
        is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'
        pharmacist = get_object_or_404(User, id=user_id, role=User.Role.PHARMACIST)

        if pharmacist.status != User.Status.APPROVED:
            err_msg = "Cannot resend approval email for an unapproved pharmacist."
            if is_ajax:
                return JsonResponse({'success': False, 'message': err_msg}, status=400)
            messages.error(request, err_msg)
            return redirect(reverse('admin_approvals') + '#approved-pane')

        if not pharmacist.email:
            err_msg = "This pharmacist account has no registered email address."
            if is_ajax:
                return JsonResponse({'success': False, 'message': err_msg}, status=400)
            messages.error(request, err_msg)
            return redirect(reverse('admin_approvals') + '#approved-pane')

        # Prevent accidental duplicate sends caused by rapid repeated clicks on Resend (15s throttle)
        recent_dispatch = AuditLog.objects.filter(
            user=pharmacist,
            action='APPROVAL_EMAIL_RESENT',
            created_at__gte=timezone.now() - timedelta(seconds=15)
        ).first()
        if recent_dispatch:
            throttle_msg = f"An approval email was already dispatched to {pharmacist.email} moments ago. Please wait before retrying."
            if is_ajax:
                return JsonResponse({'success': False, 'message': throttle_msg}, status=429)
            messages.warning(request, throttle_msg)
            return redirect(reverse('admin_approvals') + '#approved-pane')

        # Determine whether password setup is required
        needs_password_setup = (
            not pharmacist.has_usable_password()
            or pharmacist.must_change_password
            or not pharmacist.first_login_completed
        )

        setup_url = ''
        if needs_password_setup:
            uidb64 = urlsafe_base64_encode(force_bytes(pharmacist.pk))
            token = pharmacist_password_setup_token_generator.make_token(pharmacist)
            setup_url = request.build_absolute_uri(reverse('setup_password', kwargs={'uidb64': uidb64, 'token': token}))

        email_sent, diag = send_approval_email_with_diagnostics(
            pharmacist, setup_url=setup_url
        )

        if email_sent:
            log_audit(
                user=pharmacist,
                action='APPROVAL_EMAIL_RESENT',
                description=(
                    f"Approval notification resent to {pharmacist.email} (Username: {pharmacist.username}) "
                    f"by admin {request.user.username}. Mail provider accepted message."
                ),
                performed_by=request.user
            )
            success_msg = f"Official approval notification successfully sent to {pharmacist.email} for username '{pharmacist.username}'."
            if is_ajax:
                return JsonResponse({'success': True, 'message': success_msg})
            messages.success(request, success_msg)
        else:
            cat = diag.get('category', 'UNKNOWN')
            safe_desc = diag.get('error_message', 'SMTP delivery error')
            log_audit(
                user=pharmacist,
                action='APPROVAL_EMAIL_RESEND_FAILED',
                description=f"Retry email dispatch failed for {pharmacist.email}. Category: {cat}. Details: {safe_desc}",
                performed_by=request.user
            )
            fail_msg = f"Could not deliver email to {pharmacist.email}. Provider error: {cat} ({safe_desc})."
            if is_ajax:
                return JsonResponse({'success': False, 'message': fail_msg}, status=502)
            messages.error(request, fail_msg)

        return redirect(reverse('admin_approvals') + '#approved-pane')

    return redirect(reverse('admin_approvals') + '#approved-pane')


@login_required
def reject_pharmacist_view(request, user_id):
    """Admin Action: Reject Pending Pharmacist — stores reason + sends rejection email + audit log."""
    if request.user.role != User.Role.ADMIN and not request.user.is_superuser:
        return redirect('unauthorized_403')

    if request.method == 'POST':
        pharmacist = get_object_or_404(User, id=user_id, role=User.Role.PHARMACIST)
        rejection_reason = request.POST.get('rejection_reason', '').strip()

        pharmacist.status = User.Status.REJECTED
        pharmacist.is_active = False
        pharmacist.rejection_reason = rejection_reason
        pharmacist.save()

        email_sent = send_rejection_email(pharmacist, rejection_reason)
        log_audit(
            user=pharmacist,
            action='ADMIN_REJECTION',
            description=f"Pharmacist '{pharmacist.username}' rejected by admin. Reason: {rejection_reason}",
            performed_by=request.user
        )

        if email_sent:
            messages.warning(
                request,
                f"Pharmacist '{pharmacist.full_name or pharmacist.email}' has been REJECTED. "
                "A rejection email has been sent."
            )
        else:
            messages.warning(
                request,
                f"Pharmacist '{pharmacist.full_name or pharmacist.email}' has been REJECTED. "
                "Notice: Rejection email could not be delivered by the SMTP provider."
            )

    return redirect('admin_approvals')


@login_required
def approve_username_recovery_view(request, req_id):
    """Admin Action: Approve a username recovery request and send username via email."""
    if request.user.role != User.Role.ADMIN and not request.user.is_superuser:
        return redirect('unauthorized_403')

    if request.method == 'POST':
        recovery_req = get_object_or_404(UsernameRecoveryRequest, pk=req_id)
        user = recovery_req.user or User.objects.filter(email__iexact=recovery_req.email, role=User.Role.PHARMACIST).first()

        if user:
            send_username_recovery_email(user)
            recovery_req.status = UsernameRecoveryRequest.Status.COMPLETED
            recovery_req.reviewed_by = request.user
            recovery_req.reviewed_at = timezone.now()
            recovery_req.recovery_sent_at = timezone.now()
            recovery_req.save()

            log_audit(
                user=user,
                action='USERNAME_RECOVERY_APPROVED',
                description=f"Username recovery approved for {recovery_req.email}. Username sent.",
                performed_by=request.user
            )
            messages.success(request, f"Username recovery approved. Username sent to {recovery_req.email}.")
        else:
            recovery_req.status = UsernameRecoveryRequest.Status.REJECTED
            recovery_req.reviewed_by = request.user
            recovery_req.reviewed_at = timezone.now()
            recovery_req.notes = "No matching registered pharmacist account found."
            recovery_req.save()
            messages.warning(request, f"No matching pharmacist account found for email {recovery_req.email}.")

    return redirect('admin_approvals')


@login_required
def reject_username_recovery_view(request, req_id):
    """Admin Action: Reject a username recovery request."""
    if request.user.role != User.Role.ADMIN and not request.user.is_superuser:
        return redirect('unauthorized_403')

    if request.method == 'POST':
        recovery_req = get_object_or_404(UsernameRecoveryRequest, pk=req_id)
        recovery_req.status = UsernameRecoveryRequest.Status.REJECTED
        recovery_req.reviewed_by = request.user
        recovery_req.reviewed_at = timezone.now()
        recovery_req.save()

        log_audit(
            user=recovery_req.user,
            action='USERNAME_RECOVERY_REJECTED',
            description=f"Username recovery rejected for {recovery_req.email}.",
            performed_by=request.user
        )
        messages.info(request, f"Username recovery request for {recovery_req.email} has been rejected.")

    return redirect('admin_approvals')


@login_required
def pharmacist_detail_view(request, user_id):
    """Admin: View full details of a pharmacist applicant."""
    if request.user.role != User.Role.ADMIN and not request.user.is_superuser:
        return redirect('unauthorized_403')
    pharmacist = get_object_or_404(User, id=user_id, role=User.Role.PHARMACIST)
    return render(request, 'auth/pharmacist_detail.html', {'pharmacist': pharmacist})


@login_required
def login_success_view(request):
    """Role-aware immediate redirect for login success (bypasses countdown)."""
    user = request.user
    if user.role == User.Role.ADMIN or user.is_superuser:
        return redirect('admin_approvals')
    if user.role == User.Role.PHARMACIST:
        if user.status == User.Status.PENDING:
            return redirect('pending')
        elif user.status == User.Status.REJECTED:
            return redirect('rejected')
        return redirect('approved')
    return redirect('home')


def logout_view(request):
    """
    User Logout: Invalidates the authenticated session and redirects to the public homepage.
    Supports secure POST requests with CSRF protection, as well as GET requests for backward compatibility.
    """
    if request.user.is_authenticated:
        log_audit(user=request.user, action='LOGOUT', description='User logged out of Sanalogx.')
    logout(request)
    request.session.flush()
    messages.info(request, "You have been logged out successfully.")
    response = redirect('home')
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0, private'
    response['Pragma'] = 'no-cache'
    response['Expires'] = '0'
    return response


def unauthorized_403_view(request, exception=None):
    """403 Unauthorized Access Page."""
    return render(request, 'auth/403.html', status=403)


def not_found_404_view(request, exception=None):
    """404 Page Not Found."""
    return render(request, 'auth/404.html', status=404)


@login_required
def admin_dashboard_redirect_view(request):
    """Direct route for Admin Dashboard with strict role check."""
    if request.user.role != User.Role.ADMIN and not request.user.is_superuser:
        messages.error(request, "Access restricted. You do not have permission to access the Administrator Dashboard.")
        if request.user.role == User.Role.PHARMACIST:
            if request.user.status == User.Status.PENDING:
                return redirect('pending')
            elif request.user.status == User.Status.REJECTED:
                return redirect('rejected')
        return redirect('approved')
    return redirect('admin_approvals')


@login_required
def pharmacist_dashboard_redirect_view(request):
    """Direct route for Pharmacist Dashboard with strict role check."""
    if request.user.role == User.Role.ADMIN or request.user.is_superuser:
        return redirect('admin_approvals')
    if request.user.status == User.Status.PENDING:
        return redirect('pending')
    if request.user.status == User.Status.REJECTED:
        return redirect('rejected')
    return redirect('approved')

