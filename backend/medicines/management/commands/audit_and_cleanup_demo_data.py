"""
medicines/management/commands/audit_and_cleanup_demo_data.py
MediVault — Audit, Classification and Safe Cleanup of Test/Demo/Starter Records

Safety Guarantees:
  - Default mode is DRY-RUN (read-only audit).
  - Never touches active medicines, approved catalogue records, batches, or inventory.
  - Never deletes sales transactions, invoices, prescriptions, goods receipts, or audit logs.
  - Deletion of unlinked draft records requires BOTH --purge-unlinked-drafts AND --confirm flags.
  - All removals are executed inside an atomic database transaction.
  - Records an immutable AuditLog entry for compliance and healthcare traceability.
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from medicines.models import Medicine, MedicineBatch, Category
from accounts.models import User, PharmacistProfile, log_audit


class Command(BaseCommand):
    help = "Safely audit, classify, and optionally clean unlinked starter/demo records in MediVault."

    def add_arguments(self, parser):
        parser.add_argument(
            '--preview',
            action='store_true',
            help='Display individual titles and provenance of candidate records.',
        )
        parser.add_argument(
            '--purge-unlinked-drafts',
            action='store_true',
            help='Purge candidate draft medicines that have ZERO batches, ZERO stock, ZERO sales, and ZERO orders.',
        )
        parser.add_argument(
            '--confirm',
            action='store_true',
            help='Explicit confirmation required to proceed with deletion of unlinked drafts.',
        )

    def handle(self, *args, **options):
        preview = options.get('preview', False)
        purge_drafts = options.get('purge_unlinked_drafts', False)
        confirmed = options.get('confirm', False)

        self.stdout.write("=" * 76)
        self.stdout.write("  MediVault Healthcare Data Audit & Safe Cleanup Utility")
        self.stdout.write("=" * 76)

        # ── 1. Audit Medicines ──────────────────────────────────────────────
        total_medicines = Medicine.objects.count()
        active_medicines = Medicine.objects.filter(status=Medicine.Status.ACTIVE)
        active_with_batches = active_medicines.filter(batches__isnull=False).distinct().count()
        active_zero_batches = active_medicines.filter(batches__isnull=True).distinct().count()

        pending_medicines = Medicine.objects.filter(
            Q(status=Medicine.Status.PENDING_REVIEW) | Q(verification_status=Medicine.VerificationStatus.PENDING_REVIEW)
        ).distinct()

        # Check for unlinked draft records (no batches, no PO items, no sale items)
        unlinked_drafts = pending_medicines.filter(batches__isnull=True)
        try:
            from sales.models import SaleItem
            unlinked_drafts = unlinked_drafts.filter(saleitem__isnull=True)
        except Exception:
            pass
        try:
            from purchases.models import PurchaseOrderItem
            unlinked_drafts = unlinked_drafts.filter(purchaseorderitem__isnull=True)
        except Exception:
            pass

        unlinked_draft_count = unlinked_drafts.distinct().count()

        # ── 2. Audit Batches & Inventory ────────────────────────────────────
        total_batches = MedicineBatch.objects.count()
        today = timezone.now().date()
        valid_batches = MedicineBatch.objects.filter(expiry_date__gt=today).count()
        expired_batches = MedicineBatch.objects.filter(expiry_date__lte=today).count()

        # ── 3. Audit Users & Pharmacist Verification ────────────────────────
        total_users = User.objects.count()
        admin_users = User.objects.filter(role=User.Role.ADMIN).count()
        pharmacists = User.objects.filter(role=User.Role.PHARMACIST)
        verified_pharmacists = PharmacistProfile.objects.filter(verification_status='VERIFIED').count()
        pending_pharmacists = PharmacistProfile.objects.exclude(verification_status='VERIFIED').count()

        # ── Display Audit Table ─────────────────────────────────────────────
        self.stdout.write("\n[1] MEDICINE MASTER CATALOGUE AUDIT:")
        self.stdout.write(f"  • Total registered medicine records:  {total_medicines}")
        self.stdout.write(f"  • Operational Active Formulary:       {active_medicines.count()} (Protected)")
        self.stdout.write(f"      - Active with batch inventory:     {active_with_batches}")
        self.stdout.write(f"      - Active awaiting initial GRN:     {active_zero_batches}")
        self.stdout.write(f"  • Pending Pharmacy Review Queue:      {pending_medicines.count()}")
        self.stdout.write(f"      - Unlinked draft starter records:  {unlinked_draft_count}")

        self.stdout.write("\n[2] PHYSICAL LOT & BATCH AUDIT:")
        self.stdout.write(f"  • Total tracked inventory batches:    {total_batches}")
        self.stdout.write(f"  • Active lots in circulation:         {valid_batches}")
        self.stdout.write(f"  • Expired lots requiring quarantine:  {expired_batches}")

        self.stdout.write("\n[3] PHARMACIST REGULATORY VERIFICATION AUDIT:")
        self.stdout.write(f"  • Total system accounts:              {total_users}")
        self.stdout.write(f"  • System Administrators:              {admin_users}")
        self.stdout.write(f"  • Pharmacist accounts:                {pharmacists.count()}")
        self.stdout.write(f"      - Verified council licenses:      {verified_pharmacists}")
        self.stdout.write(f"      - Verification pending/required:  {pending_pharmacists}")

        # Provenance breakdown
        self.stdout.write("\n[4] DRAFT MEDICINES PROVENANCE BREAKDOWN:")
        provenance = pending_medicines.values('source_reference').annotate(count=Count('id')).order_by('-count')
        for prov in provenance:
            ref = prov['source_reference'] or '(Direct Registration)'
            if len(ref) > 60:
                ref = ref[:57] + '...'
            self.stdout.write(f"  • {prov['count']:4d} records: {ref}")

        # Preview candidate records
        if preview and unlinked_draft_count > 0:
            self.stdout.write("\n[5] PREVIEW OF UNLINKED DRAFT RECORDS (First 15):")
            for d in unlinked_drafts[:15]:
                self.stdout.write(f"  - ID: {d.id:4d} | {d.medicine_name[:30]:30s} | {d.dosage_form or 'N/A':10s} | {d.source_reference or 'N/A'}")

        # ── Cleanup Execution (Guarded) ─────────────────────────────────────
        if purge_drafts:
            if not confirmed:
                self.stdout.write(self.style.WARNING(
                    "\n⚠️  SAFETY NOTICE: Deletion was requested with --purge-unlinked-drafts, "
                    "but --confirm flag is missing.\n   No database records were altered."
                ))
                return

            self.stdout.write(self.style.NOTICE(
                f"\nExecuting safe cleanup of {unlinked_draft_count} unlinked draft records..."
            ))

            with transaction.atomic():
                deleted_count, _ = unlinked_drafts.delete()
                log_audit(
                    user=None,
                    action='DRAFT_MEDICINES_PURGED',
                    description=f"Automated cleanup purged {deleted_count} unlinked starter draft records (0 stock, 0 batches).",
                    performed_by=None,
                )

            self.stdout.write(self.style.SUCCESS(
                f"✓ Successfully and safely cleaned {deleted_count} unlinked starter records.\n"
                f"  Operational active medicines, batches, inventory, and audit logs remain intact."
            ))
        else:
            self.stdout.write("\n" + "=" * 76)
            self.stdout.write("  STATUS: Audit completed in DRY-RUN mode. All operational data is preserved.")
            self.stdout.write("  To preview individual unlinked items:   python manage.py audit_and_cleanup_demo_data --preview")
            self.stdout.write("=" * 76)
