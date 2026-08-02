"""Persist verbatim fetched bytes to disk + a raw_document row. Raw-first, idempotent."""
from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from dsr.models import RawDocument

RAW_STORAGE_ROOT = Path(__file__).resolve().parent.parent.parent.parent / "data" / "raw"


def _storage_path(source: str, sha256: str) -> Path:
    return RAW_STORAGE_ROOT / source / sha256[:2] / f"{sha256}.html"


def save_raw_document(
    session: Session,
    *,
    source: str,
    url: str,
    content: bytes,
    http_status: int | None,
    fetched_at: dt.datetime | None = None,
) -> RawDocument:
    """Write `content` to disk keyed by its hash and upsert the raw_document row.

    Idempotent: re-fetching identical bytes for the same (source, url) returns the
    existing row rather than inserting a duplicate, satisfying the UNIQUE constraint
    on (source, url, content_sha256).
    """
    fetched_at = fetched_at or dt.datetime.now(dt.timezone.utc)
    sha256 = hashlib.sha256(content).hexdigest()

    existing = session.scalar(
        select(RawDocument).where(
            RawDocument.source == source,
            RawDocument.url == url,
            RawDocument.content_sha256 == sha256,
        )
    )
    if existing is not None:
        return existing

    path = _storage_path(source, sha256)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(content)

    doc = RawDocument(
        source=source,
        url=url,
        fetched_at=fetched_at,
        http_status=http_status,
        content_sha256=sha256,
        storage_path=str(path.relative_to(RAW_STORAGE_ROOT.parent.parent)),
    )
    session.add(doc)
    session.flush()
    return doc


def load_raw_bytes(doc: RawDocument) -> bytes:
    project_root = RAW_STORAGE_ROOT.parent.parent
    return (project_root / doc.storage_path).read_bytes()
