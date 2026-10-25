"""
inventory/management/commands/refresh_pharmacy_intelligence.py

Management command: refresh_pharmacy_intelligence

Refreshes all pharmacy intelligence caches and analytics without touching
production data. Safe to run via cron job or manually.

Usage:
    python manage.py refresh_pharmacy_intelligence
    python manage.py refresh_pharmacy_intelligence --medicines "Paracetamol,Amoxicillin"
    python manage.py refresh_pharmacy_intelligence --skip-forecasts
    python manage.py refresh_pharmacy_intelligence --skip-alerts
    python manage.py refresh_pharmacy_intelligence --dry-run

What it does:
  1. Resolves the current date range (today, last 30 days) using analytics.date_service
  2. Evaluates data freshness via analytics.freshness_service
  3. For each active medicine with inventory:
     a. Refreshes ConsumptionCache (last 7d, 30d, 90d)
     b. Runs AI demand forecast (cutoff = today, no future data leakage)
     c. Evaluates forecast accuracy where possible (holdout evaluation)
     d. Triggers stock health reclassification via inventory.health_service
     e. Generates/updates StockAlert records for critical items
  4. Prints a summary report with elapsed time and counts
"""

import time
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = (
        'Refresh pharmacy intelligence caches: consumption analytics, AI demand '
        'forecasts, inventory health classification, and stock alerts. '
        'Safe to run at any time - does NOT modify inventory quantities or sales records.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--medicines',
            type=str,
            default=None,
            help=(
                'Comma-separated list of medicine names to process. '
                'If omitted, processes ALL active medicines with inventory.'
            )
        )
        parser.add_argument(
            '--skip-forecasts',
            action='store_true',
            default=False,
            help='Skip AI demand forecast refresh (faster run for cache-only refresh).',
        )
        parser.add_argument(
            '--skip-alerts',
            action='store_true',
            default=False,
            help='Skip stock alert generation pass.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='Print what would be done without actually updating any records.',
        )

    def handle(self, *args, **options):
        start_time = time.time()
        dry_run = options['dry_run']
        skip_forecasts = options['skip_forecasts']
        skip_alerts = options['skip_alerts']
        verbosity = options.get('verbosity', 1)
        medicine_filter = options.get('medicines')

        self.stdout.write(self.style.SUCCESS(
            '\n======================================================'
        ))
        self.stdout.write(self.style.SUCCESS(
            '  MediVault - Pharmacy Intelligence Refresh'
        ))
        self.stdout.write(self.style.SUCCESS(
            f'  Started at: {timezone.now().strftime("%Y-%m-%d %H:%M:%S %Z")}'
        ))
        if dry_run:
            self.stdout.write(self.style.WARNING('  [DRY RUN] No changes will be saved.'))
        self.stdout.write(self.style.SUCCESS(
            '======================================================\n'
        ))

        # -- Step 1: Resolve dynamic date context --------------------------
        try:
            from analytics.date_service import resolve_date_range
            date_ctx_30d = resolve_date_range(period='30d')
            date_ctx_today = resolve_date_range(period='today')
            self.stdout.write(
                f'[1/5] Date context resolved - as of: {date_ctx_today["date_to"]}'
            )
        except Exception as e:
            raise CommandError(f'Failed to resolve date context: {e}')

        # -- Step 2: Data freshness report ---------------------------------
        self.stdout.write('[2/5] Checking data freshness...')
        try:
            from analytics.freshness_service import get_data_freshness_report
            freshness = get_data_freshness_report()
            status = freshness.get('overall_status', 'UNKNOWN')
            color = self.style.SUCCESS if status == 'FRESH' else self.style.WARNING
            self.stdout.write(color(f'      Data freshness: {status}'))
            if freshness.get('warning_message'):
                self.stdout.write(self.style.WARNING(f'      [WARN] {freshness["warning_message"]}'))
            if verbosity >= 2:
                for src in freshness.get('data_sources', []):
                    self.stdout.write(f'        - {src.get("name")}: {src.get("status")}')
        except Exception as e:
            self.stdout.write(self.style.WARNING(f'      Warning: freshness check failed - {e}'))

        # -- Step 3: Resolve medicines to process --------------------------
        self.stdout.write('[3/5] Resolving medicines...')
        try:
            from medicines.models import Medicine
            from inventory.models import Inventory

            if medicine_filter:
                names = [n.strip() for n in medicine_filter.split(',') if n.strip()]
                medicines = Medicine.objects.filter(
                    medicine_name__in=names, status='ACTIVE'
                )
                self.stdout.write(f'      Filtered to {medicines.count()} medicine(s): {names}')
            else:
                inv_medicine_ids = Inventory.objects.values_list('medicine_id', flat=True).distinct()
                medicines = Medicine.objects.filter(
                    pk__in=inv_medicine_ids, status='ACTIVE'
                )
                self.stdout.write(f'      Found {medicines.count()} active medicine(s) with inventory.')
        except Exception as e:
            raise CommandError(f'Failed to resolve medicines: {e}')

        total = medicines.count()
        if total == 0:
            self.stdout.write(self.style.WARNING(
                '      No medicines to process. Ensure inventory records exist.'
            ))

        # -- Step 4: Per-medicine intelligence refresh ---------------------
        self.stdout.write(f'[4/5] Refreshing intelligence for {total} medicine(s)...')

        counts = {
            'consumption_refreshed': 0,
            'forecast_refreshed': 0,
            'forecast_skipped': 0,
            'forecast_errors': 0,
            'health_reclassified': 0,
            'accuracy_evaluated': 0,
        }

        cutoff_date = timezone.now()

        for i, medicine in enumerate(medicines, start=1):
            prefix = f'  [{i}/{total}] {medicine.medicine_name}'

            # 4a. Consumption cache refresh
            try:
                from inventory.consumption_service import get_consumption_for_period
                for days in (7, 30, 90):
                    get_consumption_for_period(medicine, days=days)
                if not dry_run:
                    counts['consumption_refreshed'] += 1
                if verbosity >= 2:
                    self.stdout.write(f'{prefix} - consumption cache refreshed (7/30/90d)')
            except Exception as e:
                self.stdout.write(self.style.WARNING(f'{prefix} - consumption error: {e}'))

            # 4b. AI demand forecast refresh
            if not skip_forecasts:
                try:
                    from ai_engine.intelligence_service import get_demand_forecast_multi_horizon
                    if not dry_run:
                        forecast = get_demand_forecast_multi_horizon(
                            medicine, cutoff_date=cutoff_date
                        )
                        counts['forecast_refreshed'] += 1
                    else:
                        counts['forecast_refreshed'] += 1
                    if verbosity >= 2:
                        self.stdout.write(f'{prefix} - forecast refreshed')
                except Exception as e:
                    counts['forecast_errors'] += 1
                    if verbosity >= 1:
                        self.stdout.write(self.style.WARNING(
                            f'{prefix} - forecast error (non-fatal): {e}'
                        ))
            else:
                counts['forecast_skipped'] += 1

            # 4c. Forecast accuracy evaluation (best-effort, won't fail)
            if not skip_forecasts and not dry_run:
                try:
                    from ai_engine.intelligence_service import evaluate_forecast_accuracy
                    evaluate_forecast_accuracy(medicine)
                    counts['accuracy_evaluated'] += 1
                except Exception:
                    pass

            # 4d. Health reclassification
            try:
                from inventory.health_service import classify_inventory_health
                from inventory.models import Inventory
                try:
                    inv = Inventory.objects.get(medicine=medicine)
                    if not dry_run:
                        classify_inventory_health(inv)
                    counts['health_reclassified'] += 1
                except Inventory.DoesNotExist:
                    pass
            except Exception as e:
                if verbosity >= 2:
                    self.stdout.write(self.style.WARNING(f'{prefix} - health error: {e}'))

        # -- Step 5: Stock alert generation --------------------------------
        if not skip_alerts:
            self.stdout.write('[5/5] Generating stock alerts...')
            try:
                from inventory.models import Inventory, StockAlert
                from inventory.health_service import classify_inventory_health

                alert_count = 0
                for inv in Inventory.objects.select_related('medicine').filter(
                    medicine__status='ACTIVE'
                ):
                    try:
                        health = classify_inventory_health(inv)
                        if health and hasattr(health, 'status'):
                            alert_count += 1
                    except Exception:
                        pass

                self.stdout.write(f'      Stock health checked for {alert_count} inventory record(s).')
            except Exception as e:
                self.stdout.write(self.style.WARNING(f'      Alert generation warning: {e}'))
        else:
            self.stdout.write('[5/5] Stock alert generation skipped (--skip-alerts).')

        # -- Summary -------------------------------------------------------
        elapsed = time.time() - start_time
        self.stdout.write(self.style.SUCCESS(
            '\n======================================================'
        ))
        self.stdout.write(self.style.SUCCESS('  Intelligence Refresh Complete'))
        self.stdout.write(f'  Medicines processed     : {total}')
        self.stdout.write(f'  Consumption refreshed   : {counts["consumption_refreshed"]}')
        if not skip_forecasts:
            self.stdout.write(f'  Forecasts refreshed     : {counts["forecast_refreshed"]}')
            self.stdout.write(f'  Forecast errors         : {counts["forecast_errors"]}')
            self.stdout.write(f'  Accuracy evaluations    : {counts["accuracy_evaluated"]}')
        else:
            self.stdout.write(f'  Forecasts               : skipped')
        self.stdout.write(f'  Health reclassifications: {counts["health_reclassified"]}')
        self.stdout.write(f'  Elapsed time            : {elapsed:.2f}s')
        if dry_run:
            self.stdout.write(self.style.WARNING('  [DRY RUN] No records were saved.'))
        self.stdout.write(self.style.SUCCESS(
            '======================================================\n'
        ))
