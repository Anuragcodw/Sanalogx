from django.urls import path
from . import api_views

urlpatterns = [
    path('metadata/', api_views.FHIRCapabilityStatementAPIView.as_view(), name='fhir_metadata'),
    path('MedicationRequest/', api_views.FHIRMedicationRequestAPIView.as_view(), name='fhir_medication_request_create'),
    path('MedicationRequest/<str:external_id>/', api_views.FHIRMedicationRequestAPIView.as_view(), name='fhir_medication_request_detail'),
    path('MedicationDispense/<str:dispensing_id>/', api_views.FHIRMedicationDispenseAPIView.as_view(), name='fhir_medication_dispense_detail'),
    path('mock-bundle/', api_views.FHIRMockBundleAPIView.as_view(), name='fhir_mock_bundle'),
]
