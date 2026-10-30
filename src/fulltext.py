"""Lawful open-access full-text retrieval. Only URLs OpenAlex flags as open access are used."""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path

import requests

CACHE = Path(os.environ.get("FULLTEXT_CACHE", ".cache/fulltext"))
MAX_BYTES = 30 * 1024 * 1024
HEADERS = {"User-Agent": "global-plant-research-scanner (mailto:" + os.environ.get("CONTACT_EMAIL", "unset") + ")"}

# Prefer the authoritative published version, then accepted manuscripts, then preprints.
VERSION_RANK = {"publishedVersion": 0, "acceptedVersion": 1, "submittedVersion": 2, None: 3}


def candidate_urls(oa_urls: list[dict]) -> list[dict]:
    out = []
    for loc in oa_urls or []:
        for kind in ("pdf", "landing"):
            if loc.get(kind):
                out.append({"url": loc[kind], "kind": kind, "version": loc.get("version"), "source": loc.get("source")})
    out.sort(key=lambda c: (VERSION_RANK.get(c["version"], 3), 0 if c["kind"] == "pdf" else 1))
    return out


def pdf_to_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    parts = []
    for i, page in enumerate(reader.pages, start=1):
        parts.append(f"\n[[page {i}]]\n{page.extract_text() or ''}")
    return "".join(parts)


def html_to_text(data: bytes) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(data, "html.parser")
    for t in soup(["script", "style", "nav", "header", "footer"]):
        t.decompose()
    return soup.get_text("\n")


def fetch_text(url: str, session=None) -> tuple[str | None, str | None]:
    """Return (text, error)."""
    session = session or requests.Session()
    try:
        r = session.get(url, headers=HEADERS, timeout=60, stream=True)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        data = r.raw.read(MAX_BYTES + 1, decode_content=True) if hasattr(r, "raw") and r.raw else r.content
        if len(data) > MAX_BYTES:
            return None, "file too large"
        ctype = r.headers.get("content-type", "").lower()
        is_pdf = "pdf" in ctype or data[:5] == b"%PDF-"
        text = pdf_to_text(data) if is_pdf else html_to_text(data)
        if len(text.strip()) < 2000:
            return None, "text too short (likely paywall or scanned image)"
        return text, None
    except Exception as e:  # network/parse failures are logged, never fatal
        return None, f"{type(e).__name__}: {e}"


def retrieve(publication: dict, session=None) -> dict:
    """Try candidates in preference order. Returns a source_documents-shaped dict plus 'text'."""
    last_err = "no open-access location"
    for cand in candidate_urls(publication.get("oa_urls") or []):
        text, err = fetch_text(cand["url"], session)
        if text:
            sha = hashlib.sha256(text.encode()).hexdigest()
            CACHE.mkdir(parents=True, exist_ok=True)
            (CACHE / f"{publication['publication_id']}.txt").write_text(text, encoding="utf-8")
            return {"publication_id": publication["publication_id"], "status": "retrieved",
                    "source_url": cand["url"], "version_source": cand["version"] or cand["source"],
                    "content_sha256": sha, "n_chars": len(text), "error": None, "text": text}
        last_err = f"{cand['url']}: {err}"
    status = "unavailable" if last_err == "no open-access location" else "failed"
    return {"publication_id": publication["publication_id"], "status": status, "source_url": None,
            "version_source": None, "content_sha256": None, "n_chars": None, "error": last_err, "text": None}
