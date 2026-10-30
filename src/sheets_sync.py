"""One-way database -> Google Sheets sync.

Only tabs whose names start with 'DB_' are ever written or cleared. The legacy tabs
(Studies, Composition_Values, Robustness_Screen, ...) are never touched. Human verdicts are
entered in 'Review_Verdicts', which is only ever read (and created with a header if absent).
"""
from __future__ import annotations

import json
import os

from .database import fetch_all, upsert

GENERATED_PREFIX = "DB_"
VERDICT_TAB = "Review_Verdicts"
VERDICT_HEADER = ["measurement_id", "verdict", "reviewer", "note"]
CHUNK_ROWS = 5000


class RefusedWrite(RuntimeError):
    pass


def assert_generated(tab: str) -> None:
    if not tab.startswith(GENERATED_PREFIX):
        raise RefusedWrite(f"refusing to write to non-generated tab {tab!r}")


QUERIES = {
    "DB_Measurements": (
        "SELECT m.measurement_id, st.study_id, p.doi, 'https://doi.org/' || p.doi AS doi_link, "
        "sd.source_url AS fulltext_link, sd.version_source, p.year, p.title AS publication_title, a.name AS analyte, "
        "e.plant_part, e.preparation, pr.solvent, pr.solvent_concentration, pr.technique, pr.temperature, "
        "pr.duration, m.value_original, m.unit_original, m.basis, m.uncertainty_type, m.uncertainty_value, "
        "m.n, m.method, m.locator, m.source_quote, m.verification_status "
        "FROM measurements m JOIN extracts e USING (extract_id) JOIN studies st USING (study_id) "
        "LEFT JOIN publications p ON p.publication_id=st.publication_id "
        "LEFT JOIN source_documents sd ON sd.publication_id=st.publication_id JOIN analytes a USING (analyte_id) "
        "LEFT JOIN extraction_protocols pr ON pr.protocol_id=e.protocol_id "
        "ORDER BY st.study_id, e.extract_id, a.name"
    ),
    "DB_Extracts": (
        "SELECT e.extract_id, e.study_id, e.plant_part, e.cultivar, e.sample_state, e.preparation, "
        "pr.solvent, pr.solvent_concentration, pr.technique, e.flag_macronutrients, e.flag_minerals, "
        "e.flag_polyphenols, e.flag_tannins, e.flag_bioactives, e.flag_antioxidant_assays "
        "FROM extracts e LEFT JOIN extraction_protocols pr USING (protocol_id) ORDER BY e.study_id"
    ),
    "DB_Search_Audit": (
        "SELECT species_id, query_family, query_string, filters, started_at, reported_total, retrieved_count, "
        "failed_count, pagination_complete, retry_count, errors FROM search_runs ORDER BY started_at DESC"
    ),
    "DB_Quarantine": (
        "SELECT kind, ref_id, array_to_string(reasons, '; ') AS reasons, created_at FROM quarantine "
        "ORDER BY created_at DESC"
    ),
}


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    return str(v)


def to_grid(rows: list[dict]) -> list[list[str]]:
    if not rows:
        return [["(no rows)"]]
    header = list(rows[0])
    return [header] + [[_cell(r[c]) for c in header] for r in rows]


def open_workbook(sa_json: str | None = None, sheet_id: str | None = None):
    import gspread

    info = json.loads(sa_json or os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    return gspread.service_account_from_dict(info).open_by_key(sheet_id or os.environ["SPREADSHEET_ID"])


def write_tab(wb, tab: str, grid: list[list[str]]) -> None:
    assert_generated(tab)
    try:
        ws = wb.worksheet(tab)
    except Exception:
        ws = wb.add_worksheet(title=tab, rows=max(len(grid) + 10, 100), cols=max(len(grid[0]), 5))
    ws.clear()
    if ws.row_count < len(grid):
        ws.resize(rows=len(grid) + 10)
    for i in range(0, len(grid), CHUNK_ROWS):
        ws.update(values=grid[i:i + CHUNK_ROWS], range_name=f"A{i + 1}")


def sync_out(conn, wb) -> dict:
    sizes = {}
    for tab, sql in QUERIES.items():
        grid = to_grid(fetch_all(conn, sql))
        write_tab(wb, tab, grid)
        sizes[tab] = len(grid) - 1
    try:
        wb.worksheet(VERDICT_TAB)
    except Exception:
        ws = wb.add_worksheet(title=VERDICT_TAB, rows=1000, cols=len(VERDICT_HEADER))
        ws.update(values=[VERDICT_HEADER], range_name="A1")
    return sizes


def sync_verdicts_in(conn, wb) -> dict:
    """Read human verdicts; approve -> Y, reject -> N. Never rewrites value_original."""
    try:
        rows = wb.worksheet(VERDICT_TAB).get_all_records()
    except Exception:
        return {"read": 0, "applied": 0}
    applied = 0
    for r in rows:
        mid, verdict, reviewer = r.get("measurement_id"), (r.get("verdict") or "").lower(), r.get("reviewer")
        if not mid or verdict not in ("approve", "reject", "needs_work") or not reviewer:
            continue
        upsert(conn, "validation_reviews", {"measurement_id": mid, "reviewer": reviewer,
                                            "verdict": verdict, "note": r.get("note") or None},
               ["measurement_id", "reviewer"])
        if verdict in ("approve", "reject"):
            conn.execute(
                "UPDATE measurements SET verification_status=%s, verified_by=%s, verified_at=now() "
                "WHERE measurement_id=%s",
                ["Y" if verdict == "approve" else "N", reviewer, mid],
            )
            applied += 1
    conn.commit()
    return {"read": len(rows), "applied": applied}
