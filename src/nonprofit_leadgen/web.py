"""Local-only lead browser and human-reviewed outreach drafts."""
import hmac
import json
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from flask import Flask, abort, redirect, render_template, request, url_for

from .normalize import BOROUGHS
from .outreach import DraftError, LENGTHS, OBJECTIVES, TONES, generate_draft, list_installed_models, preferred_model
from .storage import connect, edit_outreach_draft, save_outreach_draft

PAGE_SIZE = 50
MAX_QUERY_LENGTH = 100
WEBSITE_EMAIL_EXISTS = """EXISTS (SELECT 1 FROM website_emails e
    WHERE e.organization_id=o.organization_id AND e.claimed_url=o.website)"""


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
    # Idempotent schema initialization adds the draft table to existing local databases.
    connect(path).close()
    app = Flask(__name__)
    app.config["TRUSTED_HOSTS"] = ["127.0.0.1", "localhost"]
    app.config["MAX_CONTENT_LENGTH"] = 12_000
    app.config["DRAFT_CSRF_TOKEN"] = secrets.token_urlsafe(32)

    @app.after_request
    def security_headers(response):
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
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
        email_status = request.args.get("email_status", "", type=str).strip()
        if email_status not in ("", "with_email", "without_email"):
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
        if email_status == "with_email":
            filters.append(WEBSITE_EMAIL_EXISTS)
        elif email_status == "without_email":
            filters.append("NOT " + WEBSITE_EMAIL_EXISTS)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        with closing(_connect_read_only(path)) as db:
            total = db.execute("SELECT COUNT(*) FROM organizations o" + where, params).fetchone()[0]
            pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            if page > pages:
                abort(404)
            rows = db.execute(
                "SELECT o.organization_id, o.name, o.borough, o.category, o.website, o.phone, o.contact_verification_status, "
                "(SELECT w.status FROM website_checks w WHERE w.organization_id=o.organization_id AND w.claimed_url=o.website ORDER BY w.check_id DESC LIMIT 1) AS website_status, "
                + WEBSITE_EMAIL_EXISTS + " AS has_website_email "
                "FROM organizations o" + where + " ORDER BY o.name COLLATE NOCASE, o.organization_id LIMIT ? OFFSET ?",
                (*params, PAGE_SIZE, (page - 1) * PAGE_SIZE),
            ).fetchall()
        return render_template("leads.html", leads=rows, total=total, page=page, pages=pages, q=query, borough=borough, boroughs=BOROUGHS.values(), website_status=website_status, email_status=email_status)

    def detail_page(lead_id, error=None, form=None, models=None):
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
            website_emails = db.execute("""SELECT email, source_url, discovered_at, verification_status
                FROM website_emails WHERE organization_id=? AND claimed_url=? ORDER BY email""",
                (lead_id, lead["website"])).fetchall()
            drafts = db.execute("""SELECT draft_id, objective, tone, length, model, created_at, status
                FROM outreach_drafts WHERE organization_id=? ORDER BY draft_id DESC LIMIT 30""", (lead_id,)).fetchall()
            selected = request.args.get("draft", type=int)
            draft = db.execute("SELECT * FROM outreach_drafts WHERE draft_id=? AND organization_id=?", (selected, lead_id)).fetchone() if selected else None
            if selected and draft is None:
                abort(404)
        model_error = None
        if models is None:
            try:
                models = list_installed_models()
            except DraftError as exc:
                models = []
                model_error = str(exc)
        if not models and model_error is None:
            model_error = "No writing models are installed in Ollama. Install one manually, then reload."
        return render_template("detail.html", lead=lead, sources=sources, checks=checks, website_emails=website_emails,
                               drafts=drafts, draft=draft, warnings=json.loads(draft["warnings_json"]) if draft else [],
                               objectives=OBJECTIVES, tones=TONES, lengths=LENGTHS, csrf_token=app.config["DRAFT_CSRF_TOKEN"],
                               models=models, default_model=preferred_model(models), model_error=model_error,
                               error=error, form=form or {})

    @app.get("/leads/<lead_id>")
    def detail(lead_id):
        return detail_page(lead_id)

    def require_csrf():
        token = request.form.get("csrf_token", "")
        if not hmac.compare_digest(token, app.config["DRAFT_CSRF_TOKEN"]):
            abort(403)
        if request.content_length is not None and request.content_length > 12_000:
            abort(413)

    @app.post("/leads/<lead_id>/drafts")
    def create_draft(lead_id):
        require_csrf()
        form = {key: request.form.get(key, "") for key in ("objective", "tone", "length", "instructions", "model")}
        if len(lead_id) > 64:
            abort(404)
        with closing(_connect_read_only(path)) as db:
            lead = db.execute("SELECT * FROM organizations WHERE organization_id=?", (lead_id,)).fetchone()
        if lead is None:
            abort(404)
        try:
            models = list_installed_models()
            if form["model"] not in models:
                raise DraftError("Choose an installed Ollama model from the list.")
            model, result = generate_draft(lead, **form)
        except DraftError as exc:
            return detail_page(lead_id, error=str(exc), form=form), 400
        with closing(connect(path)) as db:
            draft_id = save_outreach_draft(db, lead_id, form["objective"], form["tone"], form["length"], model, result)
        return redirect(url_for("detail", lead_id=lead_id, draft=draft_id) + "#outreach", code=303)

    @app.post("/leads/<lead_id>/drafts/<int:draft_id>/edit")
    def save_draft_edit(lead_id, draft_id):
        require_csrf()
        subject = request.form.get("subject", "")
        body = request.form.get("body", "")
        if len(lead_id) > 64 or not subject.strip() or not body.strip() or len(subject) > 200 or len(body) > 4000:
            abort(400)
        with closing(connect(path)) as db:
            if not edit_outreach_draft(db, draft_id, lead_id, subject, body):
                abort(404)
        return redirect(url_for("detail", lead_id=lead_id, draft=draft_id) + "#outreach", code=303)

    return app
