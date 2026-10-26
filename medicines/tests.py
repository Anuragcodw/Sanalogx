from django.test import TestCase, Client
from django.urls import reverse
from accounts.models import User
from medicines.models import Medicine, Category, MedicineBatch
import json

class MedicineMasterAndAITests(TestCase):
    def setUp(self):
        self.client = Client()
        # Create Category
        self.category = Category.objects.create(name='Cardiology', is_active=True)
        self.category2 = Category.objects.create(name='Antibiotics', is_active=True)

        # Create Admin
        self.admin_user = User.objects.create_user(
            username='admin_test',
            email='admin@hospital.org',
            password='Password123!',
            role='ADMIN',
            status='APPROVED',
            is_staff=True,
            is_superuser=True
        )

        # Create Approved Pharmacist
        self.pharm_user = User.objects.create_user(
            username='pharm_test',
            email='pharm@hospital.org',
            password='Password123!',
            role='PHARMACIST',
            status='APPROVED'
        )

        # Create test medicines
        self.med1 = Medicine.objects.create(
            medicine_name='Amlodipine 5mg',
            generic_name='Amlodipine',
            brand_name='Norvasc',
            category=self.category,
            dosage_form='Tablet',
            strength='5 mg',
            manufacturer='Pfizer',
            unit_of_measure='Strip',
            regulatory_category=Medicine.RegulatoryCategory.SCHEDULE_H,
            status=Medicine.Status.ACTIVE
        )
        self.med2 = Medicine.objects.create(
            medicine_name='Amoxicillin 500mg',
            generic_name='Amoxicillin',
            category=self.category2,
            dosage_form='Capsule',
            strength='500 mg',
            manufacturer='GSK',
            unit_of_measure='Box',
            regulatory_category=Medicine.RegulatoryCategory.NORMAL,
            status=Medicine.Status.ACTIVE
        )

    def test_medicine_master_page_renders_with_metrics_and_ai_widget(self):
        """Verify Medicine Master listing displays statistics cards, results, and AI widget."""
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse('medicine_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Medicine Master')
        self.assertContains(response, 'med-metrics-grid')
        self.assertContains(response, 'Total Medicines')
        self.assertContains(response, 'Amlodipine 5mg')
        self.assertContains(response, 'Amoxicillin 500mg')
        # Check AI widget is present
        self.assertContains(response, 'medivoiceWidgetContainer')
        self.assertContains(response, 'medivoiceTriggerBtn')
        self.assertContains(response, 'medivoiceModal')

    def test_medicine_master_filters_and_empty_state(self):
        """Verify filtering criteria work and empty state is rendered when no matches exist."""
        self.client.force_login(self.admin_user)

        # Filter by category
        res_cat = self.client.get(reverse('medicine_list'), {'category': self.category.id})
        self.assertEqual(res_cat.status_code, 200)
        self.assertContains(res_cat, 'Amlodipine 5mg')
        self.assertNotContains(res_cat, 'Amoxicillin 500mg')

        # Filter with non-matching query -> triggers empty state
        res_empty = self.client.get(reverse('medicine_list'), {'search': 'NonExistentMedicine999'})
        self.assertEqual(res_empty.status_code, 200)
        self.assertContains(res_empty, 'No medicines found')
        self.assertContains(res_empty, 'Add a medicine profile or adjust your filters')

    def test_add_medicine_page_renders_stepper_and_ai_widget(self):
        """Verify Add New Medicine renders 5-step stepper and floating AI assistant."""
        self.client.force_login(self.pharm_user)
        response = self.client.get(reverse('medicine_add'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Add New Medicine')
        self.assertContains(response, 'med-stepper-nav')
        self.assertContains(response, '1. Basic Medicine Information')
        self.assertContains(response, '2. Drug Classification & Units')
        self.assertContains(response, '3. Identification & Packaging Visual')
        self.assertContains(response, '4. Pharmacy, Regulatory & Reorder Settings')
        self.assertContains(response, '5. Capacity Limits, Clinical Notes & Submission')
        # Check AI widget
        self.assertContains(response, 'medivoiceWidgetContainer')

    def test_add_medicine_form_submission_success(self):
        """Verify successful creation of a new medicine profile with validation."""
        self.client.force_login(self.admin_user)
        payload = {
            'medicine_name': 'Metformin 500mg ER',
            'generic_name': 'Metformin Hydrochloride',
            'brand_name': 'Glucophage',
            'category': self.category.id,
            'dosage_form': 'Tablet',
            'strength': '500 mg',
            'manufacturer': 'Merck',
            'unit_of_measure': 'Box',
            'regulatory_category': 'NORMAL',
            'reorder_level': 60,
            'minimum_stock': 15,
            'maximum_stock': 600,
            'safety_stock': 25,
            'status': 'ACTIVE',
            'description': 'Oral antihyperglycemic agent for T2DM.',
        }
        response = self.client.post(reverse('medicine_add'), data=payload)
        self.assertEqual(response.status_code, 302)
        created_med = Medicine.objects.filter(medicine_name='Metformin 500mg ER').first()
        self.assertIsNotNone(created_med)
        self.assertEqual(created_med.generic_name, 'Metformin Hydrochloride')
        self.assertEqual(created_med.regulatory_category, 'NORMAL')

    def test_duplicate_check_api(self):
        """Verify duplicate check API endpoint identifies matching generic/strength/dosage."""
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse('medicine_duplicate_check'), {
            'generic_name': 'Amlodipine',
            'strength': '5 mg',
            'dosage_form': 'Tablet'
        })
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get('exists'))

    def test_ai_chat_api_endpoint(self):
        """Verify the AI Assistant backend endpoint processes queries accurately."""
        self.client.force_login(self.admin_user)
        response = self.client.post(
            reverse('api_ai_chat'),
            data=json.dumps({'message': 'Check low stock medicines'}),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get('success'))
        self.assertIn('answer', data)
