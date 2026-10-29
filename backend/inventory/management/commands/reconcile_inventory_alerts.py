"""
inventory/management/commands/reconcile_inventory_alerts.py
MediVault — Dynamic Background Inventory Reconciliation Engine

Periodically runs (e.g. every 5 minutes or on-demand):
1. Reconciles batch availability with Inventory current_stock summaries.
2. Monitors batches nearing expiry (30 days, 7 days) and expired batches, updating status and alerts.
3. Identifies missed stockout and low-stock conditions and generates deduplicated alerts.
4. Auto-resolves alerts and notifications for medicines whose stock has risen above reorder thresholds.
5. Employs a distributed cache lock to prevent duplicate overlapping executions.

Usage:
    python manage.py reconcile_inventory_alerts
    python manage.py reconcile_inventory_alerts --loop --interval=300
    python manage.py reconcile_inventory_alerts --dry-run
"""

import time
import logging
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.core.cache import cache
from django.utils import timezone
from django.db.models import Sum, Q

from medicines.models import Medicine, MedicineBatch
from inventory.models import Inventory, StockAlert
from inventory import services as inventory_services

logger = logging.getLogger(__name__)

LOCK_CACHE_KEY = "medivault_reconciliation_running"
LOCK_TIMEOUT_SECONDS = 240  # 4 minutes safety timeout


class Command(BaseCommand):
    help = (
        "Reconciles inventory stock, monitors batch expiry, resolves recovered alerts, "
        "and checks for missed stock notifications. Configurable for periodic background execution."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--interval',
            type=int,
            default=300,
            help='Interval in seconds between runs when executing with --loop (default: 300 / 5 minutes).'
        )
        parser.add_argument(
            '--loop',
            action='store_true',
            default=False,
            help='Run continuously in a loop at the specified interval.'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='Simulate checks without mutating database records.'
        )
        parser.add_argument(
            '--quiet',
            action='store_true',
            default=False,
            help='Suppress non-error logging output.'
        )

    def handle(self, *args, **options):
        loop_mode = options.get('loop', False)
        interval = max(10, options.get('interval', 300))
        dry_run = options.get('dry_run', False)
        quiet = options.get('quiet', False)

        if not quiet:
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"Sanalogx Background Reconciliation Engine Initialized "
                f"[Loop={loop_mode}, Interval={interval}s, DryRun={dry_run}]"
            ))

        while True:
            # 1. Acquire execution lock to prevent overlapping runs
            if not dry_run:
                acquired = cache.add(LOCK_CACHE_KEY, timezone.now().isoformat(), LOCK_TIMEOUT_SECONDS)
                if not acquired:
                    if not quiet:
                        self.stdout.write(self.style.WARNING(
                            "Another reconciliation task is currently running. Skipping this cycle."
                        ))
                    if not loop_mode:
                        return
                    time.sleep(interval)
                    continue

            try:
                self._run_reconciliation_cycle(dry_run=dry_run, quiet=quiet)
            finally:
                if not dry_run:
                    cache.delete(LOCK_CACHE_KEY)

            if not loop_mode:
                break

            time.sleep(interval)

    def _run_reconciliation_cycle(self, dry_run=False, quiet=False):
        start_time = time.time()
        today = timezone.now().date()
        stats = {
            'medicines_recalculated': 0,
            'expiry_alerts_updated': 0,
            'low_stock_alerts_updated': 0,
            'alerts_resolved': 0,
            'notifications_dispatched': 0,
        }

        # ── Step 1: Batch Expiry Monitoring & Status Updates ────────────────
        active_batches = MedicineBatch.objects.filter(
            quantity_available__gt=0
        ).exclude(status__in=[MedicineBatch.BatchStatus.QUARANTINED, MedicineBatch.BatchStatus.RETURNED])

        for batch in active_batches:
            days_to_expiry = (batch.expiry_date - today).days
            if days_to_expiry <= 0:
                if batch.status != MedicineBatch.BatchStatus.EXPIRED:
                    if not dry_run:
                        batch.status = MedicineBatch.BatchStatus.EXPIRED
                        batch.save(update_fields=['status'])
                    stats['expiry_alerts_updated'] += 1
            elif days_to_expiry <= 90:
                if batch.status == MedicineBatch.BatchStatus.ACTIVE:
                    if not dry_run:
                        batch.status = MedicineBatch.BatchStatus.NEAR_EXPIRY
                        batch.save(update_fields=['status'])
                    stats['expiry_alerts_updated'] += 1

        # Generate standard expiry alerts
        if not dry_run:
            inventory_services.generate_expiry_alerts()

        # ── Step 2: Inventory Summary Synchronization & Thresholds ──────────
        inventories = Inventory.objects.select_related('medicine').all()
        for inv in inventories:
            med = inv.medicine

            # Recalculate stock from actual batches
            if not dry_run:
                inventory_services.recalculate_inventory(inv)
                inv.refresh_from_db()
            stats['medicines_recalculated'] += 1

            # ── Step 3: Threshold Evaluation & Resolution ───────────────────
            if inv.current_stock > inv.reorder_level:
                # Resolved: Stock is above reorder level
                resolved_count = StockAlert.objects.filter(
                    medicine=med,
                    alert_type__in=[StockAlert.AlertType.LOW_STOCK, StockAlert.AlertType.OUT_OF_STOCK],
                    is_active=True
                ).update(is_active=False, resolved_at=timezone.now())

                if resolved_count > 0:
                    stats['alerts_resolved'] += resolved_count
                    if not dry_run:
                        try:
                            from notifications.service import resolve_stock_notifications
                            resolve_stock_notifications(med)
                        except Exception:
                            pass
            else:
                # Active alert condition
                if not dry_run:
                    inventory_services.generate_reorder_alerts(inv)
                stats['low_stock_alerts_updated'] += 1

        # ── Step 4: Proactive Missed Expiry Notifications ────────────────────
        if not dry_run:
            try:
                from notifications import service as notif_service
                notif_service.check_and_notify_expiry(days_threshold=30)
                notif_service.check_and_notify_expiry(days_threshold=7)
                notif_service.check_and_notify_expired_batches()
                notif_service.check_and_notify_overdue_orders()
            except Exception as e:
                logger.error(f"[reconcile_inventory] Notification checks error: {e}")

        elapsed = round(time.time() - start_time, 2)
        if not quiet:
            self.stdout.write(self.style.SUCCESS(
                f"[Reconciliation Complete in {elapsed}s] "
                f"Medicines: {stats['medicines_recalculated']}, "
                f"Expiry Checked: {stats['expiry_alerts_updated']}, "
                f"Low/OOS: {stats['low_stock_alerts_updated']}, "
                f"Alerts Resolved: {stats['alerts_resolved']}"
            ))
