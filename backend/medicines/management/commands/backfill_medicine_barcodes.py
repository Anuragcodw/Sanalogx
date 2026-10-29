"""
medicines/management/commands/backfill_medicine_barcodes.py
Phase 1 — Safe, Repeatable Barcode Backfill Management Command

Usage:
  python manage.py backfill_medicine_barcodes
  python manage.py backfill_medicine_barcodes --force
"""

from django.core.management.base import BaseCommand
from medicines.barcode_service import backfill_all_medicine_barcodes


class Command(BaseCommand):
    help = "Generates and persists unique internal Code 128 barcodes (SAN-MED-000123) for all medicines."

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help='Overwrite existing internal barcodes to synchronize deterministic SAN-MED-{pk:06d} format.',
        )

    def handle(self, *args, **options):
        force = options.get('force', False)
        self.stdout.write(self.style.NOTICE(f"Starting medicine barcode backfill (force={force})..."))

        stats = backfill_all_medicine_barcodes(force=force)

        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully completed barcode backfill!\n"
                f"  Total medicines:   {stats['total']}\n"
                f"  Updated barcodes:  {stats['updated']}\n"
                f"  Unchanged/Skipped: {stats['skipped']}"
            )
        )
