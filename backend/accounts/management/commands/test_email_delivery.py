"""
MediVault — Production SMTP Email Delivery Verification Command
================================================================
Usage:
    python manage.py test_email_delivery recipient@example.com
    python manage.py test_email_delivery --check-only
    python manage.py test_email_delivery

Requirements:
- Tests real provider-backed SMTP delivery (never uses console or mocked backend).
- Validates configuration in settings and environment variables.
- Catches and translates SMTP exceptions into safe, actionable guidance.
- Distinguishes provider acceptance (SMTP 250 OK) from confirmed inbox placement.
- Never prints passwords, secrets, or raw credentials.
"""

import sys
import socket
import smtplib
from datetime import datetime
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives, get_connection
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.conf import settings
from django.utils import timezone


def _mask_email(addr):
    if not addr:
        return '(none)'
    parts = addr.split('@')
    if len(parts) == 2:
        u, d = parts
        masked_u = (u[:3] + '***') if len(u) > 3 else '***'
        return f"{masked_u}@{d}"
    return addr[:2] + '***'


class Command(BaseCommand):
    help = "Verifies real SMTP connectivity, TLS/SSL handshake, authentication, and optionally dispatches a test email."

    def add_arguments(self, parser):
        parser.add_argument(
            'recipient',
            nargs='?',
            type=str,
            default=None,
            help="Recipient email address to receive the verification email. If omitted, runs in --check-only mode."
        )
        parser.add_argument(
            '--check-only',
            action='store_true',
            help="Verify configuration, DNS, TCP connection, TLS handshake, and authentication without dispatching an email."
        )
        parser.add_argument(
            '--timeout',
            type=int,
            default=15,
            help="Connection timeout in seconds (default: 15)."
        )

    def handle(self, *args, **options):
        recipient = (options.get('recipient') or '').strip()
        check_only = options.get('check_only', False) or not recipient
        timeout = options.get('timeout', 15)

        if recipient:
            try:
                validate_email(recipient)
            except ValidationError:
                self.stderr.write(self.style.ERROR(f"[ERROR] Invalid recipient email address: '{recipient}'"))
                sys.exit(1)

        site_name = getattr(settings, 'SANALOGX_SITE_NAME', getattr(settings, 'MEDIVAULT_SITE_NAME', 'Sanalogx'))

        self.stdout.write(self.style.MIGRATE_HEADING("\n" + "=" * 70))
        self.stdout.write(self.style.MIGRATE_HEADING(f"   {site_name} - Real SMTP Delivery Diagnostics"))
        self.stdout.write(self.style.MIGRATE_HEADING("=" * 70))

        # ── 1. Configuration Audit ──────────────────────────────────────────
        host = getattr(settings, 'EMAIL_HOST', 'smtp.gmail.com')
        port = getattr(settings, 'EMAIL_PORT', 587)
        use_tls = getattr(settings, 'EMAIL_USE_TLS', True)
        use_ssl = getattr(settings, 'EMAIL_USE_SSL', False)
        host_user = getattr(settings, 'EMAIL_HOST_USER', '')
        host_pass = getattr(settings, 'EMAIL_HOST_PASSWORD', '')
        active_backend = getattr(settings, 'EMAIL_BACKEND', '')
        from accounts.email_service import get_from_email
        from_email = get_from_email()

        self.stdout.write("\n[1/5] CONFIGURATION AUDIT")
        self.stdout.write(f"  * Active Backend:  {active_backend}")
        self.stdout.write(f"  * SMTP Host:       {host}")
        self.stdout.write(f"  * SMTP Port:       {port}")
        self.stdout.write(f"  * Security:        TLS={use_tls}, SSL={use_ssl}")
        self.stdout.write(f"  * Host User:       {_mask_email(host_user)}")
        self.stdout.write(f"  * Password Status: {'[CONFIGURED - ' + str(len(host_pass)) + ' chars]' if host_pass else '[MISSING]'}")
        self.stdout.write(f"  * From Identity:   {from_email}")
        if recipient:
            self.stdout.write(f"  * Test Recipient:  {recipient}")
        else:
            self.stdout.write("  * Mode:            Diagnostic Check Only (No email will be sent)")

        # Validate mutual exclusivity
        if use_tls and use_ssl:
            self.stderr.write(self.style.ERROR(
                "\n[CONFIG ERROR] EMAIL_USE_TLS and EMAIL_USE_SSL are both enabled. "
                "Only one protocol mode must be active."
            ))
            sys.exit(1)

        # Validate credentials present
        if not host_user or not host_pass:
            self.stderr.write(self.style.ERROR(
                "\n[CONFIGURATION ERROR] Missing SMTP credentials.\n"
                "  EMAIL_HOST_USER and EMAIL_HOST_PASSWORD are not configured in your .env file.\n"
                "  Please set your verified sender email and Google App Password."
            ))
            sys.exit(1)

        # ── 2. DNS Resolution ──────────────────────────────────────────────
        self.stdout.write("\n[2/5] DNS RESOLUTION")
        try:
            resolved_ip = socket.gethostbyname(host)
            self.stdout.write(self.style.SUCCESS(f"  [PASS] Resolved host '{host}' -> IP {resolved_ip}"))
        except Exception as exc:
            self.stderr.write(self.style.ERROR(f"  [FAIL] Failed to resolve host '{host}': {exc}"))
            sys.exit(1)

        # ── 3. TCP Port Reachability ───────────────────────────────────────
        self.stdout.write("\n[3/5] TCP SOCKET CONNECTIVITY")
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.close()
            self.stdout.write(self.style.SUCCESS(f"  [PASS] TCP socket to {host}:{port} established within {timeout}s."))
        except Exception as exc:
            self.stderr.write(self.style.ERROR(
                f"  [FAIL] Cannot connect to {host}:{port}: {exc}\n"
                f"  Check network firewall or outbound port {port} restrictions."
            ))
            sys.exit(1)

        # ── 4. TLS/SSL Handshake & SMTP Authentication ──────────────────────
        self.stdout.write("\n[4/5] TLS/SSL HANDSHAKE & AUTHENTICATION")
        try:
            if use_ssl:
                server = smtplib.SMTP_SSL(host, port, timeout=timeout)
                server.ehlo()
                self.stdout.write(self.style.SUCCESS(f"  [PASS] SSL connection established on port {port}."))
            else:
                server = smtplib.SMTP(host, port, timeout=timeout)
                server.ehlo()
                if use_tls:
                    server.starttls()
                    server.ehlo()
                    self.stdout.write(self.style.SUCCESS("  [PASS] STARTTLS negotiation completed successfully."))

            # Perform authentication
            code, resp = server.login(host_user, host_pass)
            self.stdout.write(self.style.SUCCESS(f"  [PASS] SMTP Authentication accepted by provider (Code {code})."))
            server.quit()
        except smtplib.SMTPAuthenticationError as exc:
            self.stderr.write(self.style.ERROR(
                f"\n  [FAIL] SMTP Authentication Rejected: {exc}\n"
                "  Troubleshooting steps for Gmail:\n"
                "    1. Verify 2-Step Verification is active on the sender Google Account.\n"
                "    2. Generate a 16-character App Password at: https://myaccount.google.com/apppasswords\n"
                "    3. Update EMAIL_HOST_PASSWORD in your .env file."
            ))
            sys.exit(1)
        except smtplib.SMTPServerDisconnected as exc:
            self.stderr.write(self.style.ERROR(
                f"\n  [FAIL] Server Disconnected during handshake/auth: {exc}\n"
                "  Google terminates connections when credentials fail (535 BadCredentials)."
            ))
            sys.exit(1)
        except Exception as exc:
            self.stderr.write(self.style.ERROR(f"\n  [FAIL] Handshake/Auth Error: {exc}"))
            sys.exit(1)

        # ── 5. Test Email Dispatch (if recipient specified) ────────────────
        self.stdout.write("\n[5/5] EMAIL DISPATCH & MESSAGE ACCEPTANCE")
        if check_only:
            self.stdout.write(self.style.SUCCESS(
                "  [SKIPPED] Check-only mode active. To send a real test email, pass recipient:\n"
                f"            python manage.py test_email_delivery {_mask_email(host_user)}"
            ))
            self.stdout.write(self.style.SUCCESS("\n" + "=" * 70))
            self.stdout.write(self.style.SUCCESS("   [PASS] ALL SMTP DIAGNOSTIC CHECKS PASSED SUCCESSFULLY"))
            self.stdout.write(self.style.SUCCESS("=" * 70 + "\n"))
            return

        now_str = timezone.now().strftime('%Y-%m-%d %H:%M:%S UTC')
        subject = f"{site_name} | Real SMTP Delivery Verification"

        context = {
            'site_name': site_name,
            'recipient': recipient,
            'timestamp': now_str,
            'smtp_host': host,
            'smtp_port': port,
            'sender': from_email,
        }

        try:
            html_content = render_to_string('emails/smtp_test_email.html', context)
        except Exception:
            html_content = f"""
            <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #E2E8F0; border-radius: 8px;">
              <h2 style="color: #0F2744;">{site_name} SMTP Verification</h2>
              <p>This is an authorized test email confirming real SMTP delivery from the {site_name} Platform.</p>
              <table style="width: 100%; border-collapse: collapse; margin: 16px 0; background: #F8FAFC;">
                <tr><td style="padding: 8px; font-weight: bold;">Timestamp:</td><td style="padding: 8px;">{now_str}</td></tr>
                <tr><td style="padding: 8px; font-weight: bold;">SMTP Host:</td><td style="padding: 8px;">{host}:{port}</td></tr>
                <tr><td style="padding: 8px; font-weight: bold;">Sender:</td><td style="padding: 8px;">{from_email}</td></tr>
                <tr><td style="padding: 8px; font-weight: bold;">Recipient:</td><td style="padding: 8px;">{recipient}</td></tr>
              </table>
            </div>
            """

        text_content = (
            f"{subject}\n\n"
            f"Authorized verification email from {site_name}.\n"
            f"Timestamp: {now_str}\n"
            f"SMTP Host: {host}:{port}\n"
            f"Sender: {from_email}\n"
            f"Recipient: {recipient}\n"
        )

        try:
            connection = get_connection(
                backend='django.core.mail.backends.smtp.EmailBackend',
                host=host,
                port=port,
                username=host_user,
                password=host_pass,
                use_tls=use_tls,
                use_ssl=use_ssl,
                timeout=timeout,
                fail_silently=False,
            )

            msg = EmailMultiAlternatives(
                subject=subject,
                body=text_content,
                from_email=from_email,
                to=[recipient],
                connection=connection,
            )
            msg.attach_alternative(html_content, "text/html")
            self.stdout.write("  [TRANSMITTING] Dispatching test message...")
            msg.send(fail_silently=False)

            self.stdout.write(self.style.SUCCESS(f"  [PASS] SMTP server accepted message for '{recipient}' (250 OK)."))
            self.stdout.write(self.style.SUCCESS("\n" + "=" * 70))
            self.stdout.write(self.style.SUCCESS("   [SUCCESS] SMTP PROVIDER ACCEPTED MESSAGE"))
            self.stdout.write(self.style.SUCCESS("=" * 70))
            self.stdout.write(f"\n  * Provider Status:   Accepted by mail server (250 OK)")
            self.stdout.write(f"  * Dispatched To:     {recipient}")
            self.stdout.write(f"  * Sent From:         {from_email}")
            self.stdout.write(f"  * Dispatched At:     {now_str}")
            self.stdout.write(self.style.NOTICE(
                "\n  [IMPORTANT NOTICE]:\n"
                "  SMTP acceptance confirms that the provider mail server accepted the transmission.\n"
                "  Inbox placement depends on recipient spam filters, SPF/DKIM verification,\n"
                "  and recipient inbox rules. Please check the target inbox and spam folder.\n"
            ))

        except Exception as exc:
            self.stderr.write(self.style.ERROR(f"  [FAIL] Failed to transmit message: {exc}"))
            sys.exit(1)
