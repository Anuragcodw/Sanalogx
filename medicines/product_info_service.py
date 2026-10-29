"""
medicines/product_info_service.py
Phase 3 — External Product Information Lookup Service

Responsibilities:
1. Queries trusted external product-data sources (e.g. OpenFDA Drug API or configured registry).
2. Validates and sanitizes returned data before presenting it.
3. Clearly flags external information as unverified reference data.
4. CRITICAL: Never overwrites or modifies existing database medicine records.
5. Gracefully handles unconfigured, disabled, or offline external providers.
"""

import json
import logging
import urllib.request
import urllib.error
from django.conf import settings

logger = logging.getLogger(__name__)

# Timeout in seconds for external HTTP queries
EXTERNAL_LOOKUP_TIMEOUT = 3.0


def is_external_lookup_enabled() -> bool:
    """Returns True if external product lookup is explicitly enabled in settings."""
    return getattr(settings, 'EXTERNAL_PRODUCT_LOOKUP_ENABLED', False)


def lookup_external_product_info(identifier: str, id_type: str = 'barcode', generic_name: str = None) -> dict:
    """
    Safely queries a trusted external medicine knowledge source (OpenFDA or configured source).
    - strictly read-only: never alters local database records.
    - clearly distinguishes external information from local formulary records.
    - validates all response fields.
    - returns clean fallback when not configured, network fails, or record not found.
    """
    if not identifier and not generic_name:
        return {
            'available': False,
            'source': None,
            'is_external': True,
            'verified_locally': False,
            'message': 'No valid identifier or generic name provided for external lookup.'
        }

    # If external lookups are disabled in configuration, return clean inactive status
    if not is_external_lookup_enabled():
        return {
            'available': False,
            'source': None,
            'is_external': True,
            'verified_locally': False,
            'message': 'External product data source is not configured or disabled.'
        }

    # Attempt query against trusted OpenFDA drug label API
    query_param = None
    if generic_name:
        sanitized_name = ''.join(c for c in generic_name if c.isalnum() or c in ' -').strip()
        query_param = f'openfda.generic_name:"{sanitized_name}"'
    elif identifier and id_type == 'barcode':
        sanitized_code = ''.join(c for c in identifier if c.isalnum()).strip()
        if sanitized_code:
            query_param = f'openfda.package_ndc:"{sanitized_code}"'

    if not query_param:
        return {
            'available': False,
            'source': 'OpenFDA',
            'is_external': True,
            'verified_locally': False,
            'message': 'No suitable search parameters for external database query.'
        }

    url = f"https://api.fda.gov/drug/label.json?search={urllib.parse.quote(query_param)}&limit=1"

    try:
        req = urllib.request.Request(
            url,
            headers={'User-Agent': 'Sanalogx-Hospital-Pharmacy/1.0'}
        )
        with urllib.request.urlopen(req, timeout=EXTERNAL_LOOKUP_TIMEOUT) as response:
            if response.status == 200:
                raw_data = json.loads(response.read().decode('utf-8'))
                results = raw_data.get('results', [])
                if results:
                    first_res = results[0]
                    openfda = first_res.get('openfda', {})

                    brand_names = openfda.get('brand_name', [])
                    generic_names = openfda.get('generic_name', [])
                    mfr_names = openfda.get('manufacturer_name', [])
                    substance = openfda.get('substance_name', [])
                    route = openfda.get('route', [])

                    # Extract verified indication text if available
                    indications_list = first_res.get('indications_and_usage', [])
                    indications_text = indications_list[0] if indications_list else ''
                    if len(indications_text) > 300:
                        indications_text = indications_text[:297] + '...'

                    # Extract storage instructions if available
                    storage_list = first_res.get('storage_and_handling', [])
                    storage_text = storage_list[0] if storage_list else ''
                    if len(storage_text) > 200:
                        storage_text = storage_text[:197] + '...'

                    return {
                        'available': True,
                        'source': 'OpenFDA Drug Label Database',
                        'is_external': True,
                        'verified_locally': False,
                        'disclaimer': 'External regulatory product data provided for clinical reference only. Local hospital formulary records remain authoritative.',
                        'data': {
                            'brand_name': brand_names[0] if brand_names else 'N/A',
                            'generic_name': generic_names[0] if generic_names else (generic_name or 'N/A'),
                            'manufacturer': mfr_names[0] if mfr_names else 'N/A',
                            'substance_name': substance[0] if substance else 'N/A',
                            'route': route[0] if route else 'N/A',
                            'indications_summary': indications_text or 'Refer to official prescribing monograph.',
                            'storage_summary': storage_text or 'Standard regulated storage conditions.',
                        }
                    }
    except urllib.error.HTTPError as e:
        logger.debug(f"External product lookup HTTP error: {e.code}")
    except urllib.error.URLError as e:
        logger.debug(f"External product lookup URL error: {e.reason}")
    except Exception as e:
        logger.warning(f"Unexpected error in external product lookup: {e}")

    return {
        'available': False,
        'source': 'OpenFDA',
        'is_external': True,
        'verified_locally': False,
        'message': 'External product data not found or source unreachable.'
    }
