from unittest.mock import patch

import pytest

from nonprofit_leadgen.email_grabber import ScanResult, main, run_batch, scan_site
from nonprofit_leadgen.storage import connect
from nonprofit_leadgen.website_check import Response


def _site(db, org_id, url, status="reachable"):
    db.execute("""INSERT INTO organizations
        (organization_id, name, website, date_ingested, contact_verification_status, lead_qualification_status)
        VALUES (?, ?, ?, '2026-01-01', 'none', 'unreviewed')""", (org_id, org_id, url))
    db.execute("""INSERT INTO website_checks (organization_id, claimed_url, checked_at, status)
        VALUES (?, ?, '2026-01-01', ?)""", (org_id, url, status))
    db.commit()


def test_scan_follows_only_contact_links_and_ignores_hostile_content():
    calls = []
    def fetch(url, max_bytes):
        calls.append(url)
        if url.endswith("robots.txt"):
            return Response(200, None, "text/plain", b"User-agent: *\nAllow: /\n")
        if url.endswith("/contact"):
            return Response(200, None, "text/html", b'<a href="mailto:hello@example.org?subject=Hi">Email</a> info@example.org <script>admin@evil.org</script> jane@example.org')
        return Response(200, None, "text/html", b'<a href>Empty</a><a>No href</a><a href="">Blank</a><a href="/contact#staff">Contact</a><a href="https://evil.org/contact">Bad</a><!-- ignore: support@evil.org -->')
    with patch("nonprofit_leadgen.website_check._request_once", side_effect=fetch):
        result = scan_site("https://example.org/", pause=lambda _: None)
    assert result.status == "complete"
    assert result.emails == {"hello@example.org": "https://example.org/contact", "info@example.org": "https://example.org/contact"}
    assert calls == ["https://example.org/robots.txt", "https://example.org/", "https://example.org/contact"]


def test_visible_email_split_across_inline_markup_without_crossing_blocks():
    def fetch(url, max_bytes):
        if url.endswith("robots.txt"):
            return Response(404, None, "text/plain", b"")
        return Response(200, None, "text/html", b"""
            <p><span>info</span><span>@example.org</span></p>
            <p>contact</p><p>@wrong.org</p>
            <p>Write to hello@example.org.</p>
            <script>support@hidden.org</script>
        """)
    with patch("nonprofit_leadgen.website_check._request_once", side_effect=fetch):
        result = scan_site("https://example.org/", pause=lambda _: None)
    assert result.emails == {
        "info@example.org": "https://example.org/",
        "hello@example.org": "https://example.org/",
    }


def test_robots_and_redirect_block_fetch():
    calls = []
    def fetch(url, max_bytes):
        calls.append(url)
        if url.endswith("robots.txt"):
            return Response(200, None, "text/plain", b"User-agent: *\nDisallow: /\n")
        raise AssertionError("page must not be fetched")
    with patch("nonprofit_leadgen.website_check._request_once", side_effect=fetch):
        assert scan_site("https://example.org/", pause=lambda _: None).status == "blocked"
    assert calls == ["https://example.org/robots.txt"]

    def redirect(url, max_bytes):
        if url.endswith("robots.txt"):
            return Response(404, None, "text/plain", b"")
        return Response(302, "http://169.254.169.254/latest/meta-data/", "text/html", b"")
    with patch("nonprofit_leadgen.website_check._request_once", side_effect=redirect):
        assert scan_site("https://example.org/", pause=lambda _: None).status == "fetch_error"


def test_oversized_page_is_not_parsed():
    def fetch(url, max_bytes):
        if url.endswith("robots.txt"):
            return Response(404, None, "text/plain", b"")
        return Response(200, None, "text/html", b"info@example.org", too_large=True)
    with patch("nonprofit_leadgen.website_check._request_once", side_effect=fetch):
        result = scan_site("https://example.org/", pause=lambda _: None)
    assert result.status == "fetch_error" and not result.emails


def test_database_join_and_resume(tmp_path):
    db = connect(tmp_path / "test.db")
    _site(db, "lead-1", "https://example.org")
    _site(db, "lead-2", "https://other.org", "http_error")
    scanner = lambda _: ScanResult("complete", 2, {"info@example.org": "https://example.org/contact", "hello@example.org": "https://example.org/contact"})
    assert len(run_batch(db, scanner=scanner, pause=lambda _: None)) == 1
    assert run_batch(db, scanner=scanner, pause=lambda _: None) == []
    rows = db.execute("""SELECT o.organization_id, e.email, e.source_url
        FROM organizations o JOIN website_emails e ON e.organization_id=o.organization_id
        AND e.claimed_url=o.website ORDER BY e.email""").fetchall()
    assert [tuple(row) for row in rows] == [
        ("lead-1", "hello@example.org", "https://example.org/contact"),
        ("lead-1", "info@example.org", "https://example.org/contact"),
    ]
    assert len(run_batch(db, force=True, scanner=scanner, pause=lambda _: None)) == 1
    assert db.execute("SELECT COUNT(*) FROM website_emails").fetchone()[0] == 2
    db.close()


def test_latest_check_and_current_claim_control_eligibility(tmp_path):
    db = connect(tmp_path / "test.db")
    _site(db, "lead-1", "https://example.org")
    db.execute("""INSERT INTO website_checks (organization_id, claimed_url, checked_at, status)
        VALUES ('lead-1', 'https://example.org', '2026-01-02', 'http_error')""")
    db.commit()
    assert run_batch(db, scanner=lambda _: pytest.fail("must not scan")) == []
    db.execute("UPDATE organizations SET website='https://new.example.org' WHERE organization_id='lead-1'")
    db.commit()
    assert run_batch(db, scanner=lambda _: pytest.fail("must not scan")) == []
    db.close()


def test_cli_rejects_unbounded_force(tmp_path):
    with pytest.raises(SystemExit):
        main(["--db", str(tmp_path / "test.db"), "--all", "--force"])
