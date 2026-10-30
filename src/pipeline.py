"""One bounded, resumable execution of the scanner."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from . import __version__, extraction, fulltext, openalex_search as oas, quality, screening
from .database import fetch_all, upsert
from .llm import BudgetExceeded, LLM
from .taxonomy import Species, build_queries, load_registry

RERUN_AFTER_DAYS = 30
NOT_COMPLETE = "Complete within documented search scope"


def load_species(conn, registry: list[Species]) -> None:
    for sp in registry:
        upsert(conn, "species", {"species_id": sp.species_id, "accepted_name": sp.accepted_name,
                                 "authority": sp.authority, "priority": sp.priority},
               ["species_id"], update_cols=["accepted_name", "authority", "priority"])
        for n in sp.synonyms:
            upsert(conn, "synonyms", {"species_id": sp.species_id, "name": n, "kind": "synonym"},
                   ["species_id", "name"], update_cols=())
        for n in sp.common_names:
            upsert(conn, "synonyms", {"species_id": sp.species_id, "name": n, "kind": "common_name"},
                   ["species_id", "name"], update_cols=())
    conn.commit()


def pick_species(conn, registry: list[Species], wanted: str | None = None) -> Species:
    """Highest-priority unfinished species. Unfinished work is never abandoned for a new day."""
    if wanted:
        return next(s for s in registry if s.species_id == wanted)
    rows = {r["species_id"]: r["coverage_status"] for r in fetch_all(conn, "SELECT species_id, coverage_status FROM species")}
    for sp in registry:
        if rows.get(sp.species_id, "Not started") != NOT_COMPLETE:
            return sp
    return registry[0]


def _query_done_recently(conn, species_id: str, qh: str, filt: str) -> bool:
    rows = fetch_all(
        conn,
        "SELECT 1 FROM search_runs WHERE species_id=%s AND query_hash=%s AND filters=%s AND pagination_complete "
        f"AND finished_at > now() - interval '{RERUN_AFTER_DAYS} days' LIMIT 1",
        [species_id, qh, filt],
    )
    return bool(rows)


def run_searches(conn, oa: oas.OpenAlexClient, sp: Species, page_budget: int) -> tuple[list[dict], int]:
    ledger, remaining = [], page_budget
    for q in build_queries(sp):
        if remaining <= 0:
            break
        # Resolve the filter plan first so oversized queries are split into documented year blocks.
        filters = [""]
        if not _query_done_recently(conn, sp.species_id, q["hash"], ""):
            filters = oas.plan_filters(oas.count_only(oa, q["query"]))
        for filt in filters:
            if remaining <= 0:
                break
            qh = oas.query_hash(sp.species_id, q["query"], filt)
            if _query_done_recently(conn, sp.species_id, qh, filt):
                continue
            res = oas.run_query(conn, oa, sp.species_id, q["family"], q["query"], filt, max_pages=remaining)
            remaining -= res["pages_this_call"]
            ledger.append(res)
    return ledger, page_budget - remaining


def pending_extraction(conn, species_id: str, limit: int) -> list[dict]:
    return fetch_all(
        conn,
        "SELECT p.publication_id, p.title, p.oa_urls, p.pmcid FROM species_publications sp "
        "JOIN publications p USING (publication_id) "
        "JOIN screening s ON s.publication_id=p.publication_id AND s.species_id=sp.species_id AND s.stage=1 "
        "  AND s.decision IN ('likely_eligible','uncertain') "
        "LEFT JOIN source_documents d ON d.publication_id=p.publication_id "
        "WHERE sp.species_id=%s AND p.duplicate_of IS NULL AND (d.publication_id IS NULL "
        "  OR (d.status='failed' AND d.retrieved_at < now() - interval '7 days') "
        "  OR (d.status='unavailable' AND d.retrieved_at < now() - interval '30 days')) "
        "ORDER BY (s.decision='likely_eligible') DESC, p.is_oa DESC NULLS LAST, "
        "(SELECT min(r.rank) FROM search_results r WHERE r.publication_id=p.publication_id), p.publication_id LIMIT %s",
        [species_id, limit],
    )


def process_paper(conn, llm: LLM, sp: Species, pub: dict, do_quality: bool, model: str) -> dict:
    doc = fulltext.retrieve(pub)
    text = doc.pop("text")
    upsert(conn, "source_documents", doc, ["publication_id"])
    reviewer = model
    if not text:
        upsert(conn, "screening", {
            "publication_id": pub["publication_id"], "species_id": sp.species_id, "stage": 2,
            "decision": "requires_review", "reason": f"full text {doc['status']}: {doc['error']}",
            "reviewer": "pipeline", "fulltext_available": False,
        }, ["publication_id", "species_id", "stage", "reviewer"])
        conn.commit()
        return {"outcome": doc["status"]}

    result = extraction.run_model(llm, sp.accepted_name, pub["title"], text, model)
    if not result["extracts"] and result["parse_failures"]:
        decision, reason = "requires_review", "model output unparseable"
    elif not result["extracts"]:
        decision = "exclude" if not result["eligible"] else "requires_review"
        reason = result["reason"] or "no extractable original measurements"
    else:
        decision, reason = "include", result["reason"] or "original composition data present"
    upsert(conn, "screening", {
        "publication_id": pub["publication_id"], "species_id": sp.species_id, "stage": 2,
        "decision": decision, "reason": reason[:500], "reviewer": reviewer, "fulltext_available": True,
    }, ["publication_id", "species_id", "stage", "reviewer"])
    out = {"outcome": decision}
    if decision == "include":
        study_id = "S_" + pub["publication_id"]
        upsert(conn, "studies", {"study_id": study_id, "publication_id": pub["publication_id"],
                                 "species_id": sp.species_id, "claimed_title": pub["title"],
                                 "verification_status": "U", "origin": "pipeline"},
               ["study_id"], update_cols=["publication_id", "species_id"])
        counts = extraction.store_extraction(conn, study_id, sp.species_id, result, text, model)
        out.update(counts)
        if counts["verified"]:
            conn.execute("UPDATE studies SET verification_status='Y', verification_note=%s WHERE study_id=%s",
                         ["DOI/OpenAlex record exists and >=1 value passed the quote check", study_id])
        if do_quality:
            for a in quality.assess(llm, text, model):
                upsert(conn, "quality_assessments", {"study_id": study_id, **a}, ["study_id", "domain"])
    conn.commit()
    return out


def update_status(conn, sp: Species) -> str:
    def one(sql, *args):
        return fetch_all(conn, sql, list(args))[0]["n"]

    sid = sp.species_id
    runs = one("SELECT count(*) n FROM search_runs WHERE species_id=%s", sid)
    open_runs = one("SELECT count(*) n FROM search_runs WHERE species_id=%s AND NOT pagination_complete", sid)
    if runs == 0:
        status = "Discovery in progress"
    elif open_runs:
        status = "Retrieval incomplete"
    elif screening.pending_stage1(conn, sid, 1):
        status = "Screening in progress"
    elif pending_extraction(conn, sid, 1):
        status = "Full-text extraction in progress"
    else:
        status = "Coverage audit pending"  # 'Complete' is set only by an explicit, passing audit
    conn.execute("UPDATE species SET coverage_status=%s WHERE species_id=%s AND coverage_status<>%s",
                 [status, sid, NOT_COMPLETE])
    conn.commit()
    return status


def run_scan(conn, oa, llm: LLM, registry: list[Species], species_id: str | None = None, *,
             page_budget: int = 25, screen_limit: int = 200, extract_limit: int = 10,
             do_quality: bool = True, sheets_wb=None) -> dict:
    started = datetime.now(timezone.utc)
    sp = pick_species(conn, registry, species_id)
    report: dict = {"species_processed": sp.accepted_name, "software_version": __version__, "errors": []}
    ids_before = fetch_all(conn, "SELECT count(*) n FROM publications")[0]["n"]
    meas_before = fetch_all(conn, "SELECT count(*) n FROM measurements")[0]["n"]

    ledger, pages = run_searches(conn, oa, sp, page_budget)
    report.update(
        queries_executed=len(ledger), pages_fetched=pages,
        new_search_hits_retrieved=sum(r["retrieved"] for r in ledger),
        unreconciled_queries=[r["run_id"] for r in ledger if r["complete"] and r["reconciliation"]["status"] != "reconciled"],
    )
    report["errors"] += [r["error"] for r in ledger if r["error"]]
    report["unique_new_publications"] = fetch_all(conn, "SELECT count(*) n FROM publications")[0]["n"] - ids_before
    report["duplicates_identified"] = fetch_all(
        conn, "SELECT count(*) n FROM publications WHERE duplicate_of IS NOT NULL")[0]["n"]

    try:
        report["stage1_provisional"] = screening.run_stage1(conn, llm, sp.species_id, sp.accepted_name, screen_limit)
        outcomes: dict[str, int] = {}
        totals = {"verified": 0, "review": 0, "quarantined": 0, "extracts": 0}
        for pub in pending_extraction(conn, sp.species_id, extract_limit):
            res = process_paper(conn, llm, sp, pub, do_quality, extraction.EXTRACT_MODEL)
            outcomes[res["outcome"]] = outcomes.get(res["outcome"], 0) + 1
            for k in totals:
                totals[k] += res.get(k, 0)
        report["stage2_outcomes"] = outcomes
        report["extracts_created"] = totals["extracts"]
        report["measurements_verified_by_quote_check"] = totals["verified"]
        report["measurements_needing_review"] = totals["review"]
        report["measurements_quarantined"] = totals["quarantined"]
        report["new_measurements_total"] = fetch_all(conn, "SELECT count(*) n FROM measurements")[0]["n"] - meas_before
    except BudgetExceeded as e:
        conn.rollback()
        report["errors"].append(str(e))
        report["stopped_early"] = "LLM call budget reached; checkpoint saved"

    report["llm_calls"] = llm.calls
    report["coverage_status"] = update_status(conn, sp)
    report["remaining_work"] = {
        "stage1_pending": len(screening.pending_stage1(conn, sp.species_id, 100000)),
        "fulltext_pending": len(pending_extraction(conn, sp.species_id, 100000)),
        "open_search_runs": fetch_all(conn, "SELECT count(*) n FROM search_runs WHERE species_id=%s AND NOT pagination_complete", [sp.species_id])[0]["n"],
    }
    report["next_planned_operation"] = (
        "resume search checkpoints" if report["remaining_work"]["open_search_runs"]
        else "screen remaining records" if report["remaining_work"]["stage1_pending"]
        else "retrieve and extract remaining full texts" if report["remaining_work"]["fulltext_pending"]
        else "run coverage audit"
    )

    if sheets_wb is not None:
        from . import sheets_sync
        try:
            report["verdicts_applied"] = sheets_sync.sync_verdicts_in(conn, sheets_wb)
            report["sheet_rows"] = sheets_sync.sync_out(conn, sheets_wb)
        except Exception as e:
            report["errors"].append(f"sheets sync failed: {type(e).__name__}: {e}")

    conn.execute("INSERT INTO execution_log(started_at, finished_at, report, software_version, errors) "
                 "VALUES (%s, now(), %s, %s, %s)",
                 [started, json.dumps(report), __version__, "; ".join(map(str, report["errors"])) or None])
    conn.commit()
    return report


def coverage_audit(conn, sp: Species, sentinel_path: str = "config/sentinels.json") -> dict:
    """Evaluate the audit questions we can answer from recorded data; leave human ones as None."""
    sid = sp.species_id
    one = lambda sql, *a: fetch_all(conn, sql, list(a))[0]["n"]  # noqa: E731
    planned = {q["hash"] for q in build_queries(sp)}
    done = {r["query_hash"] for r in fetch_all(
        conn, "SELECT DISTINCT query_hash FROM search_runs WHERE species_id=%s AND pagination_complete", [sid])}
    sentinels = []
    if os.path.exists(sentinel_path):
        sentinels = json.load(open(sentinel_path)).get(sid, [])
    found = {r["publication_id"] for r in fetch_all(
        conn, "SELECT publication_id FROM species_publications WHERE species_id=%s", [sid])}
    recall = (sum(s in found for s in sentinels) / len(sentinels)) if sentinels else None
    checks = {
        "all_planned_queries_run": planned <= done,
        "all_result_pages_processed": one("SELECT count(*) n FROM search_runs WHERE species_id=%s AND NOT pagination_complete", sid) == 0,
        "reconciled": one("SELECT count(*) n FROM search_runs WHERE species_id=%s AND pagination_complete "
                          "AND reported_total IS NOT NULL AND reported_total <> retrieved_count + failed_count", sid) == 0,
        "deduplicated": True,  # enforced by unique keys on publications.doi / publication_id
        "all_records_screened": not screening.pending_stage1(conn, sid, 1),
        "fulltext_failures_documented": one(
            "SELECT count(*) n FROM source_documents d JOIN species_publications sp USING (publication_id) "
            "WHERE sp.species_id=%s AND d.status<>'retrieved' AND d.error IS NULL", sid) == 0,
        "references_and_citing_checked": None,       # needs citation expansion run; not implemented as automatic
        "supplementary_data_examined": None,         # human
        "sentinels_recovered": (recall == 1.0) if recall is not None else None,
        "filters_documented": True,
        "language_and_coverage_limits_documented": None,  # human
    }
    status = "Coverage audit pending"
    notes = "Human sign-off required for null checks before marking complete."
    conn.execute(
        "INSERT INTO coverage_audits(species_id,status,checks,sentinel_recall,notes) VALUES (%s,%s,%s,%s,%s)",
        [sid, status, json.dumps(checks), recall, notes])
    conn.commit()
    return {"checks": checks, "sentinel_recall": recall, "status": status}
