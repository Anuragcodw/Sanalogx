"""
integrations/api_views.py
MediVault — FHIR R4 REST API Views

Standards-compliant endpoints for EHR order exchange (Oracle Health / Cerner & Mock EHR).
"""

import json
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.core.exceptions import ValidationError
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from dispensing.models import DispensingRecord
from .models import ExternalHospitalSystem, HospitalMedicationOrder, IntegrationMessageLog
from . import fhir_service


def _get_authenticated_system(request):
    """
    Extracts and authenticates ExternalHospitalSystem via API key.
    Checks 'X-MediVault-API-Key' or 'Authorization: Bearer <key>' headers.
    Returns None if no key provided (allows demo/mock mode).
    """
    key = request.headers.get('X-MediVault-API-Key')
    if not key and 'Authorization' in request.headers:
        auth_val = request.headers.get('Authorization', '')
        if auth_val.startswith('Bearer '):
            key = auth_val.split(' ', 1)[1].strip()

    if key:
        return ExternalHospitalSystem.objects.filter(api_key=key, is_active=True).first()
    return None


@method_decorator(csrf_exempt, name='dispatch')
class FHIRMedicationRequestAPIView(APIView):
    """
    FHIR R4 MedicationRequest Ingestion Endpoint.
    POST /api/v1/fhir/MedicationRequest/
    """
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        payload = request.data
        system = _get_authenticated_system(request)
        ip = request.META.get('REMOTE_ADDR')

        try:
            order, is_created = fhir_service.ingest_fhir_medication_order(
                payload=payload,
                hospital_system=system,
                ip_address=ip
            )
            response_code = status.HTTP_201_CREATED if is_created else status.HTTP_200_OK
            return Response({
                "resourceType": "MedicationRequest",
                "id": order.external_order_id,
                "status": "draft" if order.status == 'PENDING_REVIEW' else "active",
                "medivaultStatus": order.get_status_display(),
                "prescriptionId": order.prescription.prescription_id if order.prescription else None,
                "pharmacistReviewRequired": True,
                "isDuplicate": not is_created,
                "receivedAt": order.received_at.isoformat(),
            }, status=response_code)

        except ValidationError as e:
            # Return FHIR OperationOutcome on validation error
            outcome = {
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "invalid",
                        "diagnostics": str(e)
                    }
                ]
            }
            IntegrationMessageLog.objects.create(
                direction=IntegrationMessageLog.Direction.INBOUND,
                hospital_system=system,
                resource_type='MedicationRequest',
                request_method='POST',
                request_path=request.path,
                status_code=400,
                status=IntegrationMessageLog.Status.VALIDATION_ERROR,
                payload=payload if isinstance(payload, dict) else {},
                error_message=str(e),
                ip_address=ip
            )
            return Response(outcome, status=status.HTTP_400_BAD_REQUEST)

        except Exception as e:
            outcome = {
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "fatal",
                        "code": "exception",
                        "diagnostics": f"Internal integration error: {str(e)}"
                    }
                ]
            }
            return Response(outcome, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def get(self, request, external_id=None):
        """
        Retrieves status of a previously submitted MedicationRequest.
        """
        if not external_id:
            orders = HospitalMedicationOrder.objects.all()[:50]
            data = [
                {
                    "resourceType": "MedicationRequest",
                    "id": o.external_order_id,
                    "patientName": o.patient_name,
                    "medicationName": o.raw_medication_name,
                    "status": o.status,
                    "receivedAt": o.received_at.isoformat()
                }
                for o in orders
            ]
            return Response({"resourceType": "Bundle", "type": "searchset", "entry": data})

        order = HospitalMedicationOrder.objects.filter(external_order_id=external_id).first()
        if not order:
            return Response({
                "resourceType": "OperationOutcome",
                "issue": [{"severity": "error", "code": "not-found", "diagnostics": f"Order '{external_id}' not found."}]
            }, status=status.HTTP_404_NOT_FOUND)

        return Response({
            "resourceType": "MedicationRequest",
            "id": order.external_order_id,
            "status": "draft" if order.status == 'PENDING_REVIEW' else "completed" if order.status == 'DISPENSED' else "active",
            "medivaultStatus": order.get_status_display(),
            "patient": {"display": order.patient_name, "uhid": order.patient_uhid},
            "medication": order.raw_medication_name,
            "quantity": order.prescribed_quantity,
            "pharmacistReviewRequired": order.status == 'PENDING_REVIEW',
            "prescriptionId": order.prescription.prescription_id if order.prescription else None,
            "dispensingId": order.dispensing_record.dispensing_id if order.dispensing_record else None,
        })


@method_decorator(csrf_exempt, name='dispatch')
class FHIRMedicationDispenseAPIView(APIView):
    """
    FHIR R4 MedicationDispense Retrieval Endpoint.
    GET /api/v1/fhir/MedicationDispense/<dispensing_id>/
    """
    authentication_classes = []
    permission_classes = []

    def get(self, request, dispensing_id):
        disp = DispensingRecord.objects.filter(dispensing_id=dispensing_id).first()
        if not disp:
            return Response({
                "resourceType": "OperationOutcome",
                "issue": [{"severity": "error", "code": "not-found", "diagnostics": f"Dispensing record '{dispensing_id}' not found."}]
            }, status=status.HTTP_404_NOT_FOUND)

        payload = fhir_service.generate_fhir_medication_dispense(disp)
        return Response(payload)


@method_decorator(csrf_exempt, name='dispatch')
class FHIRCapabilityStatementAPIView(APIView):
    """
    FHIR R4 CapabilityStatement / Metadata Endpoint.
    GET /api/v1/fhir/metadata/
    """
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        return Response(fhir_service.get_fhir_capability_statement())


@method_decorator(csrf_exempt, name='dispatch')
class FHIRMockBundleAPIView(APIView):
    """
    Accepts a FHIR R4 Bundle containing multiple MedicationRequests for automated ingestion testing.
    POST /api/v1/fhir/mock-bundle/
    """
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        payload = request.data
        if not isinstance(payload, dict) or payload.get('resourceType') != 'Bundle':
            return Response({"error": "Expected resourceType 'Bundle'"}, status=status.HTTP_400_BAD_REQUEST)

        entries = payload.get('entry', [])
        results = []
        system = _get_authenticated_system(request)
        ip = request.META.get('REMOTE_ADDR')

        for entry in entries:
            res = entry.get('resource', {})
            if res.get('resourceType') == 'MedicationRequest':
                try:
                    order, created = fhir_service.ingest_fhir_medication_order(res, hospital_system=system, ip_address=ip)
                    results.append({
                        "id": order.external_order_id,
                        "status": "created" if created else "duplicate",
                        "orderId": order.id
                    })
                except Exception as e:
                    results.append({"error": str(e), "id": res.get('id', 'unknown')})

        return Response({"resourceType": "Bundle", "type": "batch-response", "processed": results})
