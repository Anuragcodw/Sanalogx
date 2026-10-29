"""
notifications/management/commands/check_notifications.py
MediVault — Phase 7: Real-Time Notifications Engine Runner

Usage:
  python manage.py check_notifications
  python manage.py check_notifications --dry-run
  python manage.py check_notifications --category=low_stock
"""

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = 'Check and generate notifications for low stock, expiry, overdue orders, stockout risks, and dead stock.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Simulate checks without saving or sending notifications.'
        )
        parser.add_argument(
            '--category', type=str, default='all',
            help='Run specific category: all, low_stock, expiry, overdue, dead_stock'
        )

    def handle(self, *args, **options):
        dry_run = options.get('dry_run', False)
        category = options.get('category', 'all').lower()

        prefix = '[DRY RUN] ' if dry_run else ''
        self.stdout.write(self.style.WARNING(
            f"\n{prefix}Sanalogx Notification Check Engine — {timezone.now().strftime('%d %b %Y %H:%M:%S UTC')}"
        ))

        from notifications import service

        check_map = {
            'low_stock': [('Low Stock & Out of Stock', service.check_and_notify_low_stock)],
            'expiry': [
                ('Near Expiry (30 Days)', lambda: service.check_and_notify_expiry(days_threshold=30)),
                ('Near Expiry (7 Days)', lambda: service.check_and_notify_expiry(days_threshold=7)),
                ('Expired Batches', service.check_and_notify_expired_batches),
            ],
            'overdue': [('Overdue Purchase Orders', service.check_and_notify_overdue_orders)],
            'dead_stock': [('Dead Stock (AI Evaluated)', service.check_and_notify_dead_stock)],
        }

        if category == 'all':
            active_checks = [item for sublist in check_map.values() for item in sublist]
        elif category in check_map:
            active_checks = check_map[category]
        else:
            self.stderr.write(f"Unknown category '{category}'. Available: all, low_stock, expiry, overdue, dead_stock")
            return

        for name, fn in active_checks:
            try:
                if not dry_run:
                    fn()
                self.stdout.write(self.style.SUCCESS(f'  [PASS] {name}'))
            except Exception as e:
                self.stderr.write(self.style.ERROR(f'  [FAIL] {name}: {e}'))

        self.stdout.write(self.style.SUCCESS('Notification check completed successfully.\n'))
