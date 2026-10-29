"""
ai_engine/management/commands/auto_reorder.py
Phase 6 + Procurement Automation — Periodic AI Auto-Reorder Command

Periodically checks active medicines for low-stock / stockout conditions,
calculates accurate demand-driven replenishment quantities, selects the best active
supplier, and generates Purchase Requests in PENDING_APPROVAL status.

Usage:
    python manage.py auto_reorder
    python manage.py auto_reorder --dry-run
    python manage.py auto_reorder --loop --interval=300
    python manage.py auto_reorder --medicines "Paracetamol,Amoxicillin"
"""

import time
import logging
from django.core.management.base import BaseCommand
from django.utils import timezone

from ai_engine.auto_reorder_service import run_auto_reorder

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Automated AI Reordering Engine: detects low stock, calculates forecast-aware "
        "reorder quantities, selects best suppliers, and creates deduplicated Purchase Requests."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='Simulate checks and print reorder proposals without modifying database records.'
        )
        parser.add_argument(
            '--loop',
            action='store_true',
            default=False,
            help='Run continuously in a loop at the specified interval.'
        )
        parser.add_argument(
            '--interval',
            type=int,
            default=300,
            help='Interval in seconds between runs when executing with --loop (default: 300 / 5 minutes).'
        )
        parser.add_argument(
            '--medicines',
            type=str,
            default=None,
            help='Optional comma-separated list of medicine names or IDs to evaluate.'
        )
        parser.add_argument(
            '--quiet',
            action='store_true',
            default=False,
            help='Suppress non-error summary output.'
        )

    def handle(self, *args, **options):
        dry_run = options.get('dry_run', False)
        loop_mode = options.get('loop', False)
        interval = max(10, options.get('interval', 300))
        quiet = options.get('quiet', False)
        med_filter = options.get('medicines')

        medicine_ids = None
        if med_filter:
            from medicines.models import Medicine
            names_or_ids = [m.strip() for m in med_filter.split(',') if m.strip()]
            int_ids = [int(x) for x in names_or_ids if x.isdigit()]
            name_q = [x for x in names_or_ids if not x.isdigit()]
            qs = Medicine.objects.filter(status=Medicine.Status.ACTIVE)
            if int_ids and name_q:
                qs = qs.filter(id__in=int_ids) | qs.filter(medicine_name__in=name_q)
            elif int_ids:
                qs = qs.filter(id__in=int_ids)
            elif name_q:
                qs = qs.filter(medicine_name__in=name_q)
            medicine_ids = list(qs.values_list('id', flat=True))

        if not quiet:
            self.stdout.write(self.style.SUCCESS(
                '\n======================================================\n'
                '  Sanalogx — AI Automated Medicine Reorder Engine\n'
                f'  Started at: {timezone.now().strftime("%Y-%m-%d %H:%M:%S %Z")}\n'
                f'  Mode: {"DRY RUN (simulation only)" if dry_run else "LIVE (creating Purchase Requests)"}\n'
                f'  Loop: {loop_mode} (Interval: {interval}s)\n'
                '======================================================\n'
            ))

        while True:
            cycle_start = time.time()
            stats = run_auto_reorder(
                dry_run=dry_run,
                quiet=quiet,
                medicine_ids=medicine_ids
            )

            if stats.get('locked'):
                if not quiet:
                    self.stdout.write(self.style.WARNING(f"  [LOCK] {stats.get('message')}"))
                if not loop_mode:
                    return
                time.sleep(interval)
                continue

            elapsed = round(time.time() - cycle_start, 2)

            if not quiet:
                self.stdout.write(self.style.SUCCESS(
                    f"Cycle completed in {elapsed}s:\n"
                    f"  - Total medicines evaluated: {stats['total_evaluated']}\n"
                    f"  - Reorder needed:            {stats['reorder_needed']}\n"
                    f"  - Purchase Requests created: {stats['requests_created']}\n"
                    f"  - Purchase Requests updated: {stats['requests_updated']}\n"
                    f"  - Skipped (healthy stock):   {stats['skipped_normal_stock']}\n"
                    f"  - Skipped (active order):    {stats['skipped_existing_order']}\n"
                    f"  - Skipped (zero quantity):   {stats['skipped_zero_qty']}\n"
                    f"  - Errors / Failures:         {stats['failed']}\n"
                ))

                if stats['orders']:
                    self.stdout.write(self.style.SUCCESS('  Generated Requests:'))
                    for ord_info in stats['orders']:
                        self.stdout.write(
                            f"    * {ord_info['medicine']}: {ord_info['quantity']} units "
                            f"(Request: {ord_info['request_id']}, Supplier: {ord_info['supplier']})"
                        )

                if stats['errors']:
                    self.stdout.write(self.style.ERROR('  Errors encountered:'))
                    for err in stats['errors']:
                        self.stdout.write(self.style.ERROR(f"    * {err['medicine']}: {err['error']}"))

            if not loop_mode:
                break

            time.sleep(interval)
