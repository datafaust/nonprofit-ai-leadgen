import pytest

from nonprofit_leadgen.storage import connect
from nonprofit_leadgen.web import _connect_read_only, create_app


@pytest.fixture
def app(tmp_path):
    path = tmp_path / "test.db"
    db = connect(path)
    for n in range(55):
        db.execute(
            """INSERT INTO organizations
               (organization_id, name, description, borough, website, phone, date_ingested,
                contact_verification_status, lead_qualification_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (f"lead-{n}", "<script>alert(1)</script> Aid" if n == 0 else f"Example Aid {n:02}",
             "<img src=x onerror=alert(1)>" if n == 0 else "Community help", "Queens" if n < 30 else "Bronx",
             "https://example.org" if n == 0 else None, "2125550100" if n == 0 else None,
             "2026-01-01", "source_reported" if n == 0 else "none", "unreviewed"),
        )
    db.execute(
        "INSERT INTO source_records(source, source_record_id, organization_id, source_url, extracted_at) VALUES (?, ?, ?, ?, ?)",
        ("synthetic", "row-1", "lead-0", "https://example.org/<script>", "2026-01-01"),
    )
    db.commit()
    db.close()
    web_app = create_app(path)
    web_app.config["TEST_DB_PATH"] = path
    return web_app


def test_list_pagination_search_and_borough(app):
    client = app.test_client()
    first = client.get("/")
    assert first.status_code == 200
    assert first.data.count(b'class="lead-link"') == 50
    assert b"Page 1 of 2" in first.data
    assert b"<script>" not in first.data
    assert b"&lt;script&gt;" in first.data
    second = client.get("/?page=2")
    assert second.data.count(b'class="lead-link"') == 5
    queens = client.get("/?borough=Queens")
    assert b"30</strong>" in queens.data
    assert queens.data.count(b'class="lead-link"') == 30
    search = client.get("/?q=Example%20Aid%2054")
    assert search.data.count(b'class="lead-link"') == 1
    assert client.get("/?q=%25").data.count(b'class="lead-link"') == 0


def test_detail_escaped_read_only_and_missing(app, tmp_path):
    client = app.test_client()
    response = client.get("/leads/lead-0")
    assert response.status_code == 200
    assert b"&lt;img src=x onerror=alert(1)&gt;" in response.data
    assert b"<script>" not in response.data
    assert b"Generate email" in response.data and b"disabled" in response.data
    assert b"source-reported" in response.data.lower()
    assert client.get("/leads/missing").status_code == 404
    assert client.get("/leads/' OR 1=1 --").status_code == 404
    assert client.post("/leads/lead-0").status_code == 405
    assert response.headers["Cache-Control"] == "no-store"
    assert "script-src 'none'" in response.headers["Content-Security-Policy"]
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400


def test_invalid_query_and_missing_database(app, tmp_path):
    client = app.test_client()
    assert client.get("/?borough=Atlantis").status_code == 400
    assert client.get("/?q=" + "a" * 101).status_code == 400
    assert client.get("/?page=0").status_code == 400
    assert client.get("/?page=3").status_code == 404
    assert client.get("/?email_status=unknown").status_code == 400
    with pytest.raises(FileNotFoundError):
        create_app(tmp_path / "missing.db")


def test_viewer_database_connection_is_read_only(app):
    import sqlite3

    db = _connect_read_only(app.config["TEST_DB_PATH"])
    with pytest.raises(sqlite3.OperationalError):
        db.execute("DELETE FROM organizations")
    db.close()


def test_viewer_shows_website_check_without_treating_it_as_identity(app):
    import sqlite3

    with sqlite3.connect(app.config["TEST_DB_PATH"]) as db:
        db.execute("""INSERT INTO website_checks
                      (organization_id, claimed_url, checked_at, status, final_url, http_status)
                      VALUES (?, ?, ?, ?, ?, ?)""",
                   ("lead-0", "https://example.org", "2026-01-02", "reachable", "https://example.org/", 200))
    client = app.test_client()
    assert b"Reachable" in client.get("/").data
    detail = client.get("/leads/lead-0").data
    assert b"Reachability does not verify organization identity" in detail
    assert b"https://example.org/" in detail


def test_reachable_filter_uses_latest_check_for_current_url(app):
    import sqlite3

    with sqlite3.connect(app.config["TEST_DB_PATH"]) as db:
        db.execute("UPDATE organizations SET website='https://old.example.org' WHERE organization_id='lead-1'")
        db.execute("UPDATE organizations SET website='https://new.example.org' WHERE organization_id='lead-2'")
        db.execute("UPDATE organizations SET website='https://bronx.example.org' WHERE organization_id='lead-30'")
        db.executemany("""INSERT INTO website_checks
                         (organization_id, claimed_url, checked_at, status, http_status)
                         VALUES (?, ?, ?, ?, ?)""", [
            ("lead-0", "https://example.org", "2026-01-01", "reachable", 200),
            ("lead-1", "https://old.example.org", "2026-01-01", "reachable", 200),
            ("lead-1", "https://old.example.org", "2026-01-02", "network_error", None),
            ("lead-2", "https://old.example.org", "2026-01-01", "reachable", 200),
            ("lead-30", "https://bronx.example.org", "2026-01-01", "reachable", 200),
        ])
    client = app.test_client()
    all_leads = client.get("/")
    assert b"55</strong>" in all_leads.data
    reached = client.get("/?website_status=reachable")
    assert reached.status_code == 200
    assert b"2</strong>" in reached.data
    assert reached.data.count(b'class="lead-link"') == 2
    assert b"lead-1" not in reached.data and b"lead-2" not in reached.data
    assert b"does not confirm organization identity" in reached.data
    queens = client.get("/?borough=Queens&website_status=reachable")
    assert queens.data.count(b'class="lead-link"') == 1
    search = client.get("/?q=Example+Aid+30&website_status=reachable")
    assert search.data.count(b'class="lead-link"') == 1
    assert client.get("/?website_status=verified").status_code == 400


def test_reachable_filter_pagination_retains_selection(app):
    import sqlite3

    with sqlite3.connect(app.config["TEST_DB_PATH"]) as db:
        for n in range(55):
            db.execute("UPDATE organizations SET website=? WHERE organization_id=?", (f"https://example-{n}.org", f"lead-{n}"))
            db.execute("""INSERT INTO website_checks
                          (organization_id, claimed_url, checked_at, status, http_status)
                          VALUES (?, ?, '2026-01-01', 'reachable', 200)""",
                       (f"lead-{n}", f"https://example-{n}.org"))
    client = app.test_client()
    first = client.get("/?website_status=reachable")
    assert first.data.count(b'class="lead-link"') == 50
    assert b"website_status=reachable" in first.data
    second = client.get("/?website_status=reachable&page=2")
    assert second.status_code == 200
    assert second.data.count(b'class="lead-link"') == 5


def test_website_email_flag_and_filter_use_current_claim(app):
    import sqlite3

    with sqlite3.connect(app.config["TEST_DB_PATH"]) as db:
        db.execute("UPDATE organizations SET website='https://other.org' WHERE organization_id='lead-1'")
        db.execute("UPDATE organizations SET website='https://new.org' WHERE organization_id='lead-2'")
        db.executemany("""INSERT INTO website_emails
            (organization_id, claimed_url, email, source_url, discovered_at, extraction_method)
            VALUES (?, ?, ?, ?, '2026-01-01', 'html_text_or_mailto')""", [
            ("lead-0", "https://example.org", "info@example.org", "https://example.org/contact"),
            ("lead-0", "https://example.org", "hello@example.org", "https://example.org/contact"),
            ("lead-1", "https://other.org", "contact@other.org", "https://other.org/contact"),
            ("lead-2", "https://old.org", "info@old.org", "https://old.org/contact"),
        ])
        db.execute("""INSERT INTO website_checks (organization_id, claimed_url, checked_at, status)
            VALUES ('lead-1', 'https://other.org', '2026-01-01', 'reachable')""")
    client = app.test_client()
    all_leads = client.get("/")
    assert b"Website email" in all_leads.data
    assert all_leads.data.count(b">Found</span>") == 2
    with_email = client.get("/?email_status=with_email")
    assert b"2</strong>" in with_email.data
    assert with_email.data.count(b'class="lead-link"') == 2
    assert b"Example Aid 02" not in with_email.data
    assert b'option value="with_email" selected' in with_email.data
    without_email = client.get("/?email_status=without_email")
    assert b"53</strong>" in without_email.data
    assert b"Example Aid 02" in without_email.data
    combined = client.get("/?email_status=with_email&website_status=reachable&borough=Queens")
    assert b"1</strong>" in combined.data
    assert b"Example Aid 01" in combined.data


def test_website_email_filter_pagination_retains_selection(app):
    import sqlite3

    with sqlite3.connect(app.config["TEST_DB_PATH"]) as db:
        for n in range(55):
            url = f"https://example-{n}.org"
            db.execute("UPDATE organizations SET website=? WHERE organization_id=?", (url, f"lead-{n}"))
            db.execute("""INSERT INTO website_emails
                (organization_id, claimed_url, email, source_url, discovered_at, extraction_method)
                VALUES (?, ?, ?, ?, '2026-01-01', 'html_text_or_mailto')""",
                (f"lead-{n}", url, f"info@example-{n}.org", url))
    client = app.test_client()
    first = client.get("/?email_status=with_email")
    assert first.data.count(b'class="lead-link"') == 50
    assert b"email_status=with_email" in first.data
    second = client.get("/?email_status=with_email&page=2")
    assert second.status_code == 200
    assert second.data.count(b'class="lead-link"') == 5
