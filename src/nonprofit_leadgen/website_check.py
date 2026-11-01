"""Small, manually invoked checks of source-reported organization websites.

Connect to the validated IP directly while retaining the hostname for TLS and
the HTTP Host header. Never fetch links found in a response body.
"""
import http.client
import ipaddress
import re
import socket
import ssl
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

USER_AGENT = "nonprofit-ai-leadgen/0.2 (local nonprofit website check)"
ROBOT_AGENT = "nonprofit-ai-leadgen"
TIMEOUT = 8
MAX_REDIRECTS = 3
MAX_ROBOTS_BYTES = 512 * 1024
MAX_PAGE_BYTES = 1024
MAX_BATCH = 25


class UnsafeURL(Exception):
    pass


class DNSRejected(Exception):
    pass


class FetchError(Exception):
    pass


@dataclass(frozen=True)
class Response:
    status: int
    location: str | None
    content_type: str | None
    body: bytes
    too_large: bool = False
    content_encoding: str | None = None


@dataclass(frozen=True)
class CheckResult:
    status: str
    final_url: str | None = None
    http_status: int | None = None
    error_type: str | None = None


def validate_url(url: str):
    if not isinstance(url, str) or not url or len(url) > 500 or any(ord(c) < 32 or ord(c) == 127 for c in url) or "\\" in url:
        raise UnsafeURL("invalid URL characters")
    try:
        parts = urlsplit(url)
        hostname = (parts.hostname or "").encode("idna").decode("ascii").lower()
        port = parts.port
    except (ValueError, UnicodeError) as exc:
        raise UnsafeURL("invalid URL") from exc
    if parts.scheme not in ("http", "https") or parts.username or parts.password or parts.fragment:
        raise UnsafeURL("unsupported URL")
    if port is not None and port != (443 if parts.scheme == "https" else 80):
        raise UnsafeURL("nonstandard port")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise UnsafeURL("IP literal")
    if len(hostname) > 253 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", hostname):
        raise UnsafeURL("invalid hostname")
    return parts, hostname


def resolve_public_ip(hostname: str, port: int) -> str:
    try:
        answers = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise FetchError("DNS failed") from exc
    addresses = [ipaddress.ip_address(answer[4][0]) for answer in answers]
    if not addresses or any(not address.is_global for address in addresses):
        raise DNSRejected("non-public DNS address")
    # IPv4 first avoids waiting on broken IPv6 routes; the selected IP is pinned.
    addresses.sort(key=lambda address: address.version != 4)
    return str(addresses[0])


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, hostname, port, address):
        super().__init__(hostname, port=port, timeout=TIMEOUT)
        self._address = address

    def connect(self):
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, hostname, port, address):
        super().__init__(hostname, port=port, timeout=TIMEOUT, context=ssl.create_default_context())
        self._address = address

    def connect(self):
        raw = socket.create_connection((self._address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _request_once(url: str, max_bytes: int) -> Response:
    parts, hostname = validate_url(url)
    port = 443 if parts.scheme == "https" else 80
    address = resolve_public_ip(hostname, port)
    connection = (_PinnedHTTPS if parts.scheme == "https" else _PinnedHTTP)(hostname, port, address)
    target = urlunsplit(("", "", parts.path or "/", parts.query, ""))
    try:
        connection.request("GET", target, headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.5", "Accept-Encoding": "identity", "Connection": "close"})
        response = connection.getresponse()
        body = response.read(max_bytes + 1)
        return Response(response.status, response.getheader("Location"), response.getheader("Content-Type"), body[:max_bytes], len(body) > max_bytes, response.getheader("Content-Encoding"))
    except (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError) as exc:
        raise FetchError(type(exc).__name__) from exc
    finally:
        connection.close()


def _same_site(first: str, second: str) -> bool:
    a = first.removeprefix("www.")
    b = second.removeprefix("www.")
    return a == b


def _follow(url: str, max_bytes: int, origin_host: str):
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        parts, host = validate_url(current)
        if not _same_site(origin_host, host):
            raise UnsafeURL("external redirect")
        response = _request_once(current, max_bytes)
        if response.status not in (301, 302, 303, 307, 308):
            return response, current
        if not response.location:
            raise UnsafeURL("redirect without location")
        current = urljoin(current, response.location)
    raise UnsafeURL("too many redirects")


def _robots_policy(url: str, cache: dict):
    parts, host = validate_url(url)
    origin = (parts.scheme, host)
    if origin in cache:
        return cache[origin]
    robots_url = f"{parts.scheme}://{host}/robots.txt"
    response, _ = _follow(robots_url, MAX_ROBOTS_BYTES, host)
    if response.status in (404, 410):
        cache[origin] = (None, 1.0)
        return cache[origin]
    mime = (response.content_type or "").split(";", 1)[0].lower()
    if response.status != 200 or response.too_large or mime != "text/plain" or (response.content_encoding and response.content_encoding.lower() != "identity"):
        raise FetchError("robots unavailable")
    parser = RobotFileParser()
    parser.parse(response.body.decode("utf-8", errors="replace").splitlines())
    delay = parser.crawl_delay(ROBOT_AGENT) or 1
    rate = parser.request_rate(ROBOT_AGENT)
    if rate and rate.requests:
        delay = max(delay, rate.seconds / rate.requests)
    cache[origin] = (parser, delay)
    return cache[origin]


def _wait_until(origin: tuple[str, str], ready_at: dict, pause):
    remaining = ready_at.get(origin, 0) - time.monotonic()
    if remaining > 0:
        pause(remaining)


def check_site(url: str, pause=time.sleep, robots_cache=None, host_ready=None) -> CheckResult:
    try:
        _, original_host = validate_url(url)
        cache = {} if robots_cache is None else robots_cache
        ready_at = {} if host_ready is None else host_ready
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            parts, host = validate_url(current)
            if not _same_site(original_host, host):
                return CheckResult("external_redirect", current)
            origin = (parts.scheme, host)
            if origin not in cache:
                _wait_until(origin, ready_at, pause)
            parser, delay = _robots_policy(current, cache)
            if origin not in ready_at:
                ready_at[origin] = time.monotonic() + delay
            if parser and not parser.can_fetch(ROBOT_AGENT, current):
                return CheckResult("robots_blocked", current)
            if delay > 10:
                return CheckResult("robots_delay", current)
            _wait_until(origin, ready_at, pause)
            response = _request_once(current, MAX_PAGE_BYTES)
            ready_at[origin] = time.monotonic() + delay
            if response.status in (301, 302, 303, 307, 308):
                if not response.location:
                    return CheckResult("redirect_error", current, response.status)
                current = urljoin(current, response.location)
                continue
            if not 200 <= response.status < 300:
                return CheckResult("http_error", current, response.status)
            mime = (response.content_type or "").split(";", 1)[0].lower()
            if mime not in ("text/html", "application/xhtml+xml"):
                return CheckResult("non_html", current, response.status)
            return CheckResult("reachable", current, response.status)
        return CheckResult("redirect_error", current)
    except UnsafeURL as exc:
        status = "external_redirect" if str(exc) == "external redirect" else "unsafe_url"
        return CheckResult(status, error_type=str(exc))
    except DNSRejected:
        return CheckResult("dns_rejected", error_type="non_public_address")
    except FetchError as exc:
        return CheckResult("network_error", error_type=str(exc))


PENDING_WHERE = """o.website IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM website_checks w WHERE w.organization_id=o.organization_id
    AND w.claimed_url=o.website AND w.checked_at>=?)"""


def pending_count(db, days=30):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return db.execute("SELECT COUNT(*) FROM organizations o WHERE " + PENDING_WHERE, (cutoff,)).fetchone()[0]


def run_batch(db, limit=5, days=30, checker=check_site, pause=time.sleep, on_result=None):
    if not 1 <= limit <= MAX_BATCH:
        raise ValueError(f"limit must be between 1 and {MAX_BATCH}")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = db.execute("SELECT o.organization_id, o.website FROM organizations o WHERE " + PENDING_WHERE +
                      " ORDER BY o.name COLLATE NOCASE, o.organization_id LIMIT ?", (cutoff, limit)).fetchall()
    results = []
    for index, row in enumerate(rows):
        if index:
            pause(1)
        result = checker(row["website"])
        with db:
            db.execute("""INSERT INTO website_checks
                (organization_id, claimed_url, checked_at, status, final_url, http_status, error_type)
                VALUES (?, ?, ?, ?, ?, ?, ?)""", (row["organization_id"], row["website"], datetime.now(timezone.utc).isoformat(), result.status, result.final_url, result.http_status, result.error_type))
        results.append((row["organization_id"], result))
        if on_result:
            on_result(row["organization_id"], result)
    return results


def run_all(db, checker=None, pause=time.sleep, on_progress=None):
    """Check every currently eligible URL once; each result commits before continuing."""
    total = pending_count(db)
    done = 0
    summary = Counter()
    robots_cache = {}
    host_ready = {}
    if checker is None:
        checker = lambda url: check_site(url, robots_cache=robots_cache, host_ready=host_ready)

    def recorded(org_id, result):
        nonlocal done
        done += 1
        summary[result.status] += 1
        if on_progress:
            on_progress(done, total, org_id, result)

    while done < total:
        batch = run_batch(db, limit=min(MAX_BATCH, total - done), checker=checker,
                          pause=pause, on_result=recorded)
        if not batch:
            break
    return done, total, dict(summary)
