"""OpenAlex discovery: cursor pagination, retry, reconciliation, ledger, deduplication."""
from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timezone
from typing import Callable, Iterator

import requests

from . import __version__
from .database import fetch_all, upsert
from .taxonomy import query_hash

API = "https://api.openalex.org/works"
SELECT = (
    "id,doi,title,publication_year,type,authorships,primary_location,locations,open_access,"
    "ids,abstract_inverted_index,referenced_works,cited_by_api_url,is_retracted"
)
MAX_QUERY_RESULTS = 100_000  # cursor paging has no 10k cap; split by year only for truly huge queries


class OpenAlexError(RuntimeError):
    pass


class OpenAlexClient:
    def __init__(self, api_key: str | None, session=None, max_retries: int = 5,
                 base_delay: float = 1.0, sleep: Callable[[float], None] = time.sleep):
        self.api_key = api_key
        self.session = session or requests.Session()
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.sleep = sleep
        self.retries = 0

    def get(self, params: dict) -> dict:
        params = dict(params)
        if self.api_key:
            params["api_key"] = self.api_key
        for attempt in range(self.max_retries + 1):
            try:
                r = self.session.get(API, params=params, timeout=60)
                if r.status_code == 200:
                    return r.json()
                if r.status_code in (429, 500, 502, 503, 504):
                    raise OpenAlexError(f"transient HTTP {r.status_code}")
                raise OpenAlexError(f"HTTP {r.status_code}: {r.text[:200]}")  # not retried
            except (requests.ConnectionError, requests.Timeout, OpenAlexError) as e:
                transient = isinstance(e, (requests.ConnectionError, requests.Timeout)) or "transient" in str(e)
                if not transient or attempt == self.max_retries:
                    raise
                self.retries += 1
                self.sleep(min(self.base_delay * 2 ** attempt, 60))
        raise AssertionError("unreachable")


def reconcile(reported: int | None, retrieved: int, failed: int = 0) -> dict:
    """Numerical reconciliation: reported total vs retrieved + failed."""
    if reported is None:
        return {"status": "no_reported_total", "delta": None}
    delta = reported - (retrieved + failed)
    return {"status": "reconciled" if delta == 0 else ("short" if delta > 0 else "over"), "delta": delta}


def year_partitions(first: int, last: int, step: int = 1) -> list[str]:
    """Non-overlapping publication-year filters covering [first, last] exactly."""
    out, y = [], first
    while y <= last:
        hi = min(y + step - 1, last)
        out.append(f"publication_year:{y}" if hi == y else f"publication_year:{y}-{hi}")
        y = hi + 1
    return out


def abstract_text(inv: dict | None) -> str:
    if not inv:
        return ""
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def norm_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi.strip().lower())
    return d or None


def short_id(openalex_url: str) -> str:
    return openalex_url.rsplit("/", 1)[-1]


def work_to_row(w: dict) -> dict:
    ids = w.get("ids") or {}
    locs = []
    for loc in w.get("locations") or []:
        if loc.get("is_oa") and (loc.get("pdf_url") or loc.get("landing_page_url")):
            locs.append({
                "pdf": loc.get("pdf_url"), "landing": loc.get("landing_page_url"),
                "version": loc.get("version"), "source": (loc.get("source") or {}).get("display_name"),
            })
    venue = ((w.get("primary_location") or {}).get("source") or {}).get("display_name")
    return {
        "publication_id": short_id(w["id"]),
        "doi": norm_doi(w.get("doi")),
        "pmid": short_id(ids["pmid"]) if ids.get("pmid") else None,
        "pmcid": short_id(ids["pmcid"]) if ids.get("pmcid") else None,
        "title": w.get("title") or "(untitled)",
        "year": w.get("publication_year"),
        "venue": venue,
        "work_type": w.get("type"),
        "authors": [(a.get("author") or {}).get("display_name") for a in w.get("authorships") or []],
        "is_oa": (w.get("open_access") or {}).get("is_oa"),
        "oa_urls": locs,
        "raw": {
            "abstract": abstract_text(w.get("abstract_inverted_index")),
            "referenced_works": [short_id(x) for x in w.get("referenced_works") or []],
            "cited_by_api_url": w.get("cited_by_api_url"),
            "is_retracted": w.get("is_retracted"),
        },
    }


def store_work(conn, w: dict, species_id: str, run_id: str, rank: int) -> str:
    """Upsert one work; collapse alternate OpenAlex records that share a DOI."""
    row = work_to_row(w)
    pid = row["publication_id"]
    if row["doi"]:
        existing = fetch_all(conn, "SELECT publication_id FROM publications WHERE doi=%s", [row["doi"]])
        if existing and existing[0]["publication_id"] != pid:
            row["duplicate_of"] = existing[0]["publication_id"]
            row["doi"] = None  # keep the alternate record, but DOI stays unique
    upsert(conn, "publications", row, ["publication_id"])
    upsert(conn, "search_results", {"run_id": run_id, "publication_id": pid, "rank": rank},
           ["run_id", "publication_id"], update_cols=["rank"])
    canonical = row.get("duplicate_of") or pid
    upsert(conn, "species_publications", {"species_id": species_id, "publication_id": canonical},
           ["species_id", "publication_id"], update_cols=())
    return pid


def search_params(query: str, filt: str = "") -> dict:
    """Match title+abstract only (the plain `search` parameter also matches full text and returns
    off-topic works that merely mention the words), exclude retracted works, rank by relevance."""
    f = f"title_and_abstract.search:({query.replace(',', ' ')}),is_retracted:false"
    if filt:
        f += "," + filt
    return {"filter": f, "sort": "relevance_score:desc"}


def paginate(client: OpenAlexClient, search: str, filt: str = "", cursor: str = "*",
             per_page: int = 200) -> Iterator[tuple[list[dict], dict, str | None]]:
    """Yield (results, meta, next_cursor) until the cursor is exhausted."""
    while cursor:
        params = {"per-page": per_page, "cursor": cursor, "select": SELECT, **search_params(search, filt)}
        data = client.get(params)
        nxt = (data.get("meta") or {}).get("next_cursor")
        yield data.get("results") or [], data.get("meta") or {}, nxt
        cursor = nxt


def run_query(conn, client: OpenAlexClient, species_id: str, family: str, query: str,
              filt: str = "", max_pages: int | None = None) -> dict:
    """Run (or resume) one query with checkpointing. Returns a ledger summary."""
    qh = query_hash(species_id, query, filt)
    open_runs = fetch_all(
        conn,
        "SELECT * FROM search_runs WHERE species_id=%s AND query_hash=%s AND filters=%s "
        "AND NOT pagination_complete ORDER BY started_at DESC LIMIT 1",
        [species_id, qh, filt],
    )
    if open_runs:
        run = open_runs[0]
        run_id, cursor = run["run_id"], run["cursor_checkpoint"] or "*"
        retrieved, pages, reported = run["retrieved_count"], run["pages_done"], run["reported_total"]
    else:
        now = datetime.now(timezone.utc)
        run_id = f"{qh}:{now.strftime('%Y%m%dT%H%M%S')}"
        cursor, retrieved, pages, reported = "*", 0, 0, None
        upsert(conn, "search_runs", {
            "run_id": run_id, "species_id": species_id, "query_family": family, "query_hash": qh,
            "query_string": query, "filters": filt, "started_at": now, "software_version": __version__,
        }, ["run_id"])
        conn.commit()

    this_call, error, complete = 0, None, False
    try:
        for results, meta, nxt in paginate(client, query, filt, cursor):
            reported = meta.get("count", reported)
            for w in results:
                retrieved += 1
                store_work(conn, w, species_id, run_id, retrieved)
            pages += 1
            this_call += 1
            conn.execute(
                "UPDATE search_runs SET reported_total=%s, retrieved_count=%s, pages_done=%s, "
                "cursor_checkpoint=%s, retry_count=%s WHERE run_id=%s",
                [reported, retrieved, pages, nxt, client.retries, run_id],
            )
            conn.commit()  # checkpoint after every page
            if nxt is None:
                complete = True
                break
            if max_pages is not None and this_call >= max_pages:
                break
    except Exception as e:  # recorded, not swallowed: the run stays incomplete
        conn.rollback()
        error = f"{type(e).__name__}: {e}"
        conn.execute("UPDATE search_runs SET errors=%s, retry_count=%s WHERE run_id=%s",
                     [error, client.retries, run_id])
        conn.commit()
        # Memory may be ahead of the last committed checkpoint; trust the database.
        row = fetch_all(conn, "SELECT retrieved_count, pages_done FROM search_runs WHERE run_id=%s", [run_id])[0]
        retrieved, pages = row["retrieved_count"], row["pages_done"]

    rec = reconcile(reported, retrieved)
    if complete:
        conn.execute("UPDATE search_runs SET pagination_complete=true, finished_at=now() WHERE run_id=%s", [run_id])
        conn.commit()
    return {"run_id": run_id, "family": family, "reported": reported, "retrieved": retrieved,
            "pages_this_call": this_call, "complete": complete, "reconciliation": rec, "error": error}


def plan_filters(reported_total: int | None, first_year: int = 1900, last_year: int | None = None) -> list[str]:
    """If a query is too large, return documented non-overlapping year filters; else [''] (no filter)."""
    if reported_total is None or reported_total <= MAX_QUERY_RESULTS:
        return [""]
    last_year = last_year or datetime.now().year
    # Non-overlapping, gap-free, newest first (recent papers are more often open access).
    blocks = (
        year_partitions(first_year, 1999, 100)
        + year_partitions(2000, 2009, 5)
        + year_partitions(2010, last_year, 1)
    )
    return blocks[::-1]


def count_only(client: OpenAlexClient, query: str, filt: str = "") -> int:
    params = {"per-page": 1, **search_params(query, filt)}
    return int(client.get(params).get("meta", {}).get("count", 0))
