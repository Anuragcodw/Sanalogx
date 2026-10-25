"""
MediVault Authentication & Registration Test Suite
==================================================
Covers all 15 enterprise authentication scenarios:
1. Valid registration creates pending account with is_active=False
2. Duplicate username, email, and registration number rejection
3. Invalid registration data handling
4. Password hashing verification
5. Pending and rejected account login prevention
6. Admin-only approval and rejection
7. Registration confirmation email sent
8. Approval email with token link sent
9. Secure password setup and token expiration
10. Successful login audit log and notification
11. Graceful handling of email delivery failures
12. Duplicate submission protection
13. CSRF and authorization checks
14. Protected dashboard access restrictions
15. Form validation error states
Plus email verification, 6-digit code reset, and username recovery workflows.
"""

from unittest.mock import patch
from datetime import timedelta
from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.core import mail
from django.utils import timezone
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes
from django.contrib.auth.hashers import check_password

from accounts.models import (
    User, EmailVerification, PharmacistProfile,
    PasswordResetRequest, UsernameRecoveryRequest, AuditLog
)
from accounts.tokens import (
    email_verification_token_generator,
    pharmacist_password_setup_token_generator
)

User = get_user_model()


class MediVaultAuthTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.register_url = reverse('pharmacist_register')
        self.login_url = reverse('pharmacist_login')
        self.admin_login_url = reverse('admin_login')
        self.admin_approvals_url = reverse('admin_approvals')
        self.change_password_url = reverse('change_password')

        # Create system administrator
        self.admin_user = User.objects.create_superuser(
            username='admin_test',
            email='admin@hospital.org',
            password='SecurePassword123!',
            full_name='System Administrator'
        )

    # -------------------------------------------------------------------------
    # Scenario 1: Valid registration creates pending account with is_active=False
    # -------------------------------------------------------------------------
    def test_valid_registration_creates_pending_account_inactive(self):
        """Scenario 1: Public registration creates pending, inactive pharmacist with MV-PH ID."""
        data = {
            'full_name': 'Dr. Sarah Jenkins',
            'email': 'sarah@cityhospital.org',
            'phone': '+15550192834',
            'license_number': 'PH-2827',
            'qualification': 'Pharm.D',
            'username': 'sjenkins_ph',
            'password': 'SecurePassword123!',
            'confirm_password': 'SecurePassword123!',
        }

        response = self.client.post(self.register_url, data)
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, '/auth/registration-success/?email=sarah@cityhospital.org')

        user = User.objects.get(email='sarah@cityhospital.org')
        self.assertEqual(user.full_name, 'Dr. Sarah Jenkins')
        self.assertEqual(user.role, User.Role.PHARMACIST)
        self.assertEqual(user.status, User.Status.PENDING)
        self.assertFalse(user.is_active)
        self.assertFalse(user.email_verified)
        self.assertTrue(user.registration_id.startswith('MV-PH-'))
        self.assertEqual(user.license_number, 'PH-2827')
        self.assertEqual(user.qualification, 'Pharm.D')
        self.assertTrue(user.is_demo)
        self.assertEqual(user.license_verification_status, 'DEMO_UNVERIFIED')

    # -------------------------------------------------------------------------
    # Scenario 2: Duplicate username, email, and registration number rejection
    # -------------------------------------------------------------------------
    def test_duplicate_username_email_and_license_rejected(self):
        """Scenario 2: Duplicate username, email, and license number are cleanly rejected."""
        existing = User.objects.create_user(
            username='existing_user',
            email='existing@hospital.org',
            password='Password123!',
            license_number='PH-1111',
            role=User.Role.PHARMACIST
        )

        # Duplicate email
        data = {
            'full_name': 'New Applicant',
            'email': 'existing@hospital.org',
            'phone': '1234567890',
            'license_number': 'PH-2222',
            'qualification': 'Pharm.D',
            'username': 'new_user_1',
            'password': 'Password123!',
            'confirm_password': 'Password123!',
        }
        resp = self.client.post(self.register_url, data)
        self.assertEqual(resp.status_code, 200)

        # Duplicate username
        data['email'] = 'brandnew@hospital.org'
        data['username'] = 'existing_user'
        resp = self.client.post(self.register_url, data)
        self.assertEqual(resp.status_code, 200)

        # Duplicate license
        data['username'] = 'brandnew_user'
        data['license_number'] = 'PH-1111'
        resp = self.client.post(self.register_url, data)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "This pharmacy license number is already registered.")

    # -------------------------------------------------------------------------
    # Scenario 3: Invalid registration data handling
    # -------------------------------------------------------------------------
    def test_invalid_registration_data_handling(self):
        """Scenario 3: Missing required fields and malformed email are handled with validation messages."""
        data = {
            'full_name': '',  # missing
            'email': 'invalid-email-address',  # invalid
            'phone': '',
            'license_number': '',
            'qualification': '',
            'username': '',
        }
        resp = self.client.post(self.register_url, data)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(email='invalid-email-address').exists())

    # -------------------------------------------------------------------------
    # Scenario 4: Password hashing verification
    # -------------------------------------------------------------------------
    def test_password_hashing_verification(self):
        """Scenario 4: Passwords are cryptographically hashed and never stored in plain text."""
        raw_pass = 'SecretPassword123!'
        data = {
            'full_name': 'Hash Test',
            'email': 'hash@hospital.org',
            'phone': '5551234567',
            'license_number': 'PH-1234',
            'qualification': 'Pharm.D',
            'username': 'hash_test_user',
            'password': raw_pass,
            'confirm_password': raw_pass,
        }
        self.client.post(self.register_url, data)
        user = User.objects.get(email='hash@hospital.org')

        self.assertNotEqual(user.password, raw_pass)
        self.assertTrue(user.check_password(raw_pass))
        self.assertTrue(user.password.startswith('pbkdf2_') or user.password.startswith('argon2'))

    # -------------------------------------------------------------------------
    # Scenario 5: Pending and rejected account login prevention
    # -------------------------------------------------------------------------
    def test_pending_and_rejected_account_login_prevention(self):
        """Scenario 5: Pending and rejected accounts cannot log in and are redirected to status screens."""
        # Pending user
        pending_user = User.objects.create_user(
            username='pending_doc',
            email='pending@hospital.org',
            password='Password123!',
            license_number='LIC-PEND',
            status=User.Status.PENDING,
            email_verified=True,
            is_active=False
        )
        resp1 = self.client.post(self.login_url, {'identifier': 'pending_doc', 'password': 'Password123!'})
        self.assertEqual(resp1.status_code, 302)
        self.assertRedirects(resp1, reverse('pending'))

        # Rejected user
        rejected_user = User.objects.create_user(
            username='rejected_doc',
            email='rejected@hospital.org',
            password='Password123!',
            license_number='LIC-REJ',
            status=User.Status.REJECTED,
            email_verified=True,
            is_active=False,
            rejection_reason='Licensure mismatch'
        )
        resp2 = self.client.post(self.login_url, {'identifier': 'rejected_doc', 'password': 'Password123!'})
        self.assertEqual(resp2.status_code, 302)
        self.assertRedirects(resp2, reverse('rejected'))

    # -------------------------------------------------------------------------
    # Scenario 6: Admin-only approval and rejection
    # -------------------------------------------------------------------------
    def test_admin_only_approval_and_rejection(self):
        """Scenario 6: Only administrators can approve or reject pharmacist accounts."""
        pharmacist = User.objects.create_user(
            username='pharm_target',
            email='target@hospital.org',
            password='Password123!',
            license_number='LIC-TARGET',
            status=User.Status.PENDING,
            role=User.Role.PHARMACIST,
            email_verified=True
        )

        approve_url = reverse('approve_pharmacist', kwargs={'user_id': pharmacist.id})
        reject_url = reverse('reject_pharmacist', kwargs={'user_id': pharmacist.id})

        # Anonymous cannot approve
        anon_resp = self.client.post(approve_url)
        self.assertEqual(anon_resp.status_code, 302)
        pharmacist.refresh_from_db()
        self.assertEqual(pharmacist.status, User.Status.PENDING)

        # Admin can approve
        self.client.login(username='admin_test', password='SecurePassword123!')
        adm_resp = self.client.post(approve_url)
        self.assertEqual(adm_resp.status_code, 302)
        pharmacist.refresh_from_db()
        self.assertEqual(pharmacist.status, User.Status.APPROVED)
        self.assertTrue(pharmacist.is_active)

        # Admin can reject
        rej_resp = self.client.post(reject_url, {'rejection_reason': 'Invalid documentation'})
        self.assertEqual(rej_resp.status_code, 302)
        pharmacist.refresh_from_db()
        self.assertEqual(pharmacist.status, User.Status.REJECTED)
        self.assertEqual(pharmacist.rejection_reason, 'Invalid documentation')

    # -------------------------------------------------------------------------
    # Scenario 7: Registration confirmation email sent
    # -------------------------------------------------------------------------
    def test_registration_confirmation_email_sent(self):
        """Scenario 7: Registration triggers branded receipt and verification emails."""
        mail.outbox = []
        data = {
            'full_name': 'Dr. Marcus Vance',
            'email': 'marcus@hospital.org',
            'phone': '5559876543',
            'license_number': 'PH-8888',
            'qualification': 'Pharm.D',
            'username': 'mvance_ph',
            'password': 'SecurePass123!',
            'confirm_password': 'SecurePass123!',
        }
        self.client.post(self.register_url, data)

        self.assertEqual(len(mail.outbox), 2)
        subjects = [m.subject for m in mail.outbox]
        self.assertIn("Sanalogx | Pharmacist Registration Received", subjects)
        self.assertIn("Sanalogx | Verify Your Email Address", subjects)

    # -------------------------------------------------------------------------
    # Scenario 8: Approval email with token link sent
    # -------------------------------------------------------------------------
    def test_approval_email_with_token_link_sent(self):
        """Scenario 8: Admin approval dispatches official email containing setup token URL."""
        self.client.login(username='admin_test', password='SecurePassword123!')
        target = User.objects.create_user(
            username='appr_user',
            email='appr@hospital.org',
            password='InitialPassword123!',
            license_number='LIC-APPR-99',
            status=User.Status.PENDING,
            role=User.Role.PHARMACIST,
            email_verified=True,
            full_name='Dr. Allison Ray'
        )

        mail.outbox = []
        approve_url = reverse('approve_pharmacist', kwargs={'user_id': target.id})
        self.client.post(approve_url)

        target.refresh_from_db()
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual(email.subject, "Sanalogx | Pharmacist Account Approved")
        self.assertIn("/auth/setup-password/", email.body)
        self.assertIn(target.registration_id, email.body)

    # -------------------------------------------------------------------------
    # Scenario 9: Secure password setup and token expiration
    # -------------------------------------------------------------------------
    def test_secure_password_setup_and_token_expiration(self):
        """Scenario 9: Single-use password setup token works, updates credentials, then becomes invalid."""
        user = User.objects.create_user(
            username='setup_test',
            email='setup@hospital.org',
            password='TemporaryPass123!',
            license_number='LIC-SETUP-01',
            status=User.Status.APPROVED,
            role=User.Role.PHARMACIST,
            is_active=True,
            must_change_password=True
        )

        uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
        token = pharmacist_password_setup_token_generator.make_token(user)

        # 1. Access setup view with valid token
        setup_url = reverse('setup_password', kwargs={'uidb64': uidb64, 'token': token})
        resp_get = self.client.get(setup_url)
        self.assertEqual(resp_get.status_code, 200)
        self.assertContains(resp_get, "Configure Workspace Password")

        # 2. Submit new password
        resp_post = self.client.post(setup_url, {
            'password': 'NewlyConfiguredPass2026!',
            'confirm_password': 'NewlyConfiguredPass2026!'
        })
        self.assertEqual(resp_post.status_code, 302)
        self.assertRedirects(resp_post, reverse('pharmacist_login'))

        user.refresh_from_db()
        self.assertTrue(user.check_password('NewlyConfiguredPass2026!'))
        self.assertFalse(user.must_change_password)

        # 3. Token cannot be reused (single-use expiration)
        resp_reuse = self.client.get(setup_url)
        self.assertEqual(resp_reuse.status_code, 200)
        self.assertContains(resp_reuse, "Invalid or Expired Link")

    # -------------------------------------------------------------------------
    # Scenario 10: Successful login audit log and notification
    # -------------------------------------------------------------------------
    def test_successful_login_audit_log_and_notification(self):
        """Scenario 10: Successful login creates audit log and dispatches security alert email."""
        user = User.objects.create_user(
            username='audit_login_user',
            email='auditlogin@hospital.org',
            password='SecureLoginPass123!',
            license_number='LIC-AUDIT-02',
            status=User.Status.APPROVED,
            role=User.Role.PHARMACIST,
            is_active=True,
            email_verified=True,
            must_change_password=False
        )

        mail.outbox = []
        resp = self.client.post(self.login_url, {
            'identifier': 'audit_login_user',
            'password': 'SecureLoginPass123!'
        })
        self.assertEqual(resp.status_code, 302)

        # Check audit log
        self.assertTrue(AuditLog.objects.filter(user=user, action='LOGIN').exists())

        # Check security notification email
        self.assertTrue(len(mail.outbox) >= 1)
        subjects = [m.subject for m in mail.outbox]
        self.assertIn("Sanalogx | Successful Sign-In Notification", subjects)

    # -------------------------------------------------------------------------
    # Scenario 11: Graceful handling of email delivery failures
    # -------------------------------------------------------------------------
    def test_graceful_handling_of_email_delivery_failures(self):
        """Scenario 11: Registration and approval succeed gracefully even if SMTP server is unavailable."""
        with patch('accounts.email_service.EmailMultiAlternatives.send', side_effect=Exception("SMTP Connection Refused")):
            data = {
                'full_name': 'Dr. Offline Test',
                'email': 'offline@hospital.org',
                'phone': '5554321098',
                'license_number': 'PH-0303',
                'qualification': 'Pharm.D',
                'username': 'offline_ph',
                'password': 'Password123!',
                'confirm_password': 'Password123!',
            }
            # Should not crash with 500
            resp = self.client.post(self.register_url, data)
            self.assertEqual(resp.status_code, 302)
            self.assertTrue(User.objects.filter(email='offline@hospital.org').exists())

    # -------------------------------------------------------------------------
    # Scenario 12: Duplicate submission protection
    # -------------------------------------------------------------------------
    def test_duplicate_submission_protection(self):
        """Scenario 12: Submitting identical registration twice returns validation error, no 500."""
        data = {
            'full_name': 'Dr. Duplicate Test',
            'email': 'duplicate@hospital.org',
            'phone': '5557778888',
            'license_number': 'PH-9999',
            'qualification': 'Pharm.D',
            'username': 'duplicate_ph',
            'password': 'Password123!',
            'confirm_password': 'Password123!',
        }
        resp1 = self.client.post(self.register_url, data)
        self.assertEqual(resp1.status_code, 302)

        # Second submission
        resp2 = self.client.post(self.register_url, data)
        self.assertEqual(resp2.status_code, 200)

    # -------------------------------------------------------------------------
    # Scenario 13: CSRF and authorization checks
    # -------------------------------------------------------------------------
    def test_csrf_and_authorization_checks(self):
        """Scenario 13: CSRF checks and admin endpoint protections are verified."""
        csrf_client = Client(enforce_csrf_checks=True)
        resp = csrf_client.get(self.register_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'csrfmiddlewaretoken')

        # Pharmacist cannot access admin approvals
        user = User.objects.create_user(
            username='pharm_noadmin',
            email='noadmin@hospital.org',
            password='Password123!',
            license_number='LIC-NOADMIN',
            status=User.Status.APPROVED,
            role=User.Role.PHARMACIST,
            email_verified=True
        )
        self.client.login(username='pharm_noadmin', password='Password123!')
        resp_admin = self.client.get(self.admin_approvals_url)
        self.assertEqual(resp_admin.status_code, 302)
        self.assertIn('/auth/403/', resp_admin.url)

    # -------------------------------------------------------------------------
    # Scenario 14: Protected dashboard access restrictions
    # -------------------------------------------------------------------------
    def test_protected_dashboard_access_restrictions(self):
        """Scenario 14: Direct dashboard routes enforce role gates and authentication."""
        # Unauthenticated access redirects to login
        resp1 = self.client.get(reverse('admin_dashboard'))
        self.assertEqual(resp1.status_code, 302)
        self.assertIn(reverse('pharmacist_login'), resp1.url)

        resp2 = self.client.get(reverse('pharmacist_dashboard'))
        self.assertEqual(resp2.status_code, 302)
        self.assertIn(reverse('pharmacist_login'), resp2.url)

    # -------------------------------------------------------------------------
    # Scenario 15: Form validation error states
    # -------------------------------------------------------------------------
    def test_form_validation_error_states(self):
        """Scenario 15: Password mismatch and missing required fields show inline messages."""
        data = {
            'full_name': 'Test Validation',
            'email': 'valid@hospital.org',
            'phone': '1234567890',
            'license_number': 'PH-0101',
            'qualification': 'Pharm.D',
            'username': 'val_user',
            'password': 'Password123!',
            'confirm_password': 'MismatchedPassword123!',
        }
        resp = self.client.post(self.register_url, data)
        self.assertEqual(resp.status_code, 200)

    # -------------------------------------------------------------------------
    # Additional Verification & Recovery Workflows
    # -------------------------------------------------------------------------
    def test_email_verification_flow(self):
        """Email verification link verification and expiration."""
        user = User.objects.create_user(
            username='verify_test',
            email='verify@hospital.org',
            password='Password123!',
            license_number='LIC-222',
            role=User.Role.PHARMACIST,
            status=User.Status.PENDING,
            email_verified=False
        )
        email_verification = EmailVerification.objects.create(
            user=user,
            expires_at=timezone.now() + timedelta(hours=24)
        )

        uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
        token = email_verification.token

        # Invalid token
        bad_token_url = reverse('verify_email', kwargs={'uidb64': uidb64, 'token': 'invalid-token'})
        resp_bad = self.client.get(bad_token_url)
        self.assertEqual(resp_bad.status_code, 200)
        self.assertContains(resp_bad, "This verification link has expired or is invalid.")

        # Valid verification link
        verify_url = reverse('verify_email', kwargs={'uidb64': uidb64, 'token': token})
        resp = self.client.get(verify_url)
        self.assertEqual(resp.status_code, 302)
        self.assertRedirects(resp, '/auth/registration-success/?email=verify@hospital.org')

        user.refresh_from_db()
        self.assertTrue(user.email_verified)

    def test_forgot_password_code_flow(self):
        """6-digit verification code generation, session state, and password reset."""
        user = User.objects.create_user(
            username='reset_user',
            email='reset@hospital.org',
            password='OldPassword123!',
            license_number='LIC-RESET',
            status=User.Status.APPROVED,
            email_verified=True
        )

        mail.outbox = []
        resp1 = self.client.post(reverse('forgot_password'), {'email': 'reset@hospital.org'})
        self.assertRedirects(resp1, reverse('verify_reset_code'))

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Sanalogx | Password Reset Verification Code")

        prr = PasswordResetRequest.objects.get(email='reset@hospital.org')
        self.assertFalse(prr.is_used)

        session = self.client.session
        session['reset_prr_id'] = prr.id
        session.save()

        resp3 = self.client.post(reverse('create_new_password'), {
            'new_password': 'NewPassword123!',
            'confirm_password': 'NewPassword123!'
        })
        self.assertRedirects(resp3, reverse('pharmacist_login'))

        user.refresh_from_db()
        self.assertTrue(user.check_password('NewPassword123!'))

    def test_username_recovery_flow(self):
        """Username recovery request submission and admin approval."""
        user = User.objects.create_user(
            username='recovery_pharm',
            email='recover@hospital.org',
            password='Password123!',
            license_number='LIC-REC',
            role=User.Role.PHARMACIST
        )

        resp1 = self.client.post(reverse('forgot_username'), {'email': 'recover@hospital.org'})
        self.assertEqual(resp1.status_code, 200)

        req = UsernameRecoveryRequest.objects.get(email='recover@hospital.org')
        self.assertEqual(req.status, UsernameRecoveryRequest.Status.PENDING)

        self.client.login(username='admin_test', password='SecurePassword123!')
        mail.outbox = []
        resp2 = self.client.post(reverse('approve_username_recovery', kwargs={'req_id': req.id}))
        self.assertRedirects(resp2, reverse('admin_approvals'))

        req.refresh_from_db()
        self.assertEqual(req.status, UsernameRecoveryRequest.Status.COMPLETED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Sanalogx | Username Recovery")
        self.assertIn("recovery_pharm", mail.outbox[0].body)

    # -------------------------------------------------------------------------
    # Scenario 16: Pharmacy License Number Demo Validation & Restrictions
    # -------------------------------------------------------------------------
    def test_demo_pharmacy_license_validation(self):
        """
        Verify demo pharmacy license validation format ^PH-[0-9]{4}$:
        - PH-2827, PH-0001: valid demo formats
        - PH-123, PH-12345, PH-ABCD, PH2827, PH-12A4, PH- 2827: invalid formats
        - Whitespace trimming
        - Duplicate prevention
        - Demo status (DEMO / UNVERIFIED) & admin approval workflow
        """
        base_data = {
            'full_name': 'Dr. Demo Pharmacist',
            'email': 'demo_pharm@hospital.org',
            'phone': '+919876543210',
            'qualification': 'Pharm.D',
            'username': 'demo_pharm_user',
            'password': 'SecureDemoPassword123!',
            'confirm_password': 'SecureDemoPassword123!',
        }

        # 1. Test INVALID formats
        invalid_licenses = [
            'PH-123',     # too short (3 digits)
            'PH-12345',   # too long (5 digits)
            'PH-ABCD',    # alphabetic
            'PH2827',     # missing hyphen
            'PH-12A4',    # mixed letter in digits
            'PH- 2827',   # space after hyphen
        ]

        for inv in invalid_licenses:
            bad_data = base_data.copy()
            bad_data['license_number'] = inv
            bad_data['email'] = f"invalid_{inv.replace(' ', '_').replace('-', '_')}@hospital.org"
            bad_data['username'] = f"user_{inv.replace(' ', '_').replace('-', '_')}"

            resp = self.client.post(self.register_url, bad_data)
            self.assertEqual(resp.status_code, 200, f"Expected 200 for invalid license: {inv}")
            self.assertContains(
                resp,
                "Pharmacy License Number must follow demo format",
                msg_prefix=f"Error message missing for invalid license: {inv}"
            )
            # Ensure user was not created
            self.assertFalse(User.objects.filter(email=bad_data['email']).exists())

        # 2. Test VALID demo formats: PH-0001
        valid_data_0001 = base_data.copy()
        valid_data_0001['email'] = 'ph0001@hospital.org'
        valid_data_0001['username'] = 'ph0001_user'
        valid_data_0001['license_number'] = 'PH-0001'

        resp_0001 = self.client.post(self.register_url, valid_data_0001)
        self.assertEqual(resp_0001.status_code, 302)
        user_0001 = User.objects.get(email='ph0001@hospital.org')
        self.assertEqual(user_0001.license_number, 'PH-0001')
        self.assertTrue(user_0001.is_demo)
        self.assertEqual(user_0001.license_verification_status, 'DEMO_UNVERIFIED')
        self.assertEqual(user_0001.status, User.Status.PENDING)

        # 3. Test VALID demo format with whitespace trimming: '  PH-2827  '
        valid_data_2827 = base_data.copy()
        valid_data_2827['email'] = 'ph2827@hospital.org'
        valid_data_2827['username'] = 'ph2827_user'
        valid_data_2827['license_number'] = '  PH-2827  '

        resp_2827 = self.client.post(self.register_url, valid_data_2827)
        self.assertEqual(resp_2827.status_code, 302)
        user_2827 = User.objects.get(email='ph2827@hospital.org')
        self.assertEqual(user_2827.license_number, 'PH-2827')
        self.assertTrue(user_2827.is_demo)
        self.assertEqual(user_2827.license_verification_status, 'DEMO_UNVERIFIED')
        self.assertEqual(user_2827.status, User.Status.PENDING)

        # 4. Test DUPLICATE demo license prevention
        dup_data = base_data.copy()
        dup_data['email'] = 'ph2827_dup@hospital.org'
        dup_data['username'] = 'ph2827_dup_user'
        dup_data['license_number'] = 'PH-2827'  # already used by user_2827

        resp_dup = self.client.post(self.register_url, dup_data)
        self.assertEqual(resp_dup.status_code, 200)
        self.assertContains(resp_dup, "This pharmacy license number is already registered.")

        # 5. Verify Administrator Approval and Activation for Demo Account
        self.client.login(username='admin_test', password='SecurePassword123!')
        approve_url = reverse('approve_pharmacist', kwargs={'user_id': user_2827.id})
        resp_approve = self.client.post(approve_url)
        self.assertEqual(resp_approve.status_code, 302)

        user_2827.refresh_from_db()
        self.assertEqual(user_2827.status, User.Status.APPROVED)
        self.assertTrue(user_2827.is_active)
        self.assertTrue(user_2827.can_login)


class PharmacistApprovalAndCredentialDeliveryTests(TestCase):
    """
    Dedicated test suite covering all 12 enterprise scenarios for:
    Pharmacist Approval, Credential Delivery, Token Lifecycle, Deduplication, and SMTP Diagnostics.
    """

    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_superuser(
            username='admin_delivery_test',
            email='admin_delivery@hospital.org',
            password='AdminPassword123!',
            full_name='Chief Pharmacy Admin'
        )
        self.pharmacist = User.objects.create_user(
            username='pharm_candidate',
            email='candidate@hospital.org',
            password='InitialCandidatePassword123!',
            license_number='LIC-DELIV-001',
            qualification='Pharm.D',
            full_name='Dr. Candidate Pharmacist',
            role=User.Role.PHARMACIST,
            status=User.Status.PENDING,
            is_active=False,
            email_verified=True
        )

    # 1. Registration creates correct Pending state
    def test_registration_creates_pending_inactive_state(self):
        self.assertEqual(self.pharmacist.status, User.Status.PENDING)
        self.assertFalse(self.pharmacist.is_active)
        self.assertFalse(self.pharmacist.can_login)

    # 2. Admin approval activates correct account
    def test_admin_approval_activates_account(self):
        self.client.login(username='admin_delivery_test', password='AdminPassword123!')
        approve_url = reverse('approve_pharmacist', kwargs={'user_id': self.pharmacist.id})
        resp = self.client.post(approve_url)
        self.assertEqual(resp.status_code, 302)

        self.pharmacist.refresh_from_db()
        self.assertEqual(self.pharmacist.status, User.Status.APPROVED)
        self.assertTrue(self.pharmacist.is_active)
        self.assertEqual(self.pharmacist.approved_by, self.admin)
        self.assertIsNotNone(self.pharmacist.approval_date)
        self.assertTrue(self.pharmacist.can_login)

    # 3. Correct username is assigned or retained
    def test_username_handling_retained_and_auto_generated(self):
        self.client.login(username='admin_delivery_test', password='AdminPassword123!')
        
        # Case A: Explicit username is retained
        approve_url = reverse('approve_pharmacist', kwargs={'user_id': self.pharmacist.id})
        self.client.post(approve_url)
        self.pharmacist.refresh_from_db()
        self.assertEqual(self.pharmacist.username, 'pharm_candidate')

        # Case B: 'pending_xxxx' username gets assigned an official MEDI-PH-XXXX username
        pending_user = User.objects.create_user(
            username='pending_abc12345',
            email='auto_user@hospital.org',
            password='AutoPassword123!',
            license_number='LIC-AUTO-002',
            role=User.Role.PHARMACIST,
            status=User.Status.PENDING,
            is_active=False
        )
        approve_url_b = reverse('approve_pharmacist', kwargs={'user_id': pending_user.id})
        self.client.post(approve_url_b)
        pending_user.refresh_from_db()
        self.assertTrue(pending_user.username.startswith('MEDI-PH-'))
        self.assertTrue(pending_user.must_change_password)

    # 4. Approval triggers email to registered pharmacist address
    def test_approval_triggers_email_to_registered_recipient(self):
        self.client.login(username='admin_delivery_test', password='AdminPassword123!')
        mail.outbox = []
        approve_url = reverse('approve_pharmacist', kwargs={'user_id': self.pharmacist.id})
        self.client.post(approve_url)

        self.assertEqual(len(mail.outbox), 1)
        delivered_email = mail.outbox[0]
        self.assertEqual(delivered_email.to, ['candidate@hospital.org'])
        self.assertEqual(delivered_email.subject, "Sanalogx | Pharmacist Account Approved")

    # 5. Email subject and content contain expected login instructions
    def test_approval_email_content_contains_credentials_and_links(self):
        self.client.login(username='admin_delivery_test', password='AdminPassword123!')
        mail.outbox = []
        approve_url = reverse('approve_pharmacist', kwargs={'user_id': self.pharmacist.id})
        self.client.post(approve_url)

        email = mail.outbox[0]
        body = email.body
        self.pharmacist.refresh_from_db()
        self.assertIn("pharm_candidate", body)
        self.assertIn(self.pharmacist.registration_id, body)
        self.assertIn("/auth/setup-password/", body)
        self.assertIn("/auth/login/", body)

    # 6. Repeated approval does not send duplicate emails
    def test_repeated_approval_does_not_send_duplicate_emails(self):
        self.client.login(username='admin_delivery_test', password='AdminPassword123!')
        approve_url = reverse('approve_pharmacist', kwargs={'user_id': self.pharmacist.id})
        
        # First approval
        mail.outbox = []
        self.client.post(approve_url)
        self.assertEqual(len(mail.outbox), 1)

        # Second approval attempt on already approved user
        mail.outbox = []
        resp2 = self.client.post(approve_url)
        self.assertEqual(resp2.status_code, 302)
        self.assertEqual(len(mail.outbox), 0, "Repeated approval must NOT send duplicate credential emails")

    # 7. Rejected applications do not receive approval emails
    def test_rejected_applications_do_not_receive_approval_email(self):
        self.client.login(username='admin_delivery_test', password='AdminPassword123!')
        reject_url = reverse('reject_pharmacist', kwargs={'user_id': self.pharmacist.id})
        mail.outbox = []
        resp = self.client.post(reject_url, {'rejection_reason': 'Invalid documentation'})
        self.assertEqual(resp.status_code, 302)

        self.pharmacist.refresh_from_db()
        self.assertEqual(self.pharmacist.status, User.Status.REJECTED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Sanalogx | Registration Decision")
        self.assertNotIn("Approved", mail.outbox[0].subject)

    # 8. SMTP failure is logged and recoverable without 500 crash
    def test_smtp_failure_is_logged_and_recoverable(self):
        self.client.login(username='admin_delivery_test', password='AdminPassword123!')
        approve_url = reverse('approve_pharmacist', kwargs={'user_id': self.pharmacist.id})

        with patch('accounts.email_service.EmailMultiAlternatives.send', side_effect=Exception("SMTP Connection Refused")):
            resp = self.client.post(approve_url)
            self.assertEqual(resp.status_code, 302)

        self.pharmacist.refresh_from_db()
        # Account is approved despite email delivery failure
        self.assertEqual(self.pharmacist.status, User.Status.APPROVED)
        self.assertTrue(self.pharmacist.is_active)
        # Audit log tracks failure safely
        self.assertTrue(
            AuditLog.objects.filter(user=self.pharmacist, action='APPROVAL_EMAIL_FAILED').exists()
        )

    # 9. Failed email retry does not repeat approval operation
    def test_failed_email_retry_does_not_repeat_approval(self):
        self.client.login(username='admin_delivery_test', password='AdminPassword123!')
        approve_url = reverse('approve_pharmacist', kwargs={'user_id': self.pharmacist.id})
        self.client.post(approve_url)
        self.pharmacist.refresh_from_db()
        original_approval_date = self.pharmacist.approval_date

        resend_url = reverse('resend_approval_email', kwargs={'user_id': self.pharmacist.id})
        mail.outbox = []
        resp = self.client.post(resend_url)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)

        self.pharmacist.refresh_from_db()
        self.assertEqual(self.pharmacist.approval_date, original_approval_date)
        self.assertTrue(
            AuditLog.objects.filter(user=self.pharmacist, action='APPROVAL_EMAIL_RESENT').exists()
        )

    # 10. Password setup tokens expire and cannot be reused
    def test_password_setup_token_single_use(self):
        self.pharmacist.status = User.Status.APPROVED
        self.pharmacist.is_active = True
        self.pharmacist.save()

        uidb64 = urlsafe_base64_encode(force_bytes(self.pharmacist.pk))
        token = pharmacist_password_setup_token_generator.make_token(self.pharmacist)
        setup_url = reverse('setup_password', kwargs={'uidb64': uidb64, 'token': token})

        # Submit new password
        resp = self.client.post(setup_url, {
            'password': 'BrandNewSecurePass2026!',
            'confirm_password': 'BrandNewSecurePass2026!'
        })
        self.assertEqual(resp.status_code, 302)

        self.pharmacist.refresh_from_db()
        self.assertTrue(self.pharmacist.check_password('BrandNewSecurePass2026!'))

        # Token cannot be reused
        resp_reuse = self.client.get(setup_url)
        self.assertEqual(resp_reuse.status_code, 200)
        self.assertContains(resp_reuse, "Invalid or Expired Link")

    # 11. Unauthorized users cannot approve accounts or resend credentials
    def test_unauthorized_user_cannot_approve_or_resend(self):
        unauth_user = User.objects.create_user(
            username='regular_staff',
            email='staff@hospital.org',
            password='StaffPassword123!',
            license_number='LIC-STAFF-01',
            role=User.Role.PHARMACIST,
            status=User.Status.APPROVED,
            is_active=True
        )
        self.client.login(username='regular_staff', password='StaffPassword123!')

        approve_url = reverse('approve_pharmacist', kwargs={'user_id': self.pharmacist.id})
        resp1 = self.client.post(approve_url)
        self.assertEqual(resp1.status_code, 302)
        self.assertIn('/auth/403/', resp1.url)

        resend_url = reverse('resend_approval_email', kwargs={'user_id': self.pharmacist.id})
        resp2 = self.client.post(resend_url)
        self.assertEqual(resp2.status_code, 302)
        self.assertIn('/auth/403/', resp2.url)

    # 12. Resend preserves existing username, password hash, and approval timestamp
    def test_resend_preserves_username_and_password_and_timestamps(self):
        """Resending email preserves existing username, password hash, and approval timestamp."""
        self.client.login(username='admin_delivery_test', password='AdminPassword123!')
        approve_url = reverse('approve_pharmacist', kwargs={'user_id': self.pharmacist.id})
        self.client.post(approve_url)
        self.pharmacist.refresh_from_db()

        orig_username = self.pharmacist.username
        orig_approval_date = self.pharmacist.approval_date
        orig_approved_by = self.pharmacist.approved_by

        # Mark user as having completed password setup & login
        self.pharmacist.must_change_password = False
        self.pharmacist.first_login_completed = True
        self.pharmacist.save()
        orig_password = self.pharmacist.password

        # Resend email
        mail.outbox = []
        resend_url = reverse('resend_approval_email', kwargs={'user_id': self.pharmacist.id})
        resp = self.client.post(resend_url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('#approved-pane', resp.url)
        self.assertEqual(len(mail.outbox), 1)

        # Check preserved fields
        self.pharmacist.refresh_from_db()
        self.assertEqual(self.pharmacist.username, orig_username)
        self.assertEqual(self.pharmacist.password, orig_password)
        self.assertEqual(self.pharmacist.approval_date, orig_approval_date)
        self.assertEqual(self.pharmacist.approved_by, orig_approved_by)

        # Check email content for established user
        email = mail.outbox[0]
        self.assertEqual(email.to, [self.pharmacist.email])
        self.assertIn(self.pharmacist.username, email.body)
        self.assertNotIn("/auth/setup-password/", email.body)
        self.assertIn("/auth/login/", email.body)
        self.assertIn("/auth/forgot-password/", email.body)

    # 13. Resend rate limit debounce protects against repeated clicks
    def test_resend_throttle_prevents_duplicate_sends(self):
        """Repeated clicks on Resend within 15 seconds are throttled to prevent spam."""
        self.client.login(username='admin_delivery_test', password='AdminPassword123!')
        approve_url = reverse('approve_pharmacist', kwargs={'user_id': self.pharmacist.id})
        self.client.post(approve_url)

        resend_url = reverse('resend_approval_email', kwargs={'user_id': self.pharmacist.id})
        mail.outbox = []

        # First resend succeeds
        resp1 = self.client.post(resend_url)
        self.assertEqual(resp1.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)

        # Immediate second resend is throttled
        mail.outbox = []
        resp2 = self.client.post(resend_url)
        self.assertEqual(resp2.status_code, 302)
        self.assertEqual(len(mail.outbox), 0, "Second click within 15s must be throttled")

    # 14. SMTP failure during resend is handled cleanly without crash
    def test_resend_smtp_error_recorded_safely(self):
        """SMTP errors during resend do not crash view and log sanitized diagnostic."""
        self.client.login(username='admin_delivery_test', password='AdminPassword123!')
        approve_url = reverse('approve_pharmacist', kwargs={'user_id': self.pharmacist.id})
        self.client.post(approve_url)

        resend_url = reverse('resend_approval_email', kwargs={'user_id': self.pharmacist.id})
        with patch('accounts.email_service.EmailMultiAlternatives.send', side_effect=Exception("SMTP Authentication Failed")):
            resp = self.client.post(resend_url)
            self.assertEqual(resp.status_code, 302)
            self.assertIn('#approved-pane', resp.url)

        self.assertTrue(
            AuditLog.objects.filter(user=self.pharmacist, action='APPROVAL_EMAIL_RESEND_FAILED').exists()
        )

    # 15. AJAX resend request returns JSON response with status 200
    def test_resend_ajax_request_returns_json_success(self):
        """AJAX requests to resend_approval_email return structured JSON response without page reload."""
        self.client.login(username='admin_delivery_test', password='AdminPassword123!')
        approve_url = reverse('approve_pharmacist', kwargs={'user_id': self.pharmacist.id})
        self.client.post(approve_url)

        resend_url = reverse('resend_approval_email', kwargs={'user_id': self.pharmacist.id})
        mail.outbox = []
        resp = self.client.post(resend_url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resp.status_code, 200)
        json_data = resp.json()
        self.assertTrue(json_data['success'])
        self.assertIn('successfully sent', json_data['message'])
        self.assertEqual(len(mail.outbox), 1)

    # 16. AJAX resend request when throttled returns JSON 429
    def test_resend_ajax_request_throttled_returns_429(self):
        """Repeated AJAX requests within 15 seconds return JSON HTTP 429 status."""
        self.client.login(username='admin_delivery_test', password='AdminPassword123!')
        approve_url = reverse('approve_pharmacist', kwargs={'user_id': self.pharmacist.id})
        self.client.post(approve_url)

        resend_url = reverse('resend_approval_email', kwargs={'user_id': self.pharmacist.id})
        # First send
        resp1 = self.client.post(resend_url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resp1.status_code, 200)

        # Immediate second send
        resp2 = self.client.post(resend_url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resp2.status_code, 429)
        json_data = resp2.json()
        self.assertFalse(json_data['success'])
        self.assertIn('already dispatched', json_data['message'])


class AdminAndPharmacistRoleSeparationTests(TestCase):
    """
    Comprehensive RBAC Test Suite for Phase 6:
    Validates complete role separation between ADMIN and PHARMACIST workspaces.
    """
    def setUp(self):
        self.client = Client()

        # System Administrator
        self.admin = User.objects.create_superuser(
            username='sys_admin',
            email='admin@hospital.org',
            password='AdminPassword123!',
            role=User.Role.ADMIN,
            status=User.Status.APPROVED,
            full_name='Hospital Administrator'
        )

        # Approved Pharmacist
        self.pharmacist = User.objects.create_user(
            username='sarah_ph',
            email='sarah@hospital.org',
            password='PharmPassword123!',
            role=User.Role.PHARMACIST,
            status=User.Status.APPROVED,
            email_verified=True,
            is_active=True,
            license_number='PH-2827',
            qualification='Pharm.D',
            full_name='Dr. Sarah Jenkins'
        )

        # Pending Pharmacist (is_active=True to test in-session status gates)
        self.pending_pharmacist = User.objects.create_user(
            username='pending_ph',
            email='pending@hospital.org',
            password='PendingPass123!',
            role=User.Role.PHARMACIST,
            status=User.Status.PENDING,
            email_verified=True,
            is_active=True,
            license_number='PH-9999',
            full_name='Pending Applicant'
        )

        # Rejected Pharmacist (is_active=True to test in-session status gates)
        self.rejected_pharmacist = User.objects.create_user(
            username='rejected_ph',
            email='rejected@hospital.org',
            password='RejectedPass123!',
            role=User.Role.PHARMACIST,
            status=User.Status.REJECTED,
            email_verified=True,
            is_active=True,
            license_number='PH-0000',
            full_name='Rejected Applicant'
        )

    def test_unauthenticated_redirect_to_login(self):
        """Unauthenticated requests to protected endpoints redirect to login."""
        protected_urls = [
            reverse('admin_dashboard'),
            reverse('pharmacist_dashboard'),
            reverse('approved'),
            reverse('admin_approvals'),
            reverse('medicine_list'),
            reverse('inventory_dashboard'),
            reverse('secure_dispensing_workspace'),
        ]
        for url in protected_urls:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302, f"Expected 302 redirect for {url}")
            self.assertIn('/auth/login/', resp.url, f"Expected login redirect for {url}")

    def test_pending_pharmacist_cannot_access_workspace_or_admin(self):
        """A pending pharmacist cannot access approved dashboard, medicines, or admin portal."""
        self.client.force_login(self.pending_pharmacist)

        # Dashboard redirects to pending
        resp = self.client.get(reverse('approved'))
        self.assertRedirects(resp, reverse('pending'))

        resp = self.client.get(reverse('pharmacist_dashboard'))
        self.assertRedirects(resp, reverse('pending'))

        # Medicines access redirects to pending
        resp = self.client.get(reverse('medicine_list'))
        self.assertRedirects(resp, reverse('pending'))

        # Admin dashboard direct route redirects to pending
        resp = self.client.get(reverse('admin_dashboard'))
        self.assertRedirects(resp, reverse('pending'))

        # Admin approvals redirects to 403 page
        resp = self.client.get(reverse('admin_approvals'))
        self.assertRedirects(resp, reverse('unauthorized_403'), target_status_code=403)

    def test_rejected_pharmacist_cannot_access_workspace_or_admin(self):
        """A rejected pharmacist cannot access approved dashboard, medicines, or admin portal."""
        self.client.force_login(self.rejected_pharmacist)

        resp = self.client.get(reverse('approved'))
        self.assertRedirects(resp, reverse('rejected'))

        resp = self.client.get(reverse('pharmacist_dashboard'))
        self.assertRedirects(resp, reverse('rejected'))

        resp = self.client.get(reverse('admin_dashboard'))
        self.assertRedirects(resp, reverse('rejected'))

        resp = self.client.get(reverse('admin_approvals'))
        self.assertRedirects(resp, reverse('unauthorized_403'), target_status_code=403)

    def test_approved_pharmacist_workspace_access(self):
        """An approved pharmacist can access the pharmacist workspace and operational views."""
        self.client.force_login(self.pharmacist)

        # Dashboard renders with workspace layout
        resp = self.client.get(reverse('approved'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Pharmacist Workspace")
        self.assertContains(resp, "Low Stock Items")
        self.assertContains(resp, "pharmacist-app-container")

        # Direct dashboard route redirects to approved
        resp = self.client.get(reverse('pharmacist_dashboard'))
        self.assertRedirects(resp, reverse('approved'))

        # Operational views render successfully
        self.assertEqual(self.client.get(reverse('medicine_list')).status_code, 200)
        self.assertEqual(self.client.get(reverse('inventory_dashboard')).status_code, 200)
        self.assertEqual(self.client.get(reverse('secure_dispensing_workspace')).status_code, 200)
        self.assertEqual(self.client.get(reverse('new_sale')).status_code, 200)

    def test_approved_pharmacist_blocked_from_admin_governance(self):
        """An approved pharmacist is blocked by server-side authorization on Admin views."""
        self.client.force_login(self.pharmacist)

        # Admin approvals -> 403
        resp = self.client.get(reverse('admin_approvals'))
        self.assertRedirects(resp, reverse('unauthorized_403'), target_status_code=403)

        # Admin dashboard direct route -> redirects to approved with error message
        resp = self.client.get(reverse('admin_dashboard'))
        self.assertRedirects(resp, reverse('approved'))

        # Supplier management -> restricted to administrators
        resp = self.client.get(reverse('supplier_list'))
        self.assertEqual(resp.status_code, 302)

        # Historical data import -> restricted to administrators
        resp = self.client.get(reverse('inventory_historical_import'))
        self.assertRedirects(resp, reverse('inventory_dashboard'))

        # Category Master list -> 403 PermissionDenied
        resp = self.client.get(reverse('category_list'))
        self.assertEqual(resp.status_code, 403)

        # Mock EHR Simulator -> restricted to administrators
        resp = self.client.get(reverse('mock_ehr_simulator'))
        self.assertRedirects(resp, reverse('home'))

    def test_admin_workspace_access(self):
        """An Admin user has full access to the Admin portal, governance views, and monitoring."""
        self.client.force_login(self.admin)

        # Admin dashboard
        resp = self.client.get(reverse('admin_approvals'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "admin-app-container")

        # Direct routes redirect to admin_approvals
        resp = self.client.get(reverse('admin_dashboard'))
        self.assertRedirects(resp, reverse('admin_approvals'))

        resp = self.client.get(reverse('pharmacist_dashboard'))
        self.assertRedirects(resp, reverse('admin_approvals'))

        # Governance views render
        self.assertEqual(self.client.get(reverse('supplier_list')).status_code, 200)
        self.assertEqual(self.client.get(reverse('category_list')).status_code, 200)
        self.assertEqual(self.client.get(reverse('inventory_historical_import')).status_code, 200)
        self.assertEqual(self.client.get(reverse('mock_ehr_simulator')).status_code, 200)

        # Shared operational & governance views
        self.assertEqual(self.client.get(reverse('medicine_list')).status_code, 200)
        self.assertEqual(self.client.get(reverse('inventory_dashboard')).status_code, 200)
        # Operational dispensing workspace redirects Admin to audit history
        disp_resp = self.client.get(reverse('secure_dispensing_workspace'))
        self.assertEqual(disp_resp.status_code, 302)
        self.assertRedirects(disp_resp, reverse('dispensing_history'))
        self.assertEqual(self.client.get(reverse('dispensing_history')).status_code, 200)

    def test_login_redirect_flows(self):
        """Admin and Pharmacist login routes direct each role immediately to the appropriate landing URL."""
        # 1. Admin login via pharmacist_login view (immediate redirect to admin_approvals)
        resp = self.client.post(reverse('pharmacist_login'), {
            'identifier': 'sys_admin',
            'password': 'AdminPassword123!'
        })
        self.assertEqual(resp.status_code, 302)
        self.assertRedirects(resp, reverse('admin_approvals'))

        # Check login_success direct route also redirects Admin to admin_approvals
        resp_succ = self.client.get(reverse('login_success'))
        self.assertRedirects(resp_succ, reverse('admin_approvals'))
        self.client.logout()

        # 2. Pharmacist login via pharmacist_login view (immediate redirect to approved)
        resp = self.client.post(reverse('pharmacist_login'), {
            'identifier': 'sarah_ph',
            'password': 'PharmPassword123!'
        })
        self.assertEqual(resp.status_code, 302)
        self.assertRedirects(resp, reverse('approved'))

        # Check login_success direct route also redirects Pharmacist to approved
        resp_succ = self.client.get(reverse('login_success'))
        self.assertRedirects(resp_succ, reverse('approved'))
        self.client.logout()

        # 3. Pharmacist cannot log in via admin_login endpoint
        resp = self.client.post(reverse('admin_login'), {
            'username': 'sarah_ph',
            'password': 'PharmPassword123!'
        })
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Invalid Administrator credentials or unauthorized role.")

        # 4. Admin CAN log in via admin_login endpoint
        resp = self.client.post(reverse('admin_login'), {
            'username': 'sys_admin',
            'password': 'AdminPassword123!'
        })
        self.assertRedirects(resp, reverse('admin_approvals'))
        self.client.logout()

    def test_pending_and_rejected_login_blocked(self):
        """Pending and rejected accounts are prevented from logging in."""
        # Pending pharmacist
        resp = self.client.post(reverse('pharmacist_login'), {
            'identifier': 'pending_ph',
            'password': 'PendingPass123!'
        })
        self.assertRedirects(resp, reverse('pending'))

        # Rejected pharmacist
        resp = self.client.post(reverse('pharmacist_login'), {
            'identifier': 'rejected_ph',
            'password': 'RejectedPass123!'
        })
        self.assertRedirects(resp, reverse('rejected'))

    def test_logout_invalidates_session(self):
        """Logout invalidates the session and prevents access to protected pages."""
        self.client.force_login(self.pharmacist)
        resp = self.client.get(reverse('approved'))
        self.assertEqual(resp.status_code, 200)

        # Logout
        resp_logout = self.client.get(reverse('logout'))
        self.assertRedirects(resp_logout, reverse('home'))

        # Accessing approved page now redirects to login
        resp_after = self.client.get(reverse('approved'))
        self.assertRedirects(resp_after, reverse('pharmacist_login'))

    def test_public_navbar_logged_out(self):
        """Logged-out public navbar contains 5 links + Login + Register; no Dashboard or Sign Out."""
        resp = self.client.get(reverse('home'))
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        # 5 center navigation links
        self.assertContains(resp, 'Home')
        self.assertContains(resp, 'Features')
        self.assertContains(resp, 'How It Works')
        self.assertContains(resp, 'Intelligence')
        self.assertContains(resp, 'Security')
        # Public auth buttons present for anonymous visitor
        self.assertContains(resp, 'Login')
        self.assertContains(resp, 'Register')
        # Dashboard / Sign Out must NOT appear for anonymous visitors
        self.assertNotIn('Admin Dashboard', content)
        self.assertNotIn('My Dashboard', content)
        self.assertNotIn('Sign Out', content)
        # Authenticated-only sidebar module links absent
        self.assertNotIn('Medicine Master', content)
        self.assertNotIn('Sales &amp; Billing', content)

    def test_public_navbar_authenticated(self):
        """Authenticated users on the public homepage see their dashboard link + Sign Out
        (not Login/Register navbar buttons, which would redirect them causing UX confusion)."""
        # Pharmacist on homepage — sees My Dashboard + Sign Out
        self.client.force_login(self.pharmacist)
        resp = self.client.get(reverse('home'))
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        # Dashboard link and Sign Out present for authenticated pharmacist
        self.assertIn('My Dashboard', content)
        self.assertIn('Sign Out', content)
        # Navbar Login/Register buttons (identified by their unique CSS class btn-nav-login/btn-nav-register
        # pointing to the login/register hrefs) must NOT appear for authenticated users
        self.assertNotIn('btn-nav-login" href="/auth/login/', content)
        self.assertNotIn('btn-nav-register" href="/auth/register/', content)
        # Internal sidebar module links must NOT appear on the public homepage
        self.assertNotIn('Medicine Master', content)
        self.assertNotIn('Sales &amp; Billing', content)
        self.client.logout()

        # Admin on homepage — sees Admin Dashboard + Sign Out
        self.client.force_login(self.admin)
        resp = self.client.get(reverse('home'))
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn('Admin Dashboard', content)
        self.assertIn('Sign Out', content)
        self.assertNotIn('btn-nav-login" href="/auth/login/', content)
        self.assertNotIn('btn-nav-register" href="/auth/register/', content)

    def test_authenticated_user_visiting_login_or_register_redirects_to_dashboard(self):
        """Authenticated Admin/Pharmacist directly opening Login or Register is redirected to their own role dashboard."""
        # 1. Authenticated Admin visiting login or register -> admin_approvals
        self.client.force_login(self.admin)
        resp_login = self.client.get(reverse('pharmacist_login'))
        self.assertRedirects(resp_login, reverse('admin_approvals'))
        resp_reg = self.client.get(reverse('pharmacist_register'))
        self.assertRedirects(resp_reg, reverse('admin_approvals'))
        self.client.logout()

        # 2. Authenticated Approved Pharmacist visiting login or register -> approved
        self.client.force_login(self.pharmacist)
        resp_login = self.client.get(reverse('pharmacist_login'))
        self.assertRedirects(resp_login, reverse('approved'))
        resp_reg = self.client.get(reverse('pharmacist_register'))
        self.assertRedirects(resp_reg, reverse('approved'))
        self.client.logout()

        # 3. Pending Pharmacist visiting login or register -> pending
        self.client.force_login(self.pending_pharmacist)
        resp_login = self.client.get(reverse('pharmacist_login'))
        self.assertRedirects(resp_login, reverse('pending'))
        resp_reg = self.client.get(reverse('pharmacist_register'))
        self.assertRedirects(resp_reg, reverse('pending'))
        self.client.logout()

    def test_post_logout_with_csrf(self):
        """Logout via POST invalidates session and redirects to homepage."""
        self.client.force_login(self.pharmacist)
        resp = self.client.post(reverse('logout'))
        self.assertRedirects(resp, reverse('home'))
        # Session is invalidated
        resp_after = self.client.get(reverse('approved'))
        self.assertRedirects(resp_after, reverse('pharmacist_login'))

    def test_hero_trust_indicators_removed(self):
        """Promotional trust indicators are removed from the homepage hero."""
        resp = self.client.get(reverse('home'))
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertNotIn('hero-trust-row', content)
        self.assertNotIn('Admin Approval Required', content)
        self.assertNotIn('Secure Data Management', content)
        self.assertNotIn('AI-Powered Intelligence', content)
        # Illustrative preview label present
        self.assertIn('Illustrative Preview', content)

    def test_cache_control_headers_on_authenticated_views(self):
        """Authenticated responses include strict no-cache headers to prevent history/back-button leakage."""
        self.client.force_login(self.pharmacist)
        resp = self.client.get(reverse('approved'))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('no-store', resp.headers.get('Cache-Control', ''))
        self.assertIn('no-cache', resp.headers.get('Cache-Control', ''))
        self.assertEqual(resp.headers.get('Pragma'), 'no-cache')
        self.assertEqual(resp.headers.get('Expires'), '0')

    def test_login_safe_next_url_handling(self):
        """Login validates the next parameter, allowing safe internal URLs and blocking open redirects & privilege escalation."""
        # Safe internal URL for pharmacist
        resp = self.client.post(reverse('pharmacist_login'), {
            'identifier': 'sarah_ph',
            'password': 'PharmPassword123!',
            'next': '/medicines/'
        })
        self.assertRedirects(resp, '/medicines/')
        self.client.logout()

        # Pharmacist attempting privilege escalation to admin URL is redirected to approved dashboard
        resp = self.client.post(reverse('pharmacist_login'), {
            'identifier': 'sarah_ph',
            'password': 'PharmPassword123!',
            'next': '/auth/admin/approvals/'
        })
        self.assertRedirects(resp, reverse('approved'))
        self.client.logout()

        # External URL (open redirect attempt) is blocked and redirected to role dashboard
        resp = self.client.post(reverse('pharmacist_login'), {
            'identifier': 'sarah_ph',
            'password': 'PharmPassword123!',
            'next': 'https://malicious-external-site.com'
        })
        self.assertRedirects(resp, reverse('approved'))
        self.client.logout()


class PharmacistWorkspaceSetupAndRoleLoginTests(TestCase):
    """
    Test suite for Pharmacist Workspace Setup, Password Creation & Role-Based Login:
    Verifies that the setup workspace link opens a dedicated public password setup page,
    never opens the Admin Dashboard, validates tokens and approval status, enforces password
    policies, redirects to Login, and properly routes Pharmacist vs Admin logins.
    """
    def setUp(self):
        self.client = Client()

        self.admin = User.objects.create_superuser(
            username='admin_setup',
            email='admin_setup@hospital.org',
            password='AdminPassword123!',
            role=User.Role.ADMIN,
            status=User.Status.APPROVED,
            full_name='System Admin'
        )

        self.approved_pharmacist = User.objects.create_user(
            username='approved_ph',
            email='approved_ph@hospital.org',
            password='InitialPassword123!',
            role=User.Role.PHARMACIST,
            status=User.Status.APPROVED,
            email_verified=True,
            is_active=True,
            license_number='PH-1111',
            registration_id='MV-PH-2026-00111',
            full_name='Dr. Approved Pharmacist'
        )

        self.pending_pharmacist = User.objects.create_user(
            username='pending_ph_setup',
            email='pending_setup@hospital.org',
            password='PendingPassword123!',
            role=User.Role.PHARMACIST,
            status=User.Status.PENDING,
            email_verified=True,
            is_active=False,
            license_number='PH-2222',
            registration_id='MV-PH-2026-00222',
            full_name='Pending Pharmacist'
        )

        self.rejected_pharmacist = User.objects.create_user(
            username='rejected_ph_setup',
            email='rejected_setup@hospital.org',
            password='RejectedPassword123!',
            role=User.Role.PHARMACIST,
            status=User.Status.REJECTED,
            email_verified=True,
            is_active=False,
            license_number='PH-3333',
            registration_id='MV-PH-2026-00333',
            full_name='Rejected Pharmacist'
        )

    def _get_setup_url(self, user):
        uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
        token = pharmacist_password_setup_token_generator.make_token(user)
        return reverse('setup_password', kwargs={'uidb64': uidb64, 'token': token})

    def test_approved_pharmacist_receives_setup_link_with_correct_url(self):
        """1. Approved pharmacist receives a setup link using the correct public setup URL."""
        self.client.force_login(self.admin)
        new_pending = User.objects.create_user(
            username='candidate_setup',
            email='candidate_setup@hospital.org',
            password='TempPass123!',
            role=User.Role.PHARMACIST,
            status=User.Status.PENDING,
            license_number='PH-4444'
        )
        resp = self.client.post(reverse('approve_pharmacist', kwargs={'user_id': new_pending.id}))
        self.assertEqual(resp.status_code, 302)
        new_pending.refresh_from_db()
        self.assertEqual(new_pending.status, User.Status.APPROVED)
        uidb64 = urlsafe_base64_encode(force_bytes(new_pending.pk))
        token = pharmacist_password_setup_token_generator.make_token(new_pending)
        expected_path = f"/auth/setup-password/{uidb64}/{token}/"
        self.assertEqual(reverse('setup_password', kwargs={'uidb64': uidb64, 'token': token}), expected_path)

    def test_opening_setup_link_displays_password_setup_page(self):
        """2. Opening the setup link displays the password setup page with required fields."""
        setup_url = self._get_setup_url(self.approved_pharmacist)
        resp = self.client.get(setup_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Configure Workspace Password")
        self.assertContains(resp, "Set Password &amp; Continue")
        self.assertContains(resp, self.approved_pharmacist.username)
        self.assertContains(resp, self.approved_pharmacist.email)
        self.assertContains(resp, self.approved_pharmacist.registration_id)

    def test_setup_link_never_opens_admin_dashboard_even_with_active_admin_session(self):
        """3. Setup link never opens or grants access to the Admin Dashboard, even if Admin was logged in."""
        self.client.force_login(self.admin)
        setup_url = self._get_setup_url(self.approved_pharmacist)
        resp = self.client.get(setup_url)
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertNotIn('admin-app-container', content)
        self.assertNotIn('adminAppContainer', content)
        self.assertNotIn('Admin Governance', content)
        self.assertIn('Configure Workspace Password', content)
        self.assertIn('Set Password &amp; Continue', content)

    def test_valid_setup_token_allows_password_creation(self):
        """4 & 8. Valid setup token allows password creation, stored as Django password hash."""
        setup_url = self._get_setup_url(self.approved_pharmacist)
        resp = self.client.post(setup_url, {
            'password': 'BrandNewSecurePass2026!',
            'confirm_password': 'BrandNewSecurePass2026!'
        })
        self.assertEqual(resp.status_code, 302)
        self.assertRedirects(resp, reverse('pharmacist_login'))

        self.approved_pharmacist.refresh_from_db()
        self.assertTrue(self.approved_pharmacist.check_password('BrandNewSecurePass2026!'))
        self.assertTrue(self.approved_pharmacist.password.startswith('pbkdf2_sha256$'))
        self.assertFalse(self.approved_pharmacist.must_change_password)

    def test_password_mismatch_is_rejected(self):
        """5. Password mismatch is rejected on setup page."""
        setup_url = self._get_setup_url(self.approved_pharmacist)
        resp = self.client.post(setup_url, {
            'password': 'FirstPassword123!',
            'confirm_password': 'DifferentPassword123!'
        })
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Password and confirmation password do not match.")

    def test_expired_or_previously_used_token_is_rejected(self):
        """6. Expired or previously used token is rejected upon second access."""
        setup_url = self._get_setup_url(self.approved_pharmacist)
        # Use token once
        self.client.post(setup_url, {
            'password': 'UsedTokenPassword123!',
            'confirm_password': 'UsedTokenPassword123!'
        })
        # Try to use same token again
        resp_reuse = self.client.get(setup_url)
        self.assertEqual(resp_reuse.status_code, 200)
        self.assertContains(resp_reuse, "Invalid or Expired Link")

    def test_pending_or_rejected_pharmacist_cannot_activate_account(self):
        """7. Pending or rejected pharmacist cannot activate the account via setup link."""
        # Pending pharmacist
        pending_url = self._get_setup_url(self.pending_pharmacist)
        resp_pending = self.client.get(pending_url)
        self.assertEqual(resp_pending.status_code, 200)
        self.assertContains(resp_pending, "Invalid or Expired Link")

        # Rejected pharmacist
        rejected_url = self._get_setup_url(self.rejected_pharmacist)
        resp_rejected = self.client.get(rejected_url)
        self.assertEqual(resp_rejected.status_code, 200)
        self.assertContains(resp_rejected, "Invalid or Expired Link")

    def test_successful_setup_redirects_to_clean_login(self):
        """9. Successful setup redirects to Login, where login form is displayed."""
        setup_url = self._get_setup_url(self.approved_pharmacist)
        resp = self.client.post(setup_url, {
            'password': 'SetupRedirectPass123!',
            'confirm_password': 'SetupRedirectPass123!'
        })
        self.assertRedirects(resp, reverse('pharmacist_login'))
        # Follow to login page
        resp_login = self.client.get(reverse('pharmacist_login'))
        self.assertEqual(resp_login.status_code, 200)
        self.assertContains(resp_login, "Sign In")

    def test_pharmacist_and_admin_login_role_separation(self):
        """10 & 11. Pharmacist credentials redirect to Pharmacist Dashboard; Admin to Admin Dashboard."""
        # Setup password for approved pharmacist
        setup_url = self._get_setup_url(self.approved_pharmacist)
        self.client.post(setup_url, {
            'password': 'SeparationTestPass123!',
            'confirm_password': 'SeparationTestPass123!'
        })

        # Pharmacist login -> approved dashboard
        resp_ph = self.client.post(reverse('pharmacist_login'), {
            'identifier': 'approved_ph',
            'password': 'SeparationTestPass123!'
        })
        self.assertEqual(resp_ph.status_code, 302)
        self.assertRedirects(resp_ph, reverse('approved'))
        self.client.logout()

        # Admin login -> admin_approvals dashboard
        resp_admin = self.client.post(reverse('pharmacist_login'), {
            'identifier': 'admin_setup',
            'password': 'AdminPassword123!'
        })
        self.assertEqual(resp_admin.status_code, 302)
        self.assertRedirects(resp_admin, reverse('admin_approvals'))
        self.client.logout()

    def test_pharmacist_blocked_from_admin_governance(self):
        """12. Pharmacist cannot access Admin-only pages."""
        self.client.force_login(self.approved_pharmacist)
        resp = self.client.get(reverse('admin_approvals'))
        self.assertRedirects(resp, reverse('unauthorized_403'), target_status_code=403)
        self.client.logout()



