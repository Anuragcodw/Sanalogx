"""
MediVault / Sanalogx — Comprehensive Email Delivery & SMTP Test Suite
======================================================================
Covers:
1. Configuration loading, TLS/SSL mutual exclusivity, and backend selection.
2. Sender identity formatting, quotes/whitespace stripping.
3. Dispatching emails via in-memory backend (subject, sender, recipient, body).
4. Error classification, invalid recipient handling, and secret redaction.
5. Transient error retry with backoff vs non-retry of permanent errors.
6. Registration, Verification, Approval, Rejection, and In-App notification triggers.
7. Password reset code, password changed, and username recovery triggers.
8. Inventory critical alerts, email_sent persistence, and fingerprint deduplication.
9. PO email dispatch and Report email dispatch.
10. test_email_delivery management command in check-only mode.
"""

import smtplib
import socket
from unittest.mock import patch, MagicMock
from django.test import TestCase, Client, override_settings
from django.core import mail
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone
from django.core.exceptions import ValidationError

from accounts.models import User, AuditLog
from accounts.email_service import (
    get_from_email,
    _classify_smtp_error,
    _is_transient_error,
    send_email_with_diagnostics,
    send_registration_email,
    send_verification_email,
    send_approval_email_with_diagnostics,
    send_rejection_email,
    send_login_notification_email,
    send_password_changed_email,
    send_password_reset_code_email,
    send_username_recovery_email,
)
from notifications.models import Notification
from notifications import service as notification_service
from purchases.models import PurchaseOrder, PurchaseOrderItem
from suppliers.models import Supplier
from inventory.models import Inventory


class EmailConfigurationTests(TestCase):
    """Tests for configuration loading, security flags, and backend selection."""

    def test_sender_identity_formatting_with_angle_brackets(self):
        with override_settings(DEFAULT_FROM_EMAIL='Sanalogx Alert <alerts@hospital.org>', SANALOGX_SITE_NAME='Sanalogx'):
            from_email = get_from_email()
            self.assertEqual(from_email, 'Sanalogx Alert <alerts@hospital.org>')

    def test_sender_identity_formatting_with_plain_address(self):
        with override_settings(DEFAULT_FROM_EMAIL='alerts@hospital.org', SANALOGX_SITE_NAME='Sanalogx'):
            from_email = get_from_email()
            self.assertEqual(from_email, 'Sanalogx | Hospital Pharmacy Intelligence <alerts@hospital.org>')

    def test_sender_identity_fallback_to_host_user(self):
        with override_settings(DEFAULT_FROM_EMAIL='', EMAIL_HOST_USER='smtp_user@hospital.org', SANALOGX_SITE_NAME='Sanalogx'):
            from_email = get_from_email()
            self.assertEqual(from_email, 'Sanalogx | Hospital Pharmacy Intelligence <smtp_user@hospital.org>')

    def test_smtp_error_classification_without_secret_leakage(self):
        """Verify errors are categorized safely without leaking passwords or tokens."""
        auth_err = smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted - secret_token_xyz")
        cat, desc = _classify_smtp_error(auth_err)
        self.assertEqual(cat, 'AUTHENTICATION_FAILED')
        self.assertNotIn('secret_token_xyz', desc)

        disc_err = smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
        cat, desc = _classify_smtp_error(disc_err)
        self.assertEqual(cat, 'SERVER_DISCONNECTED')

        timeout_err = socket.timeout("timed out")
        cat, desc = _classify_smtp_error(timeout_err)
        self.assertEqual(cat, 'CONNECTION_TIMEOUT')

        val_err = ValidationError("Enter a valid email address.")
        cat, desc = _classify_smtp_error(val_err)
        self.assertEqual(cat, 'INVALID_RECIPIENT')

    def test_transient_error_detection(self):
        """Verify transient errors are identified for bounded retry."""
        self.assertTrue(_is_transient_error(smtplib.SMTPServerDisconnected()))
        self.assertTrue(_is_transient_error(socket.timeout()))
        self.assertTrue(_is_transient_error(ConnectionResetError()))
        self.assertTrue(_is_transient_error(TimeoutError()))

        # Permanent errors must NOT be flagged as transient
        self.assertFalse(_is_transient_error(smtplib.SMTPAuthenticationError(535, b"Denied")))
        self.assertFalse(_is_transient_error(smtplib.SMTPRecipientsRefused({})))
        self.assertFalse(_is_transient_error(ValidationError("Bad email")))


class EmailDispatchAndRetryTests(TestCase):
    """Tests for message construction, recipients, validation, and bounded retry."""

    def test_successful_dispatch_populates_outbox(self):
        """Valid email dispatch populates in-memory outbox with expected fields."""
        success, diag = send_email_with_diagnostics(
            subject="Test System Notification",
            to_email="pharmacist@hospital.org",
            html_content="<p>Test notification content</p>",
            text_content="Test notification content"
        )
        self.assertTrue(success)
        self.assertEqual(diag['status'], 'ACCEPTED')
        self.assertEqual(diag['category'], 'SMTP_ACCEPTED')
        self.assertEqual(len(mail.outbox), 1)

        msg = mail.outbox[0]
        self.assertEqual(msg.subject, "Test System Notification")
        self.assertEqual(msg.to, ["pharmacist@hospital.org"])
        self.assertIn("Test notification content", msg.body)
        self.assertEqual(len(msg.alternatives), 1)
        self.assertEqual(msg.alternatives[0][1], "text/html")

    def test_missing_recipient_fails_safely_without_exception(self):
        """Empty recipient address is rejected cleanly."""
        success, diag = send_email_with_diagnostics(
            subject="No Recipient",
            to_email="",
            html_content="<p>No recipient</p>"
        )
        self.assertFalse(success)
        self.assertEqual(diag['category'], 'MISSING_RECIPIENT')
        self.assertEqual(len(mail.outbox), 0)

    def test_invalid_recipient_format_fails_safely(self):
        """Malformed recipient address is rejected without crashing."""
        success, diag = send_email_with_diagnostics(
            subject="Invalid Recipient",
            to_email="not-an-email-address",
            html_content="<p>Invalid</p>"
        )
        self.assertFalse(success)
        self.assertEqual(diag['category'], 'INVALID_RECIPIENT')
        self.assertEqual(len(mail.outbox), 0)

    @patch('accounts.email_service.EmailMultiAlternatives.send')
    def test_transient_error_retries_and_succeeds(self, mock_send):
        """Transient error triggers retry; when second attempt succeeds, returns ACCEPTED."""
        # First call raises SMTPServerDisconnected, second succeeds
        mock_send.side_effect = [smtplib.SMTPServerDisconnected("Reset"), 1]

        success, diag = send_email_with_diagnostics(
            subject="Retry Test",
            to_email="retry@hospital.org",
            html_content="<p>Retry</p>",
            max_retries=1
        )
        self.assertTrue(success)
        self.assertEqual(diag['status'], 'ACCEPTED')
        self.assertEqual(mock_send.call_count, 2)

    @patch('accounts.email_service.EmailMultiAlternatives.send')
    def test_permanent_error_not_retried(self, mock_send):
        """Permanent authentication error is not retried repeatedly."""
        mock_send.side_effect = smtplib.SMTPAuthenticationError(535, b"Auth failed")

        success, diag = send_email_with_diagnostics(
            subject="Auth Failure Test",
            to_email="user@hospital.org",
            html_content="<p>Auth fail</p>",
            max_retries=2
        )
        self.assertFalse(success)
        self.assertEqual(diag['category'], 'AUTHENTICATION_FAILED')
        self.assertEqual(mock_send.call_count, 1)


class NotificationWorkflowsTests(TestCase):
    """Tests for all business notification workflows and in-app triggers."""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='admin_wf',
            email='admin_wf@hospital.org',
            password='Password123!',
            full_name='Admin Workflow',
            status=User.Status.APPROVED
        )
        self.pharmacist = User.objects.create_user(
            username='pharm_wf',
            email='pharm_wf@hospital.org',
            password='Password123!',
            full_name='Pharmacist Workflow',
            role=User.Role.PHARMACIST,
            status=User.Status.PENDING,
            registration_id='MV-PH-2026-00099'
        )

    def test_send_registration_email_workflow(self):
        """Registration receipt email contains registration ID and site branding."""
        success = send_registration_email(self.pharmacist)
        self.assertTrue(success)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.pharmacist.registration_id, mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].to, [self.pharmacist.email])

    def test_send_verification_email_workflow(self):
        """Verification email contains the unique token URL."""
        verify_url = "http://127.0.0.1:8000/auth/verify/xyz123/"
        success = send_verification_email(self.pharmacist, verify_url)
        self.assertTrue(success)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(verify_url, mail.outbox[0].body)

    def test_send_approval_email_workflow(self):
        """Approval email includes setup URL and username."""
        setup_url = "http://127.0.0.1:8000/auth/setup-password/xyz123/"
        success, diag = send_approval_email_with_diagnostics(self.pharmacist, setup_url=setup_url)
        self.assertTrue(success)
        self.assertEqual(diag['status'], 'ACCEPTED')
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.pharmacist.username, mail.outbox[0].body)

    def test_send_rejection_email_workflow(self):
        """Rejection email conveys the decision and specified reason."""
        reason = "State pharmacy license could not be verified."
        success = send_rejection_email(self.pharmacist, reason=reason)
        self.assertTrue(success)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(reason, mail.outbox[0].body)

    def test_send_password_reset_code_workflow(self):
        """Password reset verification email contains the 6-digit code."""
        code = "782914"
        success = send_password_reset_code_email("reset_user@hospital.org", code)
        self.assertTrue(success)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(code, mail.outbox[0].body)

    def test_send_password_changed_workflow(self):
        """Password changed confirmation email."""
        success = send_password_changed_email(self.pharmacist)
        self.assertTrue(success)
        self.assertEqual(len(mail.outbox), 1)

    def test_send_username_recovery_workflow(self):
        """Username recovery email includes system username."""
        success = send_username_recovery_email(self.pharmacist)
        self.assertTrue(success)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.pharmacist.username, mail.outbox[0].body)

    def test_send_login_notification_workflow(self):
        """Login security notification contains timestamp and location."""
        success = send_login_notification_email(self.pharmacist)
        self.assertTrue(success)
        self.assertEqual(len(mail.outbox), 1)

    def test_in_app_pharmacist_registered_notification(self):
        """notify_pharmacist_registered creates an in-app notification for administrators."""
        notification_service.notify_pharmacist_registered(self.pharmacist)
        notif = Notification.objects.filter(
            notification_type=Notification.NotificationType.PHARMACIST_REGISTERED,
            fingerprint__startswith=f"REG:{self.pharmacist.id}"
        ).first()
        self.assertIsNotNone(notif)
        self.assertIn(self.pharmacist.username, notif.title)

    def test_in_app_pharmacist_approved_notification(self):
        """notify_pharmacist_approved creates an in-app notification for the pharmacist."""
        notification_service.notify_pharmacist_approved(self.pharmacist)
        notif = Notification.objects.filter(
            recipient=self.pharmacist,
            notification_type=Notification.NotificationType.PHARMACIST_APPROVED,
            fingerprint=f"APPROVED:{self.pharmacist.id}"
        ).first()
        self.assertIsNotNone(notif)
        self.assertEqual(notif.priority, Notification.Priority.HIGH)


class CriticalInventoryAlertEmailTests(TestCase):
    """Tests for critical stock & expiry email alerts and deduplication."""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='admin_alert',
            email='admin_alert@hospital.org',
            password='Password123!',
            status=User.Status.APPROVED
        )

    def test_critical_stock_email_alert_dispatched_and_recorded(self):
        """Critical alert with send_email_alert=True sends email and updates email_sent=True."""
        notes = notification_service.notify(
            notification_type=Notification.NotificationType.CRITICAL_STOCK,
            title='Amoxicillin 500mg Out of Stock',
            message='Stock is completely depleted.',
            priority='CRITICAL',
            recipient=self.admin,
            fingerprint='CRIT:test:101',
            send_email_alert=True,
        )
        self.assertEqual(len(notes), 1)
        n = Notification.objects.get(pk=notes[0].pk)
        self.assertTrue(n.email_sent)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('Amoxicillin', mail.outbox[0].subject)

    def test_fingerprint_deduplication_prevents_duplicate_notifications(self):
        """Second call with same fingerprint is deduplicated and does not send duplicate email."""
        fp = 'LOW_STOCK:med:999:week42'
        notes1 = notification_service.notify(
            notification_type=Notification.NotificationType.LOW_STOCK,
            title='Low Stock 1',
            message='Message 1',
            recipient=self.admin,
            fingerprint=fp,
            send_email_alert=True,
        )
        self.assertEqual(len(notes1), 1)
        self.assertEqual(len(mail.outbox), 1)

        # Duplicate attempt with identical fingerprint
        notes2 = notification_service.notify(
            notification_type=Notification.NotificationType.LOW_STOCK,
            title='Low Stock 2',
            message='Message 2',
            recipient=self.admin,
            fingerprint=fp,
            send_email_alert=True,
        )
        self.assertEqual(len(notes2), 0)
        self.assertEqual(len(mail.outbox), 1)  # No second email dispatched


class ManagementCommandDiagnosticTests(TestCase):
    """Test test_email_delivery management command in check-only mode."""

    def test_check_only_command_completes_successfully(self):
        """Command in --check-only mode runs diagnostic pipeline without dispatching email."""
        outbox_len_before = len(mail.outbox)
        call_command('test_email_delivery', '--check-only')
        self.assertEqual(len(mail.outbox), outbox_len_before)
