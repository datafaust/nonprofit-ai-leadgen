"""SQLite persistence with source provenance and conservative identity links."""
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

FIELDS = ("name", "description", "borough", "street_address", "city", "state", "zip_code", "website", "email", "phone", "category", "last_updated", "contact_verification_status", "lead_qualification_status")

def connect(path="data/nonprofits.db"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS organizations (
      organization_id TEXT PRIMARY KEY, match_key TEXT UNIQUE,
      name TEXT NOT NULL, description TEXT, borough TEXT, street_address TEXT,
      city TEXT, state TEXT, zip_code TEXT, website TEXT, email TEXT, phone TEXT,
      category TEXT, last_updated TEXT, date_ingested TEXT NOT NULL,
      contact_verification_status TEXT NOT NULL, lead_qualification_status TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS source_records (
      source TEXT NOT NULL, source_record_id TEXT NOT NULL,
      organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
      source_url TEXT NOT NULL, claimed_website TEXT, extracted_at TEXT NOT NULL,
      extraction_method TEXT NOT NULL DEFAULT 'official_csv',
      PRIMARY KEY (source, source_record_id)
    );
    CREATE TABLE IF NOT EXISTS website_checks (
      check_id INTEGER PRIMARY KEY,
      organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
      claimed_url TEXT NOT NULL, checked_at TEXT NOT NULL,
      status TEXT NOT NULL, final_url TEXT, http_status INTEGER,
      error_type TEXT
    );
    CREATE TABLE IF NOT EXISTS website_email_scans (
      scan_id INTEGER PRIMARY KEY,
      organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
      claimed_url TEXT NOT NULL, scanned_at TEXT NOT NULL,
      status TEXT NOT NULL, pages_visited INTEGER NOT NULL,
      emails_found INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS website_emails (
      organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
      claimed_url TEXT NOT NULL, email TEXT NOT NULL,
      source_url TEXT NOT NULL, discovered_at TEXT NOT NULL,
      extraction_method TEXT NOT NULL,
      verification_status TEXT NOT NULL DEFAULT 'unverified',
      PRIMARY KEY (organization_id, claimed_url, email)
    );
    CREATE INDEX IF NOT EXISTS idx_source_org ON source_records(organization_id);
    CREATE INDEX IF NOT EXISTS idx_org_borough ON organizations(borough);
    CREATE INDEX IF NOT EXISTS idx_website_checks ON website_checks(organization_id, claimed_url, check_id DESC);
    CREATE INDEX IF NOT EXISTS idx_email_scans ON website_email_scans(organization_id, claimed_url, scan_id DESC);
    CREATE INDEX IF NOT EXISTS idx_website_emails_org ON website_emails(organization_id, claimed_url);
    """)
    # Add the source claim to databases created before website checking existed.
    columns = {row[1] for row in db.execute("PRAGMA table_info(source_records)")}
    if "claimed_website" not in columns:
        db.execute("ALTER TABLE source_records ADD COLUMN claimed_website TEXT")
    db.commit()
    return db

def save(db, records):
    count = 0
    now = datetime.now(timezone.utc).isoformat()
    with db:
        for record in records:
            existing = db.execute("SELECT organization_id FROM source_records WHERE source=? AND source_record_id=?", (record["source"], record["source_record_id"])).fetchone()
            matched = db.execute("SELECT organization_id FROM organizations WHERE match_key=?", (record["match_key"],)).fetchone() if record["match_key"] else None
            org_id = existing[0] if existing else matched[0] if matched else str(uuid.uuid4())
            if existing or matched:
                # Preserve nonempty facts already imported from another source.
                for field in FIELDS:
                    if field in ("contact_verification_status", "lead_qualification_status"):
                        continue
                    if record.get(field) is not None:
                        db.execute(f"UPDATE organizations SET {field}=? WHERE organization_id=?", (record[field], org_id))
                db.execute("""UPDATE organizations SET contact_verification_status='source_reported'
                              WHERE organization_id=? AND contact_verification_status='none'
                              AND (email IS NOT NULL OR phone IS NOT NULL)""", (org_id,))
            else:
                columns = ("organization_id", "match_key", *FIELDS, "date_ingested")
                values = (org_id, record["match_key"], *(record.get(f) for f in FIELDS), now)
                db.execute(f"INSERT INTO organizations ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", values)
            db.execute("""INSERT INTO source_records (source, source_record_id, organization_id, source_url, claimed_website, extracted_at)
                          VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(source, source_record_id) DO UPDATE SET
                          source_url=excluded.source_url, claimed_website=excluded.claimed_website,
                          extracted_at=excluded.extracted_at""",
                       (record["source"], record["source_record_id"], org_id, record["source_url"], record["website"], now))
            count += 1
    return count

def stats(db):
    return {
        "organizations": db.execute("SELECT COUNT(*) FROM organizations").fetchone()[0],
        "sources": [tuple(r) for r in db.execute("SELECT source, COUNT(*) FROM source_records GROUP BY source ORDER BY source")],
        "boroughs": [tuple(r) for r in db.execute("SELECT COALESCE(borough, 'Unknown'), COUNT(*) FROM organizations GROUP BY borough ORDER BY borough")],
        "websites": db.execute("SELECT COUNT(*) FROM organizations WHERE website IS NOT NULL").fetchone()[0],
        "emails": db.execute("SELECT COUNT(*) FROM organizations WHERE email IS NOT NULL").fetchone()[0],
        "phones": db.execute("SELECT COUNT(*) FROM organizations WHERE phone IS NOT NULL").fetchone()[0],
    }
