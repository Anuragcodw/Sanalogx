"""
dispensing/test_rbac_and_navigation.py
MediVault / Sanalogx — Regression test suite for:
  1. Role-Based Navigation Audit (Admin vs Pharmacist sidebars)
  2. Removal of Secure Dispensing from Admin navigation
  3. Server-side permission enforcement on direct dispensing workspace and API execution
  4. Preservation of Admin governance and oversight features (Medicine Master, Inventory, Reports, Procurement, etc.)
  5. Preservation of Pharmacist operational capabilities (POS, Dispensing, Batch availability)
  6. Medicine Master filter panel unclipped rendering and form controls
"""

import json
from django.test import TestCase, Client
from django.urls import reverse
from accounts.models import User, PharmacistProfile
from medicines.models import Medicine, Category, MedicineBatch


class RoleBasedNavigationAndDispensingRBACTestCase(TestCase):
    def setUp(self):
        self.client = Client()

        # Create Category
        self.category = Category.objects.create(name='Cardiology', is_active=True)

        # Create Admin User
        self.admin_user = User.objects.create_user(
            username='admin_governance',
            email='admin@hospital.org',
            password='AdminPassword123!',
            role='ADMIN',
            status='APPROVED',
            is_staff=True,
            is_superuser=True
        )

        # Create Approved Pharmacist User
        self.pharmacist_user = User.objects.create_user(
            username='pharmacist_ops',
            email='pharmacist@hospital.org',
            password='PharmPassword123!',
            role='PHARMACIST',
            status='APPROVED',
            first_login_completed=True
        )
        self.pharmacist_user.email_verified = True
        self.pharmacist_user.save()
        PharmacistProfile.objects.create(
            user=self.pharmacist_user,
            registration_number='PH-VERIF-9988',
            qualification='B.Pharm',
            is_demo=False,
            verification_status='VERIFIED'
        )

        # Create Pending / Unapproved Pharmacist User
        self.pending_pharmacist = User.objects.create_user(
            username='pharmacist_pending',
            email='pending@hospital.org',
            password='PendingPassword123!',
            role='PHARMACIST',
            status='PENDING'
        )

        # Create sample Medicine
        self.medicine = Medicine.objects.create(
            medicine_name='Amlodipine Besylate 5mg',
            generic_name='Amlodipine',
            category=self.category,
            dosage_form='Tablet',
            strength='5 mg',
            status=Medicine.Status.ACTIVE,
            regulatory_category=Medicine.RegulatoryCategory.NORMAL
        )

    # ── Test 1: Admin Sidebar does NOT contain Secure Dispensing ──────────────
    def test_admin_sidebar_does_not_contain_secure_dispensing(self):
        """Admin dashboard/sidebar navigation must NOT link to Secure Dispensing."""
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse('admin_approvals'))
        self.assertEqual(response.status_code, 200)

        # Verify Admin sidebar items
        content = response.content.decode('utf-8')
        self.assertIn('Medicine Master', content)
        self.assertIn('Inventory Control', content)
        self.assertIn('Controlled Register', content)
        self.assertIn('Discrepancies & Audit', content)
        self.assertIn('Sales & Billing', content)
        self.assertIn('Procurement', content)

        # Verify Secure Dispensing is NOT present in the admin navigation
        self.assertNotIn('/dispensing/workspace/', content)
        self.assertNotIn('Secure Dispensing</span>', content)

    # ── Test 2: Pharmacist Sidebar DOES contain Secure Dispensing ─────────────
    def test_pharmacist_sidebar_contains_secure_dispensing(self):
        """Authorized Pharmacist workspace MUST display Secure Dispensing navigation."""
        self.client.force_login(self.pharmacist_user)
        response = self.client.get(reverse('approved'))
        self.assertEqual(response.status_code, 200)

        content = response.content.decode('utf-8')
        self.assertIn('/dispensing/workspace/', content)
        self.assertIn('Secure Dispensing', content)
        self.assertIn('Dispensing History', content)

    # ── Test 3: Admin Direct URL Access to Dispensing Workspace Redirects ────
    def test_admin_direct_access_to_dispensing_workspace_redirects(self):
        """Admin navigating directly to /dispensing/workspace/ is redirected to oversight history."""
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse('secure_dispensing_workspace'), follow=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/dispensing/history/', response.url)

        # Follow redirect and verify oversight page renders cleanly
        redirected_response = self.client.get(reverse('secure_dispensing_workspace'), follow=True)
        self.assertEqual(redirected_response.status_code, 200)
        self.assertContains(redirected_response, 'Dispensing Audit &amp; Traceability History')

    # ── Test 4: Admin API Access to Execute Dispense is Blocked (403) ─────────
    def test_admin_api_secure_dispense_blocked(self):
        """Admin or unauthorized user calling the dispensing execution API gets HTTP 403."""
        self.client.force_login(self.admin_user)
        payload = json.dumps({
            'medicine_id': self.medicine.id,
            'requested_qty': 10,
            'patient_name': 'Test Patient',
            'patient_id': 'PAT-001'
        })
        response = self.client.post(
            reverse('api_secure_dispense'),
            data=payload,
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(response.status_code, 403)
        data = response.json()
        self.assertFalse(data.get('success'))

    # ── Test 5: Pharmacist Direct Access to Workspace Allowed (200) ───────────
    def test_pharmacist_access_to_dispensing_workspace_allowed(self):
        """Approved pharmacist accessing /dispensing/workspace/ loads successfully."""
        self.client.force_login(self.pharmacist_user)
        response = self.client.get(reverse('secure_dispensing_workspace'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Secure Dispensing &amp; Traceability Workspace')
        self.assertContains(response, 'Amlodipine Besylate 5mg')

    # ── Test 6: Unauthenticated or Pending User Blocked from Dispensing ───────
    def test_unauthenticated_or_pending_user_blocked_from_workspace(self):
        """Anonymous and pending users cannot access dispensing workspace."""
        # Anonymous
        res_anon = self.client.get(reverse('secure_dispensing_workspace'))
        self.assertEqual(res_anon.status_code, 302)

        # Pending Pharmacist
        self.client.force_login(self.pending_pharmacist)
        res_pending = self.client.get(reverse('secure_dispensing_workspace'))
        self.assertEqual(res_pending.status_code, 302)

    # ── Test 7: Admin Retains Full Governance & Oversight Access ─────────────
    def test_admin_governance_routes_accessible(self):
        """Verify Admin maintains access to all authorized oversight routes."""
        self.client.force_login(self.admin_user)

        routes_to_test = [
            'medicine_list',
            'inventory_dashboard',
            'controlled_register',
            'discrepancy_list',
            'sales_history',
            'purchase_orders_list',
            'reports_center',
            'notification_list',
            'category_list',
        ]
        for route in routes_to_test:
            res = self.client.get(reverse(route))
            self.assertEqual(res.status_code, 200, f"Route {route} failed for Admin.")

    # ── Test 8: Pharmacist Operational Routes Accessible ─────────────────────
    def test_pharmacist_operational_routes_accessible(self):
        """Verify Pharmacist maintains access to authorized operational routes."""
        self.client.force_login(self.pharmacist_user)

        routes_to_test = [
            'medicine_list',
            'inventory_dashboard',
            'new_sale',
            'sales_history',
            'batch_stock',
            'controlled_register',
            'discrepancy_list',
            'dispensing_history',
        ]
        for route in routes_to_test:
            res = self.client.get(reverse(route))
            self.assertEqual(res.status_code, 200, f"Route {route} failed for Pharmacist.")

    # ── Test 9: Medicine Master Filter Panel Unclipped Rendering ──────────────
    def test_medicine_master_filter_panel_classes_and_text_visibility(self):
        """Verify Medicine Master filter controls have the unclipped med-filter classes and attributes."""
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse('medicine_list'))
        self.assertEqual(response.status_code, 200)

        # Check filter panel components
        self.assertContains(response, 'med-filter-control')
        self.assertContains(response, 'med-filter-search')
        self.assertContains(response, 'med-filter-select')
        self.assertContains(response, 'med-filter-label')
        self.assertContains(response, 'Search by name, generic, barcode...')
        self.assertContains(response, 'All Categories')
        self.assertContains(response, 'All Forms')
        self.assertContains(response, 'All Schedules')
        self.assertContains(response, 'Name (A → Z)')
        self.assertContains(response, 'Requires Prescription (Rx)')
        self.assertContains(response, 'Controlled Substances (H1/X/NDPS)')

    # ── Test 10: Medicine Master Filter Functionality Intact ───────────────────
    def test_medicine_master_filter_functionality(self):
        """Verify that searching and filtering continue to return expected records."""
        self.client.force_login(self.admin_user)

        # Search query
        res_search = self.client.get(reverse('medicine_list'), {'search': 'Amlodipine'})
        self.assertEqual(res_search.status_code, 200)
        self.assertContains(res_search, 'Amlodipine Besylate 5mg')

        # Filter by category
        res_cat = self.client.get(reverse('medicine_list'), {'category': self.category.id})
        self.assertEqual(res_cat.status_code, 200)
        self.assertContains(res_cat, 'Amlodipine Besylate 5mg')

        # Filter by non-matching query
        res_none = self.client.get(reverse('medicine_list'), {'search': 'NonExistentXYZ999'})
        self.assertEqual(res_none.status_code, 200)
        self.assertContains(res_none, 'No medicines found')
