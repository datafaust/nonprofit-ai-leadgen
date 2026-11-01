"""Dataset download, parse, validate, and normalize."""
import io
import logging

import pandas as pd

from .network import download_csv
from .normalize import normalize
from .sources import SOURCES
from .storage import save

LOG = logging.getLogger(__name__)

class SourceError(Exception):
    pass

def parse_source(data: bytes, source):
    try:
        frame = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False, on_bad_lines="error", encoding="utf-8-sig")
    except (pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeError, ValueError) as exc:
        raise SourceError("invalid CSV") from exc
    if len(frame) >= 50_000:
        raise SourceError("source reached API row limit; pagination required")
    if not set(source.required).issubset(frame.columns):
        raise SourceError("required source columns missing")
    if frame.columns.duplicated().any():
        raise SourceError("duplicate CSV columns")
    records = []
    skipped = 0
    for row in frame.to_dict(orient="records"):
        record = normalize(row, source)
        if record is None:
            skipped += 1
        else:
            records.append(record)
    return records, skipped

def ingest_all(db):
    results = []
    for index, source in enumerate(SOURCES):
        try:
            data = download_csv(source.csv_url, pause=1 if index else 0)
            records, skipped = parse_source(data, source)
            count = save(db, records)
            results.append((source.key, count, skipped, None))
        except Exception as exc:
            # Never log raw rows, response bodies, or potentially sensitive URLs.
            LOG.warning("Source %s unavailable: %s", source.key, type(exc).__name__)
            results.append((source.key, 0, 0, type(exc).__name__))
    return results
