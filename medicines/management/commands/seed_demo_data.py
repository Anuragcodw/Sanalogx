"""
MediVault â€” medicines/management/commands/seed_demo_data.py

Populates the database with realistic hospital pharmacy demo data.

Usage:
    python manage.py seed_demo_data            # Insert demo data (safe, idempotent)
    python manage.py seed_demo_data --flush    # WARNING: Deletes all operational data first

Safety:
    - Never drops or recreates tables.
    - Idempotent by default: skips records that already exist.
    - Does NOT touch User, session, auth, or migration records.
    - --flush only removes operational records (medicines, inventory, sales, etc.)
"""

import random
from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction

from medicines.models import Category, Medicine, MedicineBatch
from inventory.models import Inventory, StockTransaction
from suppliers.models import Supplier
from accounts.models import User


class Command(BaseCommand):
    help = "Seed realistic hospital pharmacy demo data (idempotent by default)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--flush",
            action="store_true",
            help="Delete existing operational data before seeding (IRREVERSIBLE).",
        )

    def handle(self, *args, **options):
        if options["flush"]:
            self.stdout.write(self.style.WARNING(
                "âš   --flush: Removing existing operational data..."
            ))
            self._flush_data()

        self.stdout.write("ðŸŒ±  Seeding demo data...")
        with transaction.atomic():
            categories = self._seed_categories()
            medicines = self._seed_medicines(categories)
            batches = self._seed_batches(medicines)
            self._seed_inventory(medicines, batches)
            self._seed_suppliers()
            self._seed_sales(medicines, batches)
            self._seed_dispensing(medicines, batches)

        self.stdout.write(self.style.SUCCESS(
            "\nâœ…  Demo data seeded successfully!\n"
            "   Run the server and log in to see all dashboard metrics populated."
        ))

    # â”€â”€â”€ FLUSH â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _flush_data(self):
        """Remove all operational records safely (leaves users & auth intact)."""
        from inventory.models import StockTransaction, StockAlert, Inventory
        from sales.models import Sale, SaleItem
        from dispensing.models import (
            DispensingRecord, DispensingAllocation, MedicineRequest,
            MedicineRequestItem, Prescription, PrescriptionItem,
            DispensingInvoice, InvoiceItem,
        )
        from suppliers.models import Supplier, SupplierMedicine
        from purchases.models import PurchaseOrder, PurchaseOrderItem
        from notifications.models import Notification

        models_to_flush = [
            DispensingAllocation, DispensingRecord, PrescriptionItem, Prescription,
            MedicineRequestItem, DispensingInvoice, MedicineRequest,
            SaleItem, Sale, StockTransaction, StockAlert, Inventory,
            PurchaseOrderItem, PurchaseOrder, SupplierMedicine, Supplier,
            MedicineBatch, Medicine, Category,
        ]
        for model in models_to_flush:
            count, _ = model.objects.all().delete()
            self.stdout.write(f"   Deleted {count} {model.__name__} records.")

    # â”€â”€â”€ CATEGORIES â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _seed_categories(self):
        data = [
            ("Antibiotics", "Medicines that fight bacterial infections"),
            ("Analgesics", "Pain relief medicines"),
            ("Antidiabetics", "Medicines for blood sugar management"),
            ("Antihypertensives", "Blood pressure management medicines"),
            ("Vitamins & Supplements", "Vitamins, minerals, and dietary supplements"),
            ("Antihistamines", "Allergy and cold treatment medicines"),
            ("Cardiovascular", "Heart and circulatory system medicines"),
            ("Gastrointestinal", "Digestive system medicines"),
            ("Respiratory", "Asthma, COPD, and lung medicines"),
            ("Dermatology", "Skin care and dermatological medicines"),
            ("Ophthalmology", "Eye care medicines and drops"),
            ("Neurology", "Brain and nerve system medicines"),
            ("Controlled Substances", "Schedule H1 / X / Narcotic medicines"),
        ]
        cats = {}
        for name, desc in data:
            obj, created = Category.objects.get_or_create(
                name=name, defaults={"description": desc, "is_active": True}
            )
            cats[name] = obj
            if created:
                self.stdout.write(f"   + Category: {name}")
        self.stdout.write(self.style.SUCCESS(f"   âœ“ {len(cats)} categories ready."))
        return cats

    # â”€â”€â”€ MEDICINES â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _seed_medicines(self, cats):
        MEDICINES = [
            # (name, generic, brand, category_key, form, strength, manufacturer, unit, reg_category, reorder, min, max, safety)
            ("Amoxicillin", "Amoxicillin", "Mox", "Antibiotics", "Capsule", "500mg", "Cipla Ltd", "Capsule", "SCHEDULE_H", 100, 30, 1000, 50),
            ("Azithromycin", "Azithromycin", "Zithromax", "Antibiotics", "Tablet", "500mg", "Pfizer India", "Tablet", "SCHEDULE_H", 80, 20, 800, 40),
            ("Ciprofloxacin", "Ciprofloxacin", "Ciplox", "Antibiotics", "Tablet", "500mg", "Cipla Ltd", "Tablet", "SCHEDULE_H", 80, 20, 800, 40),
            ("Metronidazole", "Metronidazole", "Flagyl", "Antibiotics", "Tablet", "400mg", "Abbott India", "Tablet", "SCHEDULE_H", 60, 15, 600, 30),
            ("Paracetamol", "Paracetamol", "Crocin", "Analgesics", "Tablet", "500mg", "GSK India", "Tablet", "NORMAL", 200, 50, 2000, 100),
            ("Ibuprofen", "Ibuprofen", "Brufen", "Analgesics", "Tablet", "400mg", "Abbott India", "Tablet", "NORMAL", 150, 40, 1500, 75),
            ("Diclofenac Sodium", "Diclofenac Sodium", "Voveran", "Analgesics", "Tablet", "50mg", "Novartis India", "Tablet", "SCHEDULE_H", 80, 20, 800, 40),
            ("Tramadol", "Tramadol HCl", "Ultram", "Analgesics", "Capsule", "50mg", "Zydus Cadila", "Capsule", "SCHEDULE_H1", 40, 10, 400, 20),
            ("Metformin", "Metformin HCl", "Glycomet", "Antidiabetics", "Tablet", "500mg", "USV Ltd", "Tablet", "SCHEDULE_H", 120, 30, 1200, 60),
            ("Glibenclamide", "Glibenclamide", "Daonil", "Antidiabetics", "Tablet", "5mg", "Sanofi India", "Tablet", "SCHEDULE_H", 80, 20, 800, 40),
            ("Insulin Glargine", "Insulin Glargine", "Lantus", "Antidiabetics", "Injection", "100IU/mL", "Sanofi India", "Vial", "SCHEDULE_H", 30, 10, 300, 15),
            ("Amlodipine", "Amlodipine Besylate", "Norvasc", "Antihypertensives", "Tablet", "5mg", "Pfizer India", "Tablet", "SCHEDULE_H", 100, 25, 1000, 50),
            ("Enalapril", "Enalapril Maleate", "Vasotec", "Antihypertensives", "Tablet", "5mg", "MSD India", "Tablet", "SCHEDULE_H", 80, 20, 800, 40),
            ("Losartan", "Losartan Potassium", "Cozaar", "Antihypertensives", "Tablet", "50mg", "MSD India", "Tablet", "SCHEDULE_H", 80, 20, 800, 40),
            ("Vitamin C", "Ascorbic Acid", "Celin", "Vitamins & Supplements", "Tablet", "500mg", "GSK India", "Tablet", "NORMAL", 100, 30, 1000, 50),
            ("Vitamin D3", "Cholecalciferol", "Calcirol", "Vitamins & Supplements", "Capsule", "60000IU", "Cadila Healthcare", "Capsule", "NORMAL", 80, 20, 800, 40),
            ("Cetirizine", "Cetirizine HCl", "Zyrtec", "Antihistamines", "Tablet", "10mg", "UCB India", "Tablet", "NORMAL", 100, 30, 1000, 50),
            ("Loratadine", "Loratadine", "Claritin", "Antihistamines", "Tablet", "10mg", "Bayer India", "Tablet", "NORMAL", 80, 20, 800, 40),
            ("Atorvastatin", "Atorvastatin Calcium", "Lipitor", "Cardiovascular", "Tablet", "20mg", "Pfizer India", "Tablet", "SCHEDULE_H", 100, 25, 1000, 50),
            ("Aspirin", "Acetylsalicylic Acid", "Disprin", "Cardiovascular", "Tablet", "75mg", "Reckitt India", "Tablet", "NORMAL", 150, 40, 1500, 75),
            ("Digoxin", "Digoxin", "Lanoxin", "Cardiovascular", "Tablet", "0.25mg", "GSK India", "Tablet", "SCHEDULE_H", 50, 15, 500, 25),
            ("Omeprazole", "Omeprazole", "Prilosec", "Gastrointestinal", "Capsule", "20mg", "AstraZeneca India", "Capsule", "NORMAL", 100, 30, 1000, 50),
            ("Pantoprazole", "Pantoprazole Sodium", "Pantocid", "Gastrointestinal", "Tablet", "40mg", "Sun Pharma", "Tablet", "SCHEDULE_H", 100, 25, 1000, 50),
            ("Domperidone", "Domperidone", "Motilium", "Gastrointestinal", "Tablet", "10mg", "Janssen India", "Tablet", "NORMAL", 80, 20, 800, 40),
            ("Salbutamol Inhaler", "Salbutamol", "Ventolin", "Respiratory", "Inhaler", "100mcg/dose", "GSK India", "Inhaler", "SCHEDULE_H", 50, 15, 500, 25),
            ("Budesonide", "Budesonide", "Pulmicort", "Respiratory", "Inhaler", "200mcg/dose", "AstraZeneca India", "Inhaler", "SCHEDULE_H", 40, 10, 400, 20),
            ("Clotrimazole Cream", "Clotrimazole", "Canesten", "Dermatology", "Cream", "1%", "Bayer India", "Tube", "NORMAL", 40, 10, 400, 20),
            ("Betamethasone Cream", "Betamethasone", "Betnovate", "Dermatology", "Cream", "0.1%", "GSK India", "Tube", "SCHEDULE_H", 40, 10, 400, 20),
            ("Timolol Eye Drops", "Timolol Maleate", "Timoptic", "Ophthalmology", "Eye Drops", "0.5%", "MSD India", "Bottle", "SCHEDULE_H", 30, 10, 300, 15),
            ("Morphine Sulfate", "Morphine Sulfate", "MS Contin", "Controlled Substances", "Tablet", "10mg", "Mundipharma India", "Tablet", "NARCOTIC", 20, 5, 200, 10),
        ]

        meds = []
        for i, row in enumerate(MEDICINES):
            name, generic, brand, cat_key, form, strength, mfr, unit, reg_cat, reorder, min_s, max_s, safety = row
            cat = cats.get(cat_key)
            if not cat:
                continue
            obj, created = Medicine.objects.get_or_create(
                medicine_name=name,
                defaults={
                    "generic_name": generic,
                    "brand_name": brand,
                    "category": cat,
                    "dosage_form": form,
                    "strength": strength,
                    "manufacturer": mfr,
                    "unit_of_measure": unit,
                    "regulatory_category": reg_cat,
                    "status": "ACTIVE",
                    "reorder_level": reorder,
                    "minimum_stock": min_s,
                    "maximum_stock": max_s,
                    "safety_stock": safety,
                    "description": f"Standard {form.lower()} formulation of {generic} for hospital pharmacy use.",
                },
            )
            meds.append(obj)
            if created:
                self.stdout.write(f"   + Medicine: {name}")

        self.stdout.write(self.style.SUCCESS(f"   âœ“ {len(meds)} medicines ready."))
        return meds

    # â”€â”€â”€ BATCHES â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _seed_batches(self, medicines):
        today = date.today()
        batches = []

        prefixes = {
            1: 'AMX', 2: 'AZI', 3: 'CIP', 4: 'MET', 5: 'PCM', 6: 'IBU', 7: 'DIC', 8: 'TRM',
            9: 'MFM', 10: 'GLB', 11: 'INS', 12: 'AML', 13: 'ENL', 14: 'LOS', 15: 'VTC', 16: 'VTD',
            17: 'CTZ', 18: 'LOR', 19: 'ATV', 20: 'ASP', 21: 'DGX', 22: 'OMP', 23: 'PAN', 24: 'DOM',
            25: 'SBT', 26: 'BDS', 27: 'CLT', 28: 'BTM', 29: 'TML', 30: 'MPH'
        }

        def _get_storage(m):
            name = m.medicine_name.lower()
            if 'insulin' in name:
                return 'Refrigerated (2°C - 8°C). Do not freeze. Protect from light.'
            if 'eye drops' in name or 'timolol' in name:
                return 'Store below 25°C. Protect from light. Discard 28 days after opening.'
            if 'inhaler' in name or 'salbutamol' in name or 'budesonide' in name:
                return 'Store below 30°C. Protect from direct heat, frost and sunlight.'
            if 'cream' in name or 'clotrimazole' in name or 'betamethasone' in name:
                return 'Store below 25°C. Do not freeze. Keep tube tightly closed.'
            if 'morphine' in name or 'tramadol' in name:
                return 'Controlled Storage: Double-lock Schedule X/Narcotic safe, below 25°C.'
            if 'vitamin c' in name:
                return 'Store below 25°C. Protect from light and moisture.'
            if m.category and m.category.name == 'Antibiotics':
                return 'Store below 25°C in a dry place. Protect from moisture.'
            if 'prazole' in name:
                return 'Store below 25°C in a dry place. Moisture sensitive.'
            return 'Store below 25°C in a dry place.'

        for med in medicines:
            existing = med.batches.count()
            if existing >= 2:
                batches.extend(list(med.batches.all()))
                continue

            pfx = prefixes.get(med.id, f'BTH{med.id:02d}')
            storage = _get_storage(med)

            # Batch 1: Primary active batch with unique staggered dates
            b1_num = f"{pfx}-25A{med.id:02d}"
            qty1 = random.randint(150, 400)
            price_buy = Decimal(str(round(random.uniform(5, 80), 2)))
            price_sell = Decimal(str(round(float(price_buy) * random.uniform(1.15, 1.40), 2)))
            mfg1 = today - timedelta(days=200 + (med.id * 13) % 250)
            exp1 = mfg1 + timedelta(days=730 + (med.id * 23) % 365)

            b1, b1_created = MedicineBatch.objects.get_or_create(
                medicine=med,
                batch_number=b1_num,
                defaults={
                    "manufacturing_date": mfg1,
                    "expiry_date": exp1,
                    "purchase_price": price_buy,
                    "selling_price": price_sell,
                    "quantity_received": qty1,
                    "quantity_available": qty1,
                    "storage_condition": storage,
                    "is_controlled": med.regulatory_category in ["SCHEDULE_H1", "SCHEDULE_X", "NARCOTIC"],
                    "status": "ACTIVE",
                    "received_date": mfg1 + timedelta(days=30),
                },
            )
            batches.append(b1)
            if b1_created:
                self.stdout.write(f"   + Batch: {b1_num} ({med.medicine_name})")

            # Batch 2: Staggered risk distribution across critical, near-expiry, expired, quarantined
            b2_num = f"{pfx}-24K{med.id:02d}"
            qty2 = random.randint(20, 80)
            if med.id in [2, 5, 9]:
                # Critical near expiry (15-28 days)
                days_left = 15 + (med.id * 4) % 15
                exp2 = today + timedelta(days=days_left)
                mfg2 = exp2 - timedelta(days=730)
                status2 = "NEAR_EXPIRY"
            elif med.id in [1, 4, 6, 11, 15, 22]:
                # Near expiry warning (35-80 days)
                days_left = 35 + (med.id * 7) % 50
                exp2 = today + timedelta(days=days_left)
                mfg2 = exp2 - timedelta(days=730)
                status2 = "NEAR_EXPIRY"
            elif med.id in [3, 17]:
                # Expired (25-45 days ago)
                days_ago = 25 + (med.id * 5) % 40
                exp2 = today - timedelta(days=days_ago)
                mfg2 = exp2 - timedelta(days=730)
                status2 = "EXPIRED"
            elif med.id == 8:
                # Quarantined
                exp2 = today + timedelta(days=150)
                mfg2 = exp2 - timedelta(days=730)
                status2 = "QUARANTINED"
            else:
                # Standard active batch
                exp2 = today + timedelta(days=120 + (med.id * 19) % 300)
                mfg2 = exp2 - timedelta(days=730)
                status2 = "ACTIVE"

            b2, b2_created = MedicineBatch.objects.get_or_create(
                medicine=med,
                batch_number=b2_num,
                defaults={
                    "manufacturing_date": mfg2,
                    "expiry_date": exp2,
                    "purchase_price": price_buy,
                    "selling_price": price_sell,
                    "quantity_received": qty2,
                    "quantity_available": qty2,
                    "storage_condition": storage,
                    "is_controlled": med.regulatory_category in ["SCHEDULE_H1", "SCHEDULE_X", "NARCOTIC"],
                    "status": status2,
                    "received_date": mfg2 + timedelta(days=30),
                },
            )
            batches.append(b2)
            if b2_created:
                self.stdout.write(f"   + Batch: {b2_num} ({status2.lower()} — {med.medicine_name})")

        self.stdout.write(self.style.SUCCESS(f"   ✓ {len(batches)} batches ready."))
        return batches

    # â”€â”€â”€ INVENTORY â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _seed_inventory(self, medicines, batches):
        count = 0
        for med in medicines:
            # Aggregate available qty across all batches
            total_stock = sum(
                b.quantity_available for b in med.batches.all()
            )

            inv, created = Inventory.objects.get_or_create(
                medicine=med,
                defaults={
                    "current_stock": total_stock,
                    "available_stock": total_stock,
                    "minimum_stock": med.minimum_stock,
                    "maximum_stock": med.maximum_stock,
                    "reorder_level": med.reorder_level,
                    "safety_stock": med.safety_stock,
                },
            )
            if not created:
                # Sync stock from batches if it was already 0
                if inv.current_stock == 0 and total_stock > 0:
                    inv.current_stock = total_stock
                    inv.available_stock = total_stock
                    inv.save()

            # Create OPENING stock transaction
            first_batch = med.batches.order_by("received_date").first()
            if first_batch and not inv.transactions.filter(transaction_type="OPENING").exists():
                admin = User.objects.filter(role="ADMIN").first()
                StockTransaction.objects.create(
                    inventory=inv,
                    medicine=med,
                    batch=first_batch,
                    transaction_type="OPENING",
                    quantity=inv.current_stock,
                    previous_stock=0,
                    new_stock=inv.current_stock,
                    reference_number=f"OPEN-{med.id:04d}",
                    notes="Opening stock entry seeded via demo data command.",
                    performed_by=admin,
                )
                count += 1

        self.stdout.write(self.style.SUCCESS(f"   âœ“ Inventory synced + {count} OPENING transactions created."))

    # â”€â”€â”€ SUPPLIERS â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _seed_suppliers(self):
        SUPPLIERS = [
            {
                "company_name": "MedScope Pharmaceuticals Pvt. Ltd.",
                "contact_person": "Rajiv Mehta",
                "email": "rajiv@medscope.in",
                "phone": "+91-9876543210",
                "city": "Mumbai",
                "state": "Maharashtra",
                "rating": Decimal("4.5"),
                "average_delivery_days": 3,
                "on_time_delivery_rate": Decimal("95.00"),
                "fill_rate": Decimal("98.00"),
            },
            {
                "company_name": "PharmaBridge India Ltd.",
                "contact_person": "Sunita Sharma",
                "email": "sunita@pharmabridge.in",
                "phone": "+91-9123456780",
                "city": "Delhi",
                "state": "Delhi",
                "rating": Decimal("4.2"),
                "average_delivery_days": 5,
                "on_time_delivery_rate": Decimal("90.00"),
                "fill_rate": Decimal("95.00"),
            },
            {
                "company_name": "HealthLine Distributors",
                "contact_person": "Arun Nair",
                "email": "arun@healthlinedist.in",
                "phone": "+91-9988776655",
                "city": "Bengaluru",
                "state": "Karnataka",
                "rating": Decimal("3.8"),
                "average_delivery_days": 7,
                "on_time_delivery_rate": Decimal("82.00"),
                "fill_rate": Decimal("88.00"),
            },
        ]

        count = 0
        for s in SUPPLIERS:
            obj, created = Supplier.objects.get_or_create(
                company_name=s["company_name"],
                defaults={
                    "contact_person": s.get("contact_person", ""),
                    "email": s.get("email", ""),
                    "phone": s.get("phone", ""),
                    "city": s.get("city", ""),
                    "state": s.get("state", ""),
                    "country": "India",
                    "rating": s.get("rating", Decimal("3.0")),
                    "average_delivery_days": s.get("average_delivery_days", 7),
                    "on_time_delivery_rate": s.get("on_time_delivery_rate", Decimal("90.00")),
                    "fill_rate": s.get("fill_rate", Decimal("90.00")),
                    "payment_terms": "NET_30",
                    "status": "ACTIVE",
                },
            )
            if created:
                count += 1
                self.stdout.write(f"   + Supplier: {s['company_name']}")

        self.stdout.write(self.style.SUCCESS(f"   âœ“ {count} suppliers created."))

    # â”€â”€â”€ SALES â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _seed_sales(self, medicines, batches):
        from sales.models import Sale, SaleItem

        if Sale.objects.count() > 0:
            self.stdout.write(f"   â„¹  Sales already exist â€” skipping.")
            return

        pharmacist = (
            User.objects.filter(role="PHARMACIST", status="APPROVED").first()
            or User.objects.filter(role="ADMIN").first()
        )
        if not pharmacist:
            self.stdout.write(self.style.WARNING("   âš   No pharmacist/admin found â€” skipping sales seed."))
            return

        today = timezone.now().date()
        PAYMENT_METHODS = ["CASH", "CARD", "UPI", "INSURANCE"]
        PATIENTS = [
            ("Rajesh Kumar", "UHID-001", "9988001122"),
            ("Priya Sharma", "UHID-002", "9876543210"),
            ("Anil Verma", "UHID-003", "9123456789"),
            ("Sunita Patel", "UHID-004", "8765432100"),
            ("Mohammed Ali", "UHID-005", "7654321009"),
            ("Ananya Singh", "UHID-006", "9000011122"),
            ("Walk-in Customer", "WALK-IN", ""),
        ]

        # Create 60 sales spread across past 60 days
        sale_count = 0
        inv_count = 0
        for day_offset in range(59, -1, -1):
            sales_today = random.randint(1, 4)
            # Use timezone-aware datetime to avoid RuntimeWarning
            sale_date = timezone.now().replace(hour=10, minute=0, second=0, microsecond=0) - timedelta(days=day_offset)

            for _ in range(sales_today):
                patient = random.choice(PATIENTS)
                payment = random.choice(PAYMENT_METHODS)
                items_count = random.randint(1, 3)

                # Pick random medicines with available stock
                selected = random.sample(
                    [m for m in medicines if m.batches.filter(quantity_available__gt=5).exists()],
                    min(items_count, 3),
                )
                if not selected:
                    continue

                sale_number = f"SALE-{sale_date.year}-{Sale.objects.count() + 1:05d}"
                inv_number = f"INV-{sale_date.year}-{Sale.objects.count() + 1:05d}"

                sale = Sale.objects.create(
                    sale_number=sale_number,
                    invoice_number=inv_number,
                    patient_name=patient[0],
                    patient_id=patient[1],
                    mobile_number=patient[2],
                    payment_method=payment,
                    status="COMPLETED",
                    sold_by=pharmacist,
                    subtotal=Decimal("0"),
                    total_amount=Decimal("0"),
                )
                # Override auto_now_add sale_date
                Sale.objects.filter(pk=sale.pk).update(sale_date=sale_date)
                sale.refresh_from_db()

                subtotal = Decimal("0")
                for med in selected:
                    batch = (
                        med.batches.filter(quantity_available__gt=5, status="ACTIVE")
                        .order_by("expiry_date")
                        .first()
                        or med.batches.filter(quantity_available__gt=5).first()
                    )
                    if not batch:
                        continue
                    qty = random.randint(1, 5)
                    if batch.quantity_available < qty:
                        qty = batch.quantity_available
                    if qty < 1:
                        continue

                    item = SaleItem.objects.create(
                        sale=sale,
                        medicine=med,
                        batch=batch,
                        quantity=qty,
                        unit_price=batch.selling_price,
                        purchase_unit_price=batch.purchase_price,
                        total_price=batch.selling_price * qty,
                    )
                    subtotal += item.total_price

                    # Deduct from batch
                    batch.quantity_available = max(0, batch.quantity_available - qty)
                    batch.save(update_fields=["quantity_available"])

                    # Sync inventory
                    inv = Inventory.objects.filter(medicine=med).first()
                    if inv:
                        prev = inv.current_stock
                        inv.current_stock = max(0, inv.current_stock - qty)
                        inv.available_stock = max(0, inv.available_stock - qty)
                        inv.save()
                        StockTransaction.objects.create(
                            inventory=inv,
                            medicine=med,
                            batch=batch,
                            transaction_type="SALE",
                            quantity=qty,
                            previous_stock=prev,
                            new_stock=inv.current_stock,
                            reference_number=sale.sale_number,
                            performed_by=pharmacist,
                        )
                        inv_count += 1

                tax = round(subtotal * Decimal("0.05"), 2)
                total = subtotal + tax
                Sale.objects.filter(pk=sale.pk).update(
                    subtotal=subtotal,
                    tax_amount=tax,
                    total_amount=total,
                    amount_paid=total,
                )
                sale_count += 1

        self.stdout.write(self.style.SUCCESS(
            f"   âœ“ {sale_count} sales + {inv_count} SALE stock transactions created."
        ))

    # â”€â”€â”€ DISPENSING â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _seed_dispensing(self, medicines, batches):
        from dispensing.models import (
            MedicineRequest, MedicineRequestItem, Prescription,
            PrescriptionItem, DispensingRecord, DispensingAllocation,
        )

        if DispensingRecord.objects.count() > 0:
            self.stdout.write(f"   â„¹  Dispensing records exist â€” skipping.")
            return

        pharmacist = (
            User.objects.filter(role="PHARMACIST", status="APPROVED").first()
            or User.objects.filter(role="ADMIN").first()
        )
        if not pharmacist:
            self.stdout.write(self.style.WARNING("   âš   No pharmacist found â€” skipping dispensing seed."))
            return

        DOCTORS = ["Dr. Ravi Anand", "Dr. Meena Iyer", "Dr. Suresh Gupta", "Dr. Farida Khan"]
        DEPTS = ["General Medicine", "Cardiology", "Endocrinology", "Orthopedics"]

        today = timezone.now().date()
        record_count = 0

        for i in range(30):
            med = random.choice([m for m in medicines if m.batches.filter(quantity_available__gt=3).exists()])
            batch = (
                med.batches.filter(quantity_available__gt=3, status="ACTIVE")
                .order_by("expiry_date")
                .first()
            )
            if not batch:
                continue

            qty = random.randint(1, 5)
            if batch.quantity_available < qty:
                qty = batch.quantity_available
            if qty < 1:
                continue

            patient_num = str(i + 1).zfill(4)
            disp_id = DispensingRecord.generate_dispensing_id()

            record = DispensingRecord.objects.create(
                dispensing_id=disp_id,
                patient_name=f"Patient-{patient_num}",
                patient_id=f"UHID-{patient_num}",
                medicine=med,
                total_quantity=qty,
                pharmacist=pharmacist,
                regulatory_category=med.regulatory_category,
                status="COMPLETED",
                verification_checklist={
                    "rx_verified": True,
                    "medicine_verified": True,
                    "batch_verified": True,
                    "expiry_verified": True,
                    "stock_verified": True,
                    "fefo_applied": True,
                    "regulatory_verified": True,
                },
                notes="Seeded demo dispensing record.",
            )

            DispensingAllocation.objects.create(
                dispensing=record,
                batch=batch,
                quantity=qty,
                unit_price=batch.selling_price,
                total_price=batch.selling_price * qty,
            )

            # Deduct from batch + inventory
            batch.quantity_available = max(0, batch.quantity_available - qty)
            batch.save(update_fields=["quantity_available"])

            inv = Inventory.objects.filter(medicine=med).first()
            if inv:
                prev = inv.current_stock
                inv.current_stock = max(0, inv.current_stock - qty)
                inv.available_stock = max(0, inv.available_stock - qty)
                inv.save()
                StockTransaction.objects.create(
                    inventory=inv,
                    medicine=med,
                    batch=batch,
                    transaction_type="DISPENSE",
                    quantity=qty,
                    previous_stock=prev,
                    new_stock=inv.current_stock,
                    reference_number=disp_id,
                    performed_by=pharmacist,
                )

            record_count += 1

        self.stdout.write(self.style.SUCCESS(f"   âœ“ {record_count} dispensing records created."))

