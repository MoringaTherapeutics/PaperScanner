"""Database-backed tests: schema, idempotency, checkpoint recovery, quarantine, end-to-end scan."""
import json
from pathlib import Path

import pytest

from src import extraction as ex, fulltext, openalex_search as oas, pipeline, screening, sheets_sync, workbook_import
from src.database import fetch_all
from src.llm import BudgetExceeded
from tests.fakes import DOC, FakeLLM, FakeOpenAlex, extraction_payload, make_work

ROOT = Path(__file__).resolve().parent.parent
needs_workbook = pytest.mark.skipif(
    not (ROOT / "data/workbook_export.txt").exists() or not list((ROOT / "config").glob("workbook_audit_*.json")),
    reason="private workbook files are not in the repo",
)
SP = "SP_CURCUMA_LONGA"


def n(conn, table, where="true"):
    return fetch_all(conn, f"SELECT count(*) n FROM {table} WHERE {where}")[0]["n"]


# ---- search ledger: reconciliation, resume, idempotency ---------------------------------------
def test_search_reconciles_and_is_idempotent(seeded):
    works = [make_work(i) for i in range(5)]
    for _ in range(2):  # run twice: second run must not duplicate anything
        res = oas.run_query(seeded, FakeOpenAlex(works), SP, "chemical", "q")
        assert res["complete"] and res["reconciliation"]["status"] == "reconciled"
    assert n(seeded, "publications") == 5
    assert n(seeded, "species_publications") == 5
    runs = fetch_all(seeded, "SELECT reported_total, retrieved_count, pagination_complete FROM search_runs")
    assert all(r["reported_total"] == r["retrieved_count"] == 5 and r["pagination_complete"] for r in runs)


def test_search_resumes_from_checkpoint_after_page_budget(seeded):
    works = [make_work(i) for i in range(7)]
    first = oas.run_query(seeded, FakeOpenAlex(works), SP, "chemical", "q", max_pages=2)
    assert not first["complete"] and first["retrieved"] == 4
    ck = fetch_all(seeded, "SELECT cursor_checkpoint, pages_done FROM search_runs")[0]
    assert ck["pages_done"] == 2 and ck["cursor_checkpoint"] == "p2"
    second = oas.run_query(seeded, FakeOpenAlex(works), SP, "chemical", "q")
    assert second["complete"] and second["retrieved"] == 7 and second["run_id"] == first["run_id"]
    assert n(seeded, "publications") == 7 and n(seeded, "search_runs") == 1


def test_search_recovers_after_crash_mid_run(seeded):
    works = [make_work(i) for i in range(6)]
    crashed = oas.run_query(seeded, FakeOpenAlex(works, crash_on_call=2), SP, "chemical", "q")
    assert crashed["error"] and not crashed["complete"]
    assert crashed["retrieved"] == 2  # only the committed page counts
    again = oas.run_query(seeded, FakeOpenAlex(works), SP, "chemical", "q")
    assert again["complete"] and again["reconciliation"]["status"] == "reconciled"
    assert n(seeded, "publications") == 6
    assert "ConnectionError" in fetch_all(seeded, "SELECT errors FROM search_runs")[0]["errors"]  # error stays on the ledger


def test_duplicate_doi_collapses_to_one_canonical_record(seeded):
    a, b = make_work(1, doi="10.1/same"), make_work(2, doi="10.1/same")
    oas.run_query(seeded, FakeOpenAlex([a, b]), SP, "chemical", "q")
    assert n(seeded, "publications") == 2
    assert n(seeded, "publications", "duplicate_of IS NOT NULL") == 1
    assert n(seeded, "species_publications") == 1  # only the canonical record is screened


# ---- extraction persistence --------------------------------------------------------------------
def prep_study(conn):
    oas.run_query(conn, FakeOpenAlex([make_work(1)]), SP, "chemical", "q")
    pipeline.upsert(conn, "studies", {"study_id": "S1", "publication_id": "W1001", "species_id": SP,
                                      "verification_status": "U", "origin": "pipeline"}, ["study_id"])
    conn.commit()


def test_solvent_specific_extracts_and_quarantine(seeded):
    prep_study(seeded)
    counts = ex.store_extraction(seeded, "S1", SP, extraction_payload(), DOC, "m")
    assert counts == {"extracts": 2, "verified": 3, "review": 0, "quarantined": 1}
    solvents = {r["solvent"] for r in fetch_all(seeded, "SELECT pr.solvent FROM extracts e JOIN extraction_protocols pr USING (protocol_id)")}
    assert solvents == {"water", "ethanol"}  # never merged into one row
    q = fetch_all(seeded, "SELECT reasons FROM quarantine")[0]["reasons"]
    assert any("not found verbatim" in r for r in q)
    assert n(seeded, "measurements", "verification_status='Y' AND verified_by='quote-check'") == 3
    flags = fetch_all(seeded, "SELECT flag_polyphenols, flag_bioactives, flag_tannins FROM extracts WHERE flag_bioactives='Y'")
    assert flags == [{"flag_polyphenols": "Y", "flag_bioactives": "Y", "flag_tannins": "U"}]  # no Y without a trusted value


def test_store_extraction_is_idempotent(seeded):
    prep_study(seeded)
    for _ in range(3):
        ex.store_extraction(seeded, "S1", SP, extraction_payload(), DOC, "m")
    assert n(seeded, "extracts") == 2 and n(seeded, "measurements") == 3 and n(seeded, "quarantine") == 1


def test_original_value_is_never_overwritten(seeded):
    prep_study(seeded)
    ex.store_extraction(seeded, "S1", SP, extraction_payload(), DOC, "m")
    seeded.execute("UPDATE measurements SET verification_status='N', verified_by='human'")
    seeded.commit()
    ex.store_extraction(seeded, "S1", SP, extraction_payload(), DOC, "m2")  # re-run with a different model label
    assert n(seeded, "measurements", "verification_status='N' AND verified_by='human'") == 3  # human verdict survives


# ---- workbook import ---------------------------------------------------------------------------
@needs_workbook
def test_workbook_import_to_db_is_idempotent_and_conservative(seeded):
    p = workbook_import.build_plan((ROOT / "data/workbook_export.txt").read_text(),
                                   json.load(open(ROOT / "config/workbook_audit_2026-10-30.json")))
    for _ in range(2):
        workbook_import.write_to_db(p, seeded)
    assert n(seeded, "studies") == 31 and n(seeded, "studies", "verification_status<>'U'") == 0
    assert n(seeded, "legacy_values") == 29
    assert n(seeded, "quarantine", "kind='legacy_value'") == 48
    assert n(seeded, "quarantine", "kind='legacy_robustness'") == 29
    assert n(seeded, "measurements") == 0  # nothing legacy reaches the verified dataset
    gi2 = fetch_all(seeded, "SELECT claimed_doi FROM studies WHERE study_id='GI002'")[0]["claimed_doi"]
    assert gi2 == "10.1016/j.jpba.2016.03.009"  # original claim preserved
    corr = fetch_all(seeded, "SELECT new_value FROM corrections WHERE row_id='GI002' AND field='doi'")
    assert corr == [{"new_value": "10.1016/j.jchromb.2016.01.009"}]
    assert n(seeded, "corrections") == 8  # BS003 x3, GI002 x2, GI004, GI005, GI006


def test_screening_takes_best_ranked_records_first(seeded):
    # IDs deliberately run opposite to relevance rank: W1004 is rank 1.
    works = [make_work(i) for i in (4, 3, 2, 1, 0)]
    oas.run_query(seeded, FakeOpenAlex(works), SP, "chemical", "q")
    first = screening.pending_stage1(seeded, SP, 2)
    assert [r["publication_id"] for r in first] == ["W1004", "W1003"]
