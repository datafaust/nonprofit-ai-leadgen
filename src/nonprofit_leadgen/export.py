"""Spreadsheet-safe CSV exports."""
import csv
import re
from pathlib import Path

from .normalize import BOROUGHS

EXPORT_FIELDS = ("organization_id", "name", "description", "borough", "street_address", "city", "state", "zip_code", "website", "email", "phone", "category", "data_source", "source_record_id", "source_url", "last_updated", "date_ingested", "contact_verification_status", "lead_qualification_status")
FORMULA = re.compile(r"^[\s\x00-\x1f\x7f]*[=+\-@]")

def safe_cell(value):
    if value is None:
        return ""
    value = str(value)
    return "'" + value if FORMULA.match(value) else value

def export_csv(db, output, borough=None):
    if borough:
        borough = BOROUGHS.get(borough.casefold())
        if not borough:
            raise ValueError("unknown borough")
    query = """SELECT o.*, GROUP_CONCAT(s.source, ';') AS data_source,
                      GROUP_CONCAT(s.source_record_id, ';') AS source_record_id,
                      GROUP_CONCAT(s.source_url, ';') AS source_url
               FROM organizations o JOIN source_records s USING(organization_id)"""
    params = ()
    if borough:
        query += " WHERE o.borough=?"
        params = (borough,)
    query += " GROUP BY o.organization_id ORDER BY o.name, o.organization_id"
    rows = db.execute(query, params)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with output.open("w", newline="", encoding="utf-8", errors="strict") as f:
        writer = csv.DictWriter(f, fieldnames=EXPORT_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: safe_cell(row[field]) for field in EXPORT_FIELDS})
            count += 1
    return count
