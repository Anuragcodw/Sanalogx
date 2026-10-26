"""
reports/csv_service.py
Phase 7 — CSV Report Generator
"""

import csv
import io


def generate_csv_bytes(report_data):
    """
    Generate clean UTF-8 CSV from report data.
    Returns bytes (UTF-8 with BOM for Excel compatibility).
    """
    buf = io.StringIO()
    columns = report_data.get('columns', [])
    data_rows = report_data.get('data', [])

    writer = csv.writer(buf)
    if columns:
        writer.writerow(columns)

    for row in data_rows:
        if isinstance(row, dict):
            writer.writerow([str(v) if v is not None else '' for v in row.values()])
        else:
            writer.writerow([str(v) if v is not None else '' for v in row])

    # UTF-8 BOM for Excel compatibility
    return ('\ufeff' + buf.getvalue()).encode('utf-8')
