"""Read-only, local-only lead browser."""
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from flask import Flask, abort, render_template, request

from .normalize import BOROUGHS

PAGE_SIZE = 50
MAX_QUERY_LENGTH = 100


def _connect_read_only(path: Path):
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def _like_pattern(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def create_app(db_path="data/nonprofits.db"):
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Database not found: {path}. Run 'uv run nonprofit-leadgen ingest' first.")
    app = Flask(__name__)
    app.config["TRUSTED_HOSTS"] = ["127.0.0.1", "localhost"]

    @app.after_request
    def security_headers(response):
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'none'; style-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index():
        query = request.args.get("q", "", type=str).strip()
        if len(query) > MAX_QUERY_LENGTH:
            abort(400)
        borough = request.args.get("borough", "", type=str).strip()
        if borough and borough.casefold() not in BOROUGHS:
            abort(400)
        borough = BOROUGHS.get(borough.casefold(), "")
        website_status = request.args.get("website_status", "", type=str).strip()
        if website_status not in ("", "reachable"):
            abort(400)
        page = request.args.get("page", 1, type=int)
        if page < 1 or page > 100_000:
            abort(400)
        filters, params = [], []
        if query:
            filters.append("(o.name LIKE ? ESCAPE '\\' OR o.description LIKE ? ESCAPE '\\')")
            pattern = _like_pattern(query)
            params.extend((pattern, pattern))
        if borough:
            filters.append("o.borough = ?")
            params.append(borough)
        if website_status == "reachable":
            filters.append("""(SELECT w.status FROM website_checks w
                              WHERE w.organization_id=o.organization_id AND w.claimed_url=o.website
                              ORDER BY w.check_id DESC LIMIT 1) = ?""")
            params.append("reachable")
        where = " WHERE " + " AND ".join(filters) if filters else ""
        with closing(_connect_read_only(path)) as db:
            total = db.execute("SELECT COUNT(*) FROM organizations o" + where, params).fetchone()[0]
            pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            if page > pages:
                abort(404)
            rows = db.execute(
                "SELECT o.organization_id, o.name, o.borough, o.category, o.website, o.phone, o.contact_verification_status, "
                "(SELECT w.status FROM website_checks w WHERE w.organization_id=o.organization_id AND w.claimed_url=o.website ORDER BY w.check_id DESC LIMIT 1) AS website_status "
                "FROM organizations o" + where + " ORDER BY o.name COLLATE NOCASE, o.organization_id LIMIT ? OFFSET ?",
                (*params, PAGE_SIZE, (page - 1) * PAGE_SIZE),
            ).fetchall()
        return render_template("leads.html", leads=rows, total=total, page=page, pages=pages, q=query, borough=borough, boroughs=BOROUGHS.values(), website_status=website_status)

    @app.get("/leads/<lead_id>")
    def detail(lead_id):
        if len(lead_id) > 64:
            abort(404)
        with closing(_connect_read_only(path)) as db:
            lead = db.execute("SELECT * FROM organizations WHERE organization_id=?", (lead_id,)).fetchone()
            if lead is None:
                abort(404)
            sources = db.execute(
                "SELECT source, source_record_id, source_url, claimed_website, extracted_at, extraction_method "
                "FROM source_records WHERE organization_id=? ORDER BY source, source_record_id", (lead_id,)
            ).fetchall()
            checks = db.execute("""SELECT claimed_url, checked_at, status, final_url, http_status, error_type
                                   FROM website_checks WHERE organization_id=? AND claimed_url=?
                                   ORDER BY check_id DESC LIMIT 5""", (lead_id, lead["website"])).fetchall()
        return render_template("detail.html", lead=lead, sources=sources, checks=checks)

    return app
