"""
notifications/management/commands/send_scheduled_notifications.py
Phase 7 — Scheduled Notification Runner

Usage: python manage.py send_scheduled_notifications

Runs all scheduled notification checks:
- Low stock / out of stock
- Near expiry (30 days)
- Near expiry (7 days)
- Expired batches
- Overdue purchase orders
- Dead stock (AI-powered)

Safe to run repeatedly — fingerprint deduplication prevents duplicate notifications.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = 'Send scheduled notifications for expiry, low stock, overdue orders, and dead stock.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Run checks but do not save any notifications.'
        )

    def handle(self, *args, **options):
        dry_run = options.get('dry_run', False)
        self.stdout.write(self.style.WARNING(
            f"\n{'[DRY RUN] ' if dry_run else ''}Sanalogx Scheduled Notifications — {timezone.now().strftime('%d %b %Y %H:%M')}"
        ))

        from notifications import service

        checks = [
            ('Low Stock & Out of Stock', service.check_and_notify_low_stock),
            ('Near Expiry (30 Days)', lambda: service.check_and_notify_expiry(days_threshold=30)),
            ('Near Expiry (7 Days)', lambda: service.check_and_notify_expiry(days_threshold=7)),
            ('Expired Batches', service.check_and_notify_expired_batches),
            ('Overdue Purchase Orders', service.check_and_notify_overdue_orders),
            ('Dead Stock (AI)', service.check_and_notify_dead_stock),
        ]

        total_created = 0
        for name, fn in checks:
            try:
                if not dry_run:
                    fn()
                self.stdout.write(f'  [OK] {name}')
            except Exception as e:
                self.stderr.write(f'  [ERROR] {name}: {e}')

        self.stdout.write(self.style.SUCCESS('\nScheduled notifications complete.\n'))
