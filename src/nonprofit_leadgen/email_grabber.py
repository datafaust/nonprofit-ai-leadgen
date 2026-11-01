"""Manually crawl reachable organization sites for public, general contact inboxes."""
import argparse
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from . import website_check as net
from .storage import connect

MAX_PAGES = 5
MAX_PAGE_BYTES = 256 * 1024
MAX_BATCH = 25
MAX_EMAILS = 30
EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Z0-9._%+-]{1,64}@[A-Z0-9.-]{1,253}\.[A-Z]{2,24}(?![\w-])", re.I)
CONTACT_WORDS = ("contact", "about", "connect", "get-in-touch", "reach-us")
TEXT_BREAK_TAGS = frozenset({
    "address", "article", "br", "dd", "div", "dl", "dt", "footer", "form",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main",
    "ol", "p", "section", "table", "td", "th", "tr", "ul",
})
GENERAL_LOCAL_PARTS = frozenset({
    "info", "contact", "hello", "hi", "office", "admin", "administration",
    "help", "support", "inquiries", "enquiries", "outreach", "programs",
    "services", "team", "general", "mail", "email", "reception", "frontdesk",
    "volunteer", "volunteers", "donate", "donations", "development", "events",
})


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.mailtos = []
        self.text = []
        self.hidden = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in ("script", "style", "template", "noscript") or "hidden" in attributes or attributes.get("aria-hidden") == "true":
            if not self.hidden:
                self.text.append(" ")
            self.hidden.append(tag)
        if tag in TEXT_BREAK_TAGS and not self.hidden:
            self.text.append(" ")
        if tag == "a" and not self.hidden:
            href = attributes.get("href")
            if isinstance(href, str) and 0 < len(href) <= 500:
                (self.mailtos if href.lower().startswith("mailto:") else self.links).append(href)

    def handle_endtag(self, tag):
        if self.hidden and tag == self.hidden[-1]:
            self.hidden.pop()
            if not self.hidden:
                self.text.append(" ")
        elif tag in TEXT_BREAK_TAGS and not self.hidden:
            self.text.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def _general_email(value):
    value = value.strip(" \t\r\n<>.,;:!?()[]\"'").lower()
    if len(value) > 254 or not EMAIL_RE.fullmatch(value):
        return None
    local, _ = value.split("@", 1)
    # Limit collection to public role inboxes; named staff addresses are excluded.
    if local not in GENERAL_LOCAL_PARTS and not any(local.startswith(prefix + ".") or local.startswith(prefix + "-") for prefix in GENERAL_LOCAL_PARTS):
        return None
    return value


def _contact_link(base, href, origin_host):
    try:
        candidate = urljoin(base, href).split("#", 1)[0]
        parts, host = net.validate_url(candidate)
    except (net.UnsafeURL, ValueError):
        return None
    if not net._same_site(origin_host, host) or parts.query:
        return None
    path = parts.path.lower()
    if not any(word in path for word in CONTACT_WORDS):
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))


def _fetch_html(url, origin_host, robots_cache, host_ready, pause):
    current = url
    for _ in range(net.MAX_REDIRECTS + 1):
        parts, host = net.validate_url(current)
        if not net._same_site(origin_host, host):
            raise net.UnsafeURL("external redirect")
        origin = (parts.scheme, host)
        if origin not in robots_cache:
            net._wait_until(origin, host_ready, pause)
        parser, delay = net._robots_policy(current, robots_cache)
        if delay > 10:
            raise net.FetchError("robots delay exceeds 10 seconds")
        if parser and not parser.can_fetch(net.ROBOT_AGENT, current):
            raise net.FetchError("robots blocked")
        if origin not in host_ready:
            host_ready[origin] = time.monotonic() + delay
        net._wait_until(origin, host_ready, pause)
        response = net._request_once(current, MAX_PAGE_BYTES)
        host_ready[origin] = time.monotonic() + delay
        if response.status in (301, 302, 303, 307, 308):
            if not response.location:
                raise net.FetchError("redirect without location")
            try:
                current = urljoin(current, response.location)
            except ValueError as exc:
                raise net.FetchError("invalid redirect") from exc
            continue
        if response.status != 200:
            raise net.FetchError("HTTP error")
        mime = (response.content_type or "").split(";", 1)[0].lower()
        if mime not in ("text/html", "application/xhtml+xml") or response.too_large or (response.content_encoding and response.content_encoding.lower() != "identity"):
            raise net.FetchError("unsupported or oversized page")
        return response.body, current
    raise net.FetchError("too many redirects")


@dataclass(frozen=True)
class ScanResult:
    status: str
    pages_visited: int
    emails: dict[str, str]


def scan_site(url, pause=time.sleep, robots_cache=None, host_ready=None):
    try:
        _, origin_host = net.validate_url(url)
    except net.UnsafeURL:
        return ScanResult("unsafe_url", 0, {})
    cache = {} if robots_cache is None else robots_cache
    ready = {} if host_ready is None else host_ready
    queue, seen, found = [url], set(), {}
    status = "complete"
    while queue and len(seen) < MAX_PAGES:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        try:
            body, final_url = _fetch_html(current, origin_host, cache, ready, pause)
        except (net.UnsafeURL, net.DNSRejected, net.FetchError, ValueError) as exc:
            if len(seen) == 1:
                status = "blocked" if isinstance(exc, net.FetchError) and "robots" in str(exc) else "fetch_error"
            elif status == "complete":
                status = "partial"
            continue
        page = PageParser()
        page.feed(body.decode("utf-8", errors="replace"))
        # Joining text nodes recovers addresses split by inline markup. A second
        # pass with spaces handles adjacent nodes that are separate words.
        candidates = EMAIL_RE.findall("".join(page.text))
        candidates.extend(EMAIL_RE.findall(" ".join(page.text)))
        candidates.extend(unquote(href[7:].split("?", 1)[0]) for href in page.mailtos)
        for candidate in candidates:
            email = _general_email(candidate)
            if email and len(found) < MAX_EMAILS:
                found.setdefault(email, final_url)
        for href in page.links:
            link = _contact_link(final_url, href, origin_host)
            if link and link not in seen and link not in queue and len(queue) < MAX_PAGES * 2:
                queue.append(link)
    return ScanResult(status, len(seen), found)


ELIGIBLE = """o.website IS NOT NULL AND
    (SELECT w.status FROM website_checks w WHERE w.organization_id=o.organization_id
     AND w.claimed_url=o.website ORDER BY w.check_id DESC LIMIT 1)='reachable'"""


def run_batch(db, limit=5, force=False, scanner=None, pause=time.sleep, on_result=None):
    if not 1 <= limit <= MAX_BATCH:
        raise ValueError(f"limit must be between 1 and {MAX_BATCH}")
    where = ELIGIBLE + ("" if force else """ AND NOT EXISTS (
        SELECT 1 FROM website_email_scans s WHERE s.organization_id=o.organization_id
        AND s.claimed_url=o.website)""")
    rows = db.execute("SELECT o.organization_id, o.website FROM organizations o WHERE " + where +
                      " ORDER BY o.name COLLATE NOCASE, o.organization_id LIMIT ?", (limit,)).fetchall()
    cache, ready = {}, {}
    if scanner is None:
        scanner = lambda url: scan_site(url, robots_cache=cache, host_ready=ready)
    results = []
    for index, row in enumerate(rows):
        if index:
            pause(1)
        result = scanner(row["website"])
        now = datetime.now(timezone.utc).isoformat()
        with db:
            if result.status == "complete":
                db.execute("DELETE FROM website_emails WHERE organization_id=? AND claimed_url=?",
                           (row["organization_id"], row["website"]))
            for email, source_url in result.emails.items():
                db.execute("""INSERT INTO website_emails
                    (organization_id, claimed_url, email, source_url, discovered_at, extraction_method, verification_status)
                    VALUES (?, ?, ?, ?, ?, 'html_text_or_mailto', 'unverified')
                    ON CONFLICT(organization_id, claimed_url, email) DO UPDATE SET
                    source_url=excluded.source_url, discovered_at=excluded.discovered_at""",
                    (row["organization_id"], row["website"], email, source_url, now))
            db.execute("""INSERT INTO website_email_scans
                (organization_id, claimed_url, scanned_at, status, pages_visited, emails_found)
                VALUES (?, ?, ?, ?, ?, ?)""", (row["organization_id"], row["website"], now,
                                               result.status, result.pages_visited, len(result.emails)))
        results.append((row["organization_id"], result))
        if on_result:
            on_result(row["organization_id"], result)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description="Extract public general inboxes from reachable organization websites")
    parser.add_argument("--db", default="data/nonprofits.db")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--limit", type=int, default=5, help="Maximum sites this run (1-25; default: 5)")
    scope.add_argument("--all", action="store_true", help="Scan all eligible sites once; can take a long time")
    parser.add_argument("--force", action="store_true", help="Rescan sites already scanned")
    args = parser.parse_args(argv)
    if args.all and args.force:
        parser.error("--all and --force cannot be combined")
    try:
        with connect(args.db) as db:
            total = 0
            while True:
                batch = run_batch(db, limit=MAX_BATCH if args.all else args.limit, force=args.force,
                                  on_result=lambda org, result: print(f"{org[:8]}: {result.status}, {result.pages_visited} pages, {len(result.emails)} general emails", flush=True))
                total += len(batch)
                if not args.all or not batch or len(batch) < MAX_BATCH:
                    break
    except ValueError as exc:
        parser.error(str(exc))
    except KeyboardInterrupt:
        print("Interrupted; completed sites were saved.")
        return 130
    print(f"Scanned {total} reachable sites.")
    return 0
