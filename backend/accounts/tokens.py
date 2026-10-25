"""
MediVault Email Verification & Password Setup Token Generators
==============================================================
Uses Django's PasswordResetTokenGenerator as a base to create
time-limited, user-specific, HMAC-signed tokens.

Tokens:
- Are HMAC-signed (not stored in DB — cryptographically verified)
- Email verification expires after EMAIL_VERIFICATION_TIMEOUT_HOURS (default: 24h)
- Password setup token is invalidated once password is set or account is activated.
"""

from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.utils import timezone
from django.conf import settings


EMAIL_VERIFICATION_TIMEOUT_HOURS = getattr(settings, 'EMAIL_VERIFICATION_TIMEOUT_HOURS', 24)


class EmailVerificationTokenGenerator(PasswordResetTokenGenerator):
    """
    Custom token generator for email verification.
    Inherits Django's built-in HMAC token generation.
    Tokens become invalid once email_verified = True or password changes.
    """

    def _make_hash_value(self, user, timestamp):
        return (
            str(user.pk)
            + str(timestamp)
            + str(user.email_verified)
            + str(user.email)
            + str(user.password)
        )


class PharmacistPasswordSetupTokenGenerator(PasswordResetTokenGenerator):
    """
    Custom token generator for newly approved pharmacists to securely configure their initial password.
    Invalidated once the password is set, must_change_password changes, or the user logs in.
    """

    def _make_hash_value(self, user, timestamp):
        login_timestamp = '' if user.last_login is None else user.last_login.replace(microsecond=0, tzinfo=None)
        return (
            str(user.pk)
            + str(user.password)
            + str(login_timestamp)
            + str(timestamp)
            + str(user.is_active)
            + str(user.must_change_password)
        )


email_verification_token_generator = EmailVerificationTokenGenerator()
pharmacist_password_setup_token_generator = PharmacistPasswordSetupTokenGenerator()
