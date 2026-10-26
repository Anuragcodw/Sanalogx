"""
MediVault Email Service
=======================
Central email dispatch module for all MediVault authentication & security notifications.
All functions are wrapped in try/except so email delivery failures NEVER crash
registration, approval, login, or security transactions.
Never logs or transmits raw credentials in plain text.
All settings read from environment variables / Django settings.
"""

import logging
import smtplib
import socket
import ssl
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# Use helper functions so brand name is always read fresh from settings (never cached at import time)
def _get_site_name():
    return getattr(settings, 'SANALOGX_SITE_NAME', getattr(settings, 'MEDIVAULT_SITE_NAME', 'Sanalogx'))

def _get_base_url():
    return getattr(settings, 'SANALOGX_BASE_URL', getattr(settings, 'MEDIVAULT_BASE_URL', 'http://127.0.0.1:8000'))


import time
from django.core.validators import validate_email
from django.core.exceptions import ValidationError

def get_from_email():
    """
    Constructs official sender identity:
    '{site_name} | Hospital Pharmacy Intelligence <verified_address>'
    Uses the verified sender address configured for the provider.
    Preserves configured display name if explicitly provided in DEFAULT_FROM_EMAIL.
    """
    configured_from = getattr(settings, 'DEFAULT_FROM_EMAIL', '').strip()
    host_user = getattr(settings, 'EMAIL_HOST_USER', '').strip()
    site_name = _get_site_name()

    if configured_from:
        if '<' in configured_from and '>' in configured_from:
            return configured_from
        return f"{site_name} | Hospital Pharmacy Intelligence <{configured_from}>"

    address = host_user or 'noreply@hospital.org'
    return f"{site_name} | Hospital Pharmacy Intelligence <{address}>"


def _is_transient_error(exc):
    """Determine whether an exception is transient and safe to retry once."""
    transient_types = (
        smtplib.SMTPServerDisconnected,
        smtplib.SMTPConnectError,
        socket.timeout,
        TimeoutError,
        ConnectionRefusedError,
        ConnectionResetError,
        BrokenPipeError,
    )
    return isinstance(exc, transient_types)


def _classify_smtp_error(exc):
    """
    Safely classify SMTP exceptions without leaking credentials, passwords, or tokens.
    Returns (category_code, human_safe_description).
    """
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return 'AUTHENTICATION_FAILED', 'SMTP authentication credentials rejected by mail provider (check host user & app password)'
    elif isinstance(exc, smtplib.SMTPServerDisconnected):
        return 'SERVER_DISCONNECTED', 'Mail server unexpectedly closed connection (bad credentials or TLS/firewall termination)'
    elif isinstance(exc, (smtplib.SMTPConnectError, socket.timeout, TimeoutError, ConnectionRefusedError, ConnectionResetError)):
        return 'CONNECTION_TIMEOUT', 'Unable to establish TCP connection to SMTP host or port'
    elif isinstance(exc, smtplib.SMTPRecipientsRefused):
        return 'RECIPIENTS_REFUSED', 'Recipient email address refused by mail server'
    elif isinstance(exc, smtplib.SMTPSenderRefused):
        return 'SENDER_REFUSED', 'Sender identity was rejected by the mail server'
    elif isinstance(exc, smtplib.SMTPDataError):
        return 'DATA_ERROR', 'SMTP provider rejected message data or daily sending quota exceeded'
    elif isinstance(exc, smtplib.SMTPHeloError):
        return 'HELO_ERROR', 'SMTP server rejected HELO/EHLO handshake'
    elif isinstance(exc, ssl.SSLError):
        return 'SSL_TLS_ERROR', 'SSL/TLS handshake negotiation error'
    elif isinstance(exc, ValidationError):
        return 'INVALID_RECIPIENT', 'Recipient email address format is invalid'
    elif isinstance(exc, OSError):
        return 'NETWORK_ERROR', f"Network I/O error during email transmission: {type(exc).__name__}"
    elif isinstance(exc, smtplib.SMTPException):
        return 'SMTP_ERROR', f"SMTP provider exception: {type(exc).__name__}"
    return 'DELIVERY_ERROR', f"Delivery exception: {type(exc).__name__}"


def send_email_with_diagnostics(subject, to_email, html_content, text_content=None, max_retries=1):
    """
    Internal dispatcher that returns (success: bool, diagnostics: dict).
    Diagnostics dict contains:
      - status: 'ACCEPTED' | 'FAILED'
      - category: str
      - error_message: str (sanitized)
      - recipient: str
      - timestamp: datetime
    Performs bounded retry for transient network errors only.
    Never logs or exposes credentials.
    """
    to_email = (to_email or '').strip()
    if not to_email:
        logger.warning("[MediVault Email] No recipient address provided — skipping email dispatch.")
        return False, {
            'status': 'FAILED',
            'category': 'MISSING_RECIPIENT',
            'error_message': 'No recipient address provided.',
            'recipient': '',
            'timestamp': timezone.now()
        }

    try:
        validate_email(to_email)
    except ValidationError as val_err:
        cat, safe_desc = _classify_smtp_error(val_err)
        logger.warning(f"[MediVault Email] Invalid recipient format: '{to_email}'")
        return False, {
            'status': 'FAILED',
            'category': cat,
            'error_message': safe_desc,
            'recipient': to_email,
            'timestamp': timezone.now()
        }

    plain_text = text_content or strip_tags(html_content)
    from_email = get_from_email()

    attempt = 0
    last_exc = None

    while attempt <= max_retries:
        attempt += 1
        try:
            msg = EmailMultiAlternatives(
                subject=subject,
                body=plain_text,
                from_email=from_email,
                to=[to_email],
            )
            msg.attach_alternative(html_content, "text/html")
            msg.send(fail_silently=False)
            logger.info(f"[MediVault Email] Dispatched '{subject}' → {to_email}")
            return True, {
                'status': 'ACCEPTED',
                'category': 'SMTP_ACCEPTED',
                'error_message': '',
                'recipient': to_email,
                'timestamp': timezone.now()
            }
        except Exception as exc:
            last_exc = exc
            if attempt <= max_retries and _is_transient_error(exc):
                logger.warning(
                    f"[MediVault Email] Transient error on attempt {attempt} for '{subject}' → {to_email}: "
                    f"{type(exc).__name__}. Retrying in 0.5s..."
                )
                time.sleep(0.5)
                continue
            break

    cat, safe_desc = _classify_smtp_error(last_exc)
    logger.error(
        f"[MediVault Email] Delivery failure for '{subject}' → {to_email}: "
        f"Category={cat} | Description={safe_desc}"
    )
    return False, {
        'status': 'FAILED',
        'category': cat,
        'error_message': safe_desc,
        'recipient': to_email,
        'timestamp': timezone.now()
    }


def _send_email(subject, to_email, html_content, text_content=None):
    """
    Internal helper — sends an HTML email with plain-text fallback.
    Returns True on success, False on failure. Never raises an exception.
    """
    success, _ = send_email_with_diagnostics(subject, to_email, html_content, text_content)
    return success


def send_registration_email(user):
    """
    Dispatched immediately after public registration submission.
    Confirms receipt and informs applicant of pending administrator review.
    Subject: MediVault | Pharmacist Registration Received
    """
    subject = f"{_get_site_name()} | Pharmacist Registration Received"
    context = {
        'user': user,
        'site_name': _get_site_name(),
        'base_url': _get_base_url(),
        'current_year': timezone.now().year,
    }
    try:
        html_content = render_to_string('emails/registration_email.html', context)
        text_content = render_to_string('emails/registration_email.txt', context)
    except Exception as exc:
        logger.error(f"[MediVault Email] Render error for registration email: {exc}")
        html_content = f"""
        <p>Dear {user.full_name or user.username},</p>
        <p>Your registration for {_get_site_name()} has been received.</p>
        <p><strong>Registration ID:</strong> {user.registration_id or 'Pending'}</p>
        <p><strong>Status:</strong> Under Administrator Review</p>
        <p>You will receive an official notification once your application is reviewed.</p>
        <p>{_get_site_name()} Pharmacy Systems</p>
        """
        text_content = strip_tags(html_content)

    return _send_email(subject, user.email, html_content, text_content)


def send_verification_email(user, verification_url):
    """
    Dispatched after registration with a secure 24-hour HMAC email verification link.
    Subject: MediVault | Verify Your Email Address
    """
    subject = f"{_get_site_name()} | Verify Your Email Address"
    context = {
        'user': user,
        'site_name': _get_site_name(),
        'base_url': _get_base_url(),
        'verification_url': verification_url,
        'current_year': timezone.now().year,
    }
    try:
        html_content = render_to_string('emails/verification_email.html', context)
        text_content = render_to_string('emails/verification_email.txt', context)
    except Exception as exc:
        logger.error(f"[MediVault Email] Render error for verification email: {exc}")
        html_content = f"""
        <p>Dear {user.full_name or user.username},</p>
        <p>Please verify your email address to continue your {_get_site_name()} registration.</p>
        <p><a href="{verification_url}">Verify Email Address</a></p>
        <p>This link expires in 24 hours.</p>
        <p>{_get_site_name()} Pharmacy Systems</p>
        """
        text_content = f"Verify your email: {verification_url}"

    return _send_email(subject, user.email, html_content, text_content)


def send_approval_email_with_diagnostics(user, temp_password='', setup_url=''):
    """
    Dispatched when Administrator approves a pharmacist application.
    Provides system username, registration ID, and secure onboarding link.
    Subject: MediVault | Pharmacist Account Approved
    Returns (success: bool, diagnostics: dict).
    """
    subject = f"{_get_site_name()} | Pharmacist Account Approved"
    login_url = f"{_get_base_url()}/auth/login/"
    forgot_password_url = f"{_get_base_url()}/auth/forgot-password/"
    context = {
        'user': user,
        'site_name': _get_site_name(),
        'base_url': _get_base_url(),
        'login_url': login_url,
        'forgot_password_url': forgot_password_url,
        'temp_password': temp_password,
        'setup_url': setup_url,
        'registration_id': user.registration_id,
        'system_username': user.username,
        'approval_date': timezone.now().strftime('%d %b %Y, %I:%M %p'),
        'current_year': timezone.now().year,
    }
    try:
        html_content = render_to_string('emails/approval_email.html', context)
        text_content = render_to_string('emails/approval_email.txt', context)
    except Exception as exc:
        logger.error(f"[MediVault Email] Render error for approval email: {exc}")
        action_note = f"<p>Setup password: <a href='{setup_url}'>{setup_url}</a></p>" if setup_url else ""
        if temp_password:
            action_note += f"<p>Temporary Password: <code>{temp_password}</code></p>"
        html_content = f"""
        <p>Congratulations {user.full_name or user.username},</p>
        <p>Your {_get_site_name()} pharmacist account has been approved by the administrator.</p>
        <p><strong>Username:</strong> {user.username}</p>
        <p><strong>Registration ID:</strong> {user.registration_id}</p>
        {action_note}
        <p><a href="{login_url}">Sign In to Sanalogx</a></p>
        <p>{_get_site_name()} Pharmacy Systems</p>
        """
        text_content = strip_tags(html_content)

    return send_email_with_diagnostics(subject, user.email, html_content, text_content)


def send_approval_email(user, temp_password='', setup_url=''):
    """
    Backwards-compatible wrapper returning boolean.
    """
    success, _ = send_approval_email_with_diagnostics(user, temp_password=temp_password, setup_url=setup_url)
    return success


def send_rejection_email(user, reason=''):
    """
    Dispatched when Administrator rejects a pharmacist application.
    Subject: MediVault | Registration Decision
    """
    subject = f"{_get_site_name()} | Registration Decision"
    context = {
        'user': user,
        'site_name': _get_site_name(),
        'base_url': _get_base_url(),
        'rejection_reason': reason or "Credential verification requirements were not met.",
        'current_year': timezone.now().year,
    }
    try:
        html_content = render_to_string('emails/rejection_email.html', context)
        text_content = render_to_string('emails/rejection_email.txt', context)
    except Exception as exc:
        logger.error(f"[MediVault Email] Render error for rejection email: {exc}")
        html_content = f"""
        <p>Dear {user.full_name or user.username},</p>
        <p>Your {_get_site_name()} pharmacist application could not be approved at this time.</p>
        <p><strong>Reason:</strong> {reason or 'Credential verification requirements were not met.'}</p>
        <p>Contact hospital administration for further inquiries.</p>
        <p>{_get_site_name()} Pharmacy Systems</p>
        """
        text_content = strip_tags(html_content)

    return _send_email(subject, user.email, html_content, text_content)


def send_login_notification_email(user, request=None):
    """
    Dispatched on successful sign-in with session security metadata.
    Subject: MediVault | Successful Sign-In Notification
    """
    subject = f"{_get_site_name()} | Successful Sign-In Notification"

    ip_address = 'Authorized Network'
    user_agent = 'Standard Healthcare Browser'
    if request:
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip_address = x_forwarded_for.split(',')[0].strip()
        else:
            ip_address = request.META.get('REMOTE_ADDR', 'Authorized Network')
        user_agent = request.META.get('HTTP_USER_AGENT', 'Standard Healthcare Browser')

    login_time = timezone.localtime(timezone.now()).strftime('%d %b %Y, %I:%M %p %Z')

    context = {
        'user': user,
        'site_name': _get_site_name(),
        'base_url': _get_base_url(),
        'ip_address': ip_address,
        'user_agent': user_agent,
        'login_time': login_time,
        'current_year': timezone.now().year,
    }
    try:
        html_content = render_to_string('emails/login_notification_email.html', context)
        text_content = render_to_string('emails/login_notification_email.txt', context)
    except Exception as exc:
        logger.error(f"[MediVault Email] Render error for login notification: {exc}")
        html_content = f"""
        <p>Hello {user.full_name or user.username},</p>
        <p>Your {_get_site_name()} account was successfully accessed at {login_time}.</p>
        <p>IP Address: {ip_address}</p>
        <p>If you did not sign in, please contact system administration immediately.</p>
        """
        text_content = strip_tags(html_content)

    return _send_email(subject, user.email, html_content, text_content)


def send_password_changed_email(user):
    """
    Dispatched when a user successfully updates or configures their password.
    Subject: MediVault | Password Changed Successfully
    """
    subject = f"{_get_site_name()} | Password Changed Successfully"
    context = {
        'user': user,
        'site_name': _get_site_name(),
        'base_url': _get_base_url(),
        'current_year': timezone.now().year,
    }
    try:
        html_content = render_to_string('emails/password_changed_email.html', context)
        text_content = render_to_string('emails/password_changed_email.txt', context)
    except Exception as exc:
        logger.error(f"[MediVault Email] Render error for password changed email: {exc}")
        html_content = f"""
        <p>Dear {user.full_name or user.username},</p>
        <p>Your {_get_site_name()} account password was successfully updated.</p>
        <p>If you did not perform this change, contact system administration immediately.</p>
        """
        text_content = strip_tags(html_content)

    return _send_email(subject, user.email, html_content, text_content)


def send_password_reset_code_email(email, code):
    """
    Dispatched when a self-service 6-digit password reset code is requested.
    Subject: MediVault | Password Reset Verification Code
    """
    subject = f"{_get_site_name()} | Password Reset Verification Code"
    context = {
        'code': code,
        'site_name': _get_site_name(),
        'base_url': _get_base_url(),
        'current_year': timezone.now().year,
    }
    try:
        html_content = render_to_string('emails/password_reset_code_email.html', context)
        text_content = render_to_string('emails/password_reset_code_email.txt', context)
    except Exception as exc:
        logger.error(f"[MediVault Email] Render error for password reset code: {exc}")
        html_content = f"""
        <p>Your {_get_site_name()} verification code is: <strong>{code}</strong></p>
        <p>This code expires in 10 minutes.</p>
        """
        text_content = f"Your {_get_site_name()} verification code is: {code} (expires in 10 minutes)"

    return _send_email(subject, email, html_content, text_content)


def send_password_reset_success_email(user):
    """Alias for send_password_changed_email for consistency."""
    return send_password_changed_email(user)


def send_username_recovery_email(user):
    """
    Dispatched when Administrator approves a username recovery request.
    Subject: MediVault | Username Recovery
    """
    subject = f"{_get_site_name()} | Username Recovery"
    context = {
        'user': user,
        'site_name': _get_site_name(),
        'base_url': _get_base_url(),
        'current_year': timezone.now().year,
    }
    try:
        html_content = render_to_string('emails/username_recovery_email.html', context)
        text_content = render_to_string('emails/username_recovery_email.txt', context)
    except Exception as exc:
        logger.error(f"[MediVault Email] Render error for username recovery: {exc}")
        html_content = f"""
        <p>Dear {user.full_name or user.username},</p>
        <p>Your registered username for {_get_site_name()} is: <strong>{user.username}</strong></p>
        """
        text_content = f"Your {_get_site_name()} username is: {user.username}"

    return _send_email(subject, user.email, html_content, text_content)
