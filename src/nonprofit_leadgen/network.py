"""Bounded downloads of the two configured Socrata CSVs only."""
import ipaddress
import socket
import time
from urllib.parse import urlsplit

import requests
from .sources import SOURCES

MAX_BYTES = 30 * 1024 * 1024
HOST = "data.cityofnewyork.us"

class DownloadError(Exception):
    pass

def validate_public_host(host: str) -> None:
    if host != HOST:
        raise DownloadError("unexpected download host")
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise DownloadError("DNS lookup failed") from exc
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise DownloadError("download host resolved to a non-public address")

def download_csv(url: str, session: requests.Session | None = None, pause: float = 0) -> bytes:
    parts = urlsplit(url)
    if url not in {source.csv_url for source in SOURCES} or parts.scheme != "https" or parts.hostname != HOST:
        raise DownloadError("download URL is not an approved HTTPS endpoint")
    validate_public_host(parts.hostname)
    if pause:
        time.sleep(pause)
    client = session or requests.Session()
    try:
        for attempt in range(2):
            try:
                with client.get(url, timeout=(5, 30), stream=True, allow_redirects=False,
                                headers={"User-Agent": "nonprofit-ai-leadgen/0.1 (local research; public NYC data)"}) as response:
                    if response.is_redirect:
                        raise DownloadError("redirects are disabled")
                    if response.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                        time.sleep(1)
                        continue
                    response.raise_for_status()
                    mime = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                    if mime not in ("text/csv", "application/csv", "application/octet-stream"):
                        raise DownloadError("unexpected content type")
                    size = response.headers.get("Content-Length")
                    if size:
                        try:
                            declared_size = int(size)
                        except ValueError as exc:
                            raise DownloadError("invalid content length") from exc
                        if declared_size < 0 or declared_size > MAX_BYTES:
                            raise DownloadError("download exceeds size limit")
                    chunks, total = [], 0
                    for chunk in response.iter_content(chunk_size=65536):
                        total += len(chunk)
                        if total > MAX_BYTES:
                            raise DownloadError("download exceeds size limit")
                        chunks.append(chunk)
                    if not total:
                        raise DownloadError("empty download")
                    return b"".join(chunks)
            except requests.RequestException as exc:
                if attempt == 0:
                    time.sleep(1)
                    continue
                raise DownloadError("download failed") from exc
    finally:
        if session is None:
            client.close()
    raise DownloadError("download failed")
