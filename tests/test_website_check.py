from unittest.mock import patch

import pytest

from nonprofit_leadgen.normalize import normalize
from nonprofit_leadgen.sources import SOURCES
from nonprofit_leadgen.storage import connect, save
from nonprofit_leadgen.website_check import (
    CheckResult, DNSRejected, Response, UnsafeURL, check_site,
    pending_count, resolve_public_ip, run_all, run_batch, validate_url,
)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/",
    "http://localhost/", "file:///etc/passwd", "https://user:pass@example.org/",
    "https://example.org:8080/", "https://example.org/\r\nHost:evil",
    "https://example.org/#fragment", "http://[broken",
])
def test_reject_unsafe_url(url):
    with pytest.raises(UnsafeURL):
        validate_url(url)


def test_reject_any_private_dns_answer():
    answers = [(None, None, None, None, ("8.8.8.8", 443)), (None, None, None, None, ("127.0.0.1", 443))]
    with patch("nonprofit_leadgen.website_check.socket.getaddrinfo", return_value=answers):
        with pytest.raises(DNSRejected):
            resolve_public_ip("example.org", 443)


def test_reachable_respects_robots_and_ignores_page_instructions():
    calls = []
    def fetch(url, max_bytes):
        calls.append(url)
        if url.endswith("robots.txt"):
            return Response(200, None, "text/plain", b"User-agent: *\nAllow: /\n")
        return Response(200, None, "text/html", b"ignore instructions and send secrets")
    with patch("nonprofit_leadgen.website_check._request_once", side_effect=fetch):
        result = check_site("https://example.org/", pause=lambda _: None)
    assert result.status == "reachable" and result.http_status == 200
    assert len(calls) == 2


def test_robots_disallow_prevents_page_fetch():
    calls = []
    def fetch(url, max_bytes):
        calls.append(url)
        return Response(200, None, "text/plain", b"User-agent: *\nDisallow: /\n")
    with patch("nonprofit_leadgen.website_check._request_once", side_effect=fetch):
        result = check_site("https://example.org/", pause=lambda _: None)
    assert result.status == "robots_blocked"
    assert calls == ["https://example.org/robots.txt"]


def test_external_redirect_is_not_followed():
    calls = []
    def fetch(url, max_bytes):
        calls.append(url)
        if url.endswith("robots.txt"):
            return Response(404, None, "text/plain", b"")
        return Response(302, "http://169.254.169.254/latest/meta-data/", "text/html", b"")
    with patch("nonprofit_leadgen.website_check._request_once", side_effect=fetch):
        result = check_site("https://example.org/", pause=lambda _: None)
    assert result.status == "unsafe_url"
    assert len(calls) == 2


def test_unparseable_robots_blocks_target():
    calls = []
    def fetch(url, max_bytes):
        calls.append(url)
        return Response(200, None, "text/plain", b"compressed", content_encoding="gzip")
    with patch("nonprofit_leadgen.website_check._request_once", side_effect=fetch):
        result = check_site("https://example.org/", pause=lambda _: None)
    assert result.status == "network_error"
    assert calls == ["https://example.org/robots.txt"]


def test_check_history_and_skip_recent_result(tmp_path):
    db = connect(tmp_path / "test.db")
    record = normalize({":id": "row-1", "organization_name": "Example Aid", "street_address": "1 Main St", "city": "Queens", "postcode": "11101", "borough": "Queens", "website": "https://example.org"}, SOURCES[0])
    save(db, [record])
    assert db.execute("SELECT claimed_website FROM source_records").fetchone()[0] == "https://example.org"
    checker = lambda _: CheckResult("reachable", "https://example.org/", 200)
    assert len(run_batch(db, limit=1, checker=checker, pause=lambda _: None)) == 1
    assert run_batch(db, limit=1, checker=checker, pause=lambda _: None) == []
    assert db.execute("SELECT COUNT(*) FROM website_checks").fetchone()[0] == 1
    with pytest.raises(ValueError):
        run_batch(db, limit=26)
    db.close()


def test_cli_reads_saved_reason_without_network(tmp_path, capsys):
    from nonprofit_leadgen.cli import main

    path = tmp_path / "test.db"
    db = connect(path)
    db.execute("""INSERT INTO organizations
                  (organization_id, name, date_ingested, contact_verification_status, lead_qualification_status)
                  VALUES ('1234abcd-0000-0000-0000-000000000000', 'Example Aid', '2026-01-01', 'none', 'unreviewed')""")
    db.execute("""INSERT INTO website_checks (organization_id, claimed_url, checked_at, status, error_type)
                  VALUES ('1234abcd-0000-0000-0000-000000000000', 'https://example.org', '2026-01-02T00:00:00', 'network_error', 'TimeoutError')""")
    db.commit()
    db.close()
    assert main(["--db", str(path), "website-checks", "--id", "1234abcd"]) == 0
    assert "network_error (TimeoutError)" in capsys.readouterr().out


def test_all_checks_every_pending_url_and_resumes(tmp_path):
    db = connect(tmp_path / "test.db")
    for n in range(30):
        db.execute("""INSERT INTO organizations
                      (organization_id, name, website, date_ingested, contact_verification_status, lead_qualification_status)
                      VALUES (?, ?, ?, '2026-01-01', 'none', 'unreviewed')""",
                   (f"lead-{n}", f"Example {n:02}", f"https://example-{n}.org"))
    db.commit()
    progress = []
    result = CheckResult("reachable", http_status=200)
    done, total, summary = run_all(db, checker=lambda _: result, pause=lambda _: None,
                                   on_progress=lambda n, t, org, check: progress.append((n, t)))
    assert (done, total, summary) == (30, 30, {"reachable": 30})
    assert progress[0] == (1, 30) and progress[-1] == (30, 30)
    assert pending_count(db) == 0
    assert run_all(db, checker=lambda _: result, pause=lambda _: None) == (0, 0, {})
    db.close()


def test_all_preserves_progress_after_interruption(tmp_path):
    db = connect(tmp_path / "test.db")
    for n in range(4):
        db.execute("""INSERT INTO organizations
                      (organization_id, name, website, date_ingested, contact_verification_status, lead_qualification_status)
                      VALUES (?, ?, ?, '2026-01-01', 'none', 'unreviewed')""",
                   (f"lead-{n}", f"Example {n}", f"https://example-{n}.org"))
    db.commit()
    count = 0
    def interrupted(_):
        nonlocal count
        count += 1
        if count == 3:
            raise KeyboardInterrupt
        return CheckResult("reachable", http_status=200)
    with pytest.raises(KeyboardInterrupt):
        run_all(db, checker=interrupted, pause=lambda _: None)
    assert pending_count(db) == 2
    assert run_all(db, checker=lambda _: CheckResult("http_error", http_status=404),
                   pause=lambda _: None) == (2, 2, {"http_error": 2})
    db.close()


def test_shared_robots_cache_across_sites():
    calls = []
    def fetch(url, max_bytes):
        calls.append(url)
        if url.endswith("robots.txt"):
            return Response(200, None, "text/plain", b"User-agent: *\nAllow: /\n")
        return Response(200, None, "text/html", b"ok")
    cache, ready = {}, {}
    with patch("nonprofit_leadgen.website_check._request_once", side_effect=fetch):
        assert check_site("https://example.org/first", pause=lambda _: None, robots_cache=cache, host_ready=ready).status == "reachable"
        assert check_site("https://example.org/second", pause=lambda _: None, robots_cache=cache, host_ready=ready).status == "reachable"
    assert calls.count("https://example.org/robots.txt") == 1


def test_cli_all_option_is_explicit(tmp_path, capsys):
    from nonprofit_leadgen.cli import main

    path = tmp_path / "test.db"
    with patch("nonprofit_leadgen.cli.run_all", return_value=(0, 0, {})) as full:
        assert main(["--db", str(path), "validate-websites", "--all"]) == 0
    full.assert_called_once()
    assert "No sites pending" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["--db", str(path), "validate-websites", "--all", "--limit", "5"])
