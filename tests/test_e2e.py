"""End-to-end scan with fake OpenAlex / LLM / full text: the validation gate scenarios."""
import pytest

from src import fulltext, pipeline, sheets_sync
from src.database import fetch_all
from src.llm import LLM
from tests.fakes import DOC, FakeLLM, FakeOpenAlex, extraction_payload, make_work

SP = "SP_CURCUMA_LONGA"


@pytest.fixture()
def fake_fulltext(monkeypatch):
    def retrieve(pub):
        return {"publication_id": pub["publication_id"], "status": "retrieved", "source_url": "u",
                "version_source": "publishedVersion", "content_sha256": "x", "n_chars": len(DOC), "error": None, "text": DOC}
    monkeypatch.setattr(fulltext, "retrieve", retrieve)


def n(conn, table, where="true"):
    return fetch_all(conn, f"SELECT count(*) n FROM {table} WHERE {where}")[0]["n"]


def works():
    return [make_work(i) for i in range(4)] + [make_work(9, title="Turmeric marketing survey")]


def test_full_scan_reconciles_and_second_run_adds_nothing(seeded, registry, fake_fulltext):
    llm = FakeLLM(extraction_payload())
    r1 = pipeline.run_scan(seeded, FakeOpenAlex(works()), llm, registry, SP, page_budget=100, extract_limit=10)
    assert r1["unreconciled_queries"] == [] and not r1["errors"]
    assert n(seeded, "publications") == 5
    assert r1["stage1_provisional"] == {"likely_eligible": 4, "likely_ineligible": 1, "uncertain": 0}
    assert r1["stage2_outcomes"] == {"include": 4}
    assert r1["measurements_verified_by_quote_check"] == 12 and r1["measurements_quarantined"] == 4
    assert n(seeded, "measurements") == 12 and n(seeded, "extracts") == 8
    assert n(seeded, "screening", "stage=2 AND decision='include'") == 4
    assert n(seeded, "quality_assessments") == 4 * 15  # every domain rated, unassessed ones 'insufficient information'
    assert r1["coverage_status"] == "Coverage audit pending"  # never auto-complete

    snapshot = {t: n(seeded, t) for t in ("publications", "search_results", "screening", "extracts", "measurements", "quarantine")}
    seeded.execute("UPDATE search_runs SET finished_at = now() - interval '40 days'")  # force a re-run (stale)
    seeded.commit()
    pipeline.run_scan(seeded, FakeOpenAlex(works()), FakeLLM(extraction_payload()), registry, SP, page_budget=100)
    after = {t: n(seeded, t) for t in snapshot}
    assert after["publications"] == snapshot["publications"]
    assert after["measurements"] == snapshot["measurements"] and after["extracts"] == snapshot["extracts"]
    assert after["quarantine"] == snapshot["quarantine"] and after["screening"] == snapshot["screening"]


def test_bounded_batches_resume_to_the_same_result(seeded, registry, fake_fulltext):
    llm = FakeLLM(extraction_payload())
    oa = FakeOpenAlex(works())
    reports = []
    for _ in range(12):  # tiny page budget forces many resumed executions
        reports.append(pipeline.run_scan(seeded, oa, llm, registry, SP, page_budget=2, extract_limit=2))
        if reports[-1]["coverage_status"] == "Coverage audit pending":
            break
    assert reports[-1]["coverage_status"] == "Coverage audit pending"
    assert len(reports) > 2
    assert n(seeded, "publications") == 5 and n(seeded, "search_runs", "NOT pagination_complete") == 0
    bad = fetch_all(seeded, "SELECT 1 FROM search_runs WHERE reported_total <> retrieved_count")
    assert bad == []
    assert n(seeded, "measurements") == 12


def test_llm_budget_stops_cleanly_and_keeps_checkpoint(seeded, registry, fake_fulltext):
    llm = FakeLLM(extraction_payload(), max_calls=3)
    r = pipeline.run_scan(seeded, FakeOpenAlex(works()), llm, registry, SP, page_budget=100)
    assert "stopped_early" in r and r["errors"]
    assert n(seeded, "search_runs", "pagination_complete") == 7  # search work was already committed
    # a later run with budget finishes the job
    r2 = pipeline.run_scan(seeded, FakeOpenAlex(works()), FakeLLM(extraction_payload()), registry, SP, page_budget=100)
    assert not r2["errors"] and n(seeded, "publications") == 5


def test_unavailable_full_text_is_logged_not_dropped(seeded, registry, monkeypatch):
    monkeypatch.setattr(fulltext, "retrieve", lambda pub: {
        "publication_id": pub["publication_id"], "status": "unavailable", "source_url": None, "version_source": None,
        "content_sha256": None, "n_chars": None, "error": "no open-access location", "text": None})
    r = pipeline.run_scan(seeded, FakeOpenAlex(works()), FakeLLM(extraction_payload()), registry, SP, page_budget=100)
    assert r["stage2_outcomes"] == {"unavailable": 4}
    assert n(seeded, "screening", "stage=2 AND decision='requires_review' AND fulltext_available=false") == 4
    assert n(seeded, "source_documents", "status='unavailable'") == 4


def test_coverage_audit_flags_open_items(seeded, registry, fake_fulltext):
    pipeline.run_scan(seeded, FakeOpenAlex(works()), FakeLLM(extraction_payload()), registry, SP, page_budget=100)
    sp = next(s for s in registry if s.species_id == SP)
    audit = pipeline.coverage_audit(seeded, sp, "config/sentinels.json")
    assert audit["checks"]["all_planned_queries_run"] and audit["checks"]["reconciled"]
    assert audit["checks"]["supplementary_data_examined"] is None  # human sign-off still required
    assert audit["status"] == "Coverage audit pending"


# ---- Sheets: one-way sync + verdicts ----------------------------------------------------------
class FakeWS:
    def __init__(self, title, records=None):
        self.title, self.records, self.written, self.row_count = title, records or [], [], 1000

    def clear(self): self.written = []
    def resize(self, rows): self.row_count = rows
    def update(self, values, range_name): self.written.append((range_name, values))
    def get_all_records(self): return self.records


class FakeWB:
    def __init__(self, verdicts=None):
        self.tabs = {"Studies": FakeWS("Studies"), "Review_Verdicts": FakeWS("Review_Verdicts", verdicts)}

    def worksheet(self, t):
        if t not in self.tabs:
            raise KeyError(t)
        return self.tabs[t]

    def add_worksheet(self, title, rows, cols):
        self.tabs[title] = FakeWS(title)
        return self.tabs[title]


def test_sync_out_touches_only_generated_tabs(seeded, registry, fake_fulltext):
    pipeline.run_scan(seeded, FakeOpenAlex(works()), FakeLLM(extraction_payload()), registry, SP, page_budget=100)
    wb = FakeWB()
    sizes = sheets_sync.sync_out(seeded, wb)
    assert sizes["DB_Measurements"] == 12
    assert wb.tabs["Studies"].written == []  # legacy tab untouched
    assert wb.tabs["Review_Verdicts"].written == []
    assert all(t.startswith("DB_") or t in ("Studies", "Review_Verdicts") for t in wb.tabs)


def test_human_verdicts_flow_back_without_rewriting_values(seeded, registry, fake_fulltext):
    pipeline.run_scan(seeded, FakeOpenAlex(works()), FakeLLM(extraction_payload()), registry, SP, page_budget=100)
    ids = [r["measurement_id"] for r in fetch_all(seeded, "SELECT measurement_id FROM measurements ORDER BY 1 LIMIT 2")]
    before = fetch_all(seeded, "SELECT measurement_id, value_original FROM measurements ORDER BY 1")
    wb = FakeWB([{"measurement_id": ids[0], "verdict": "reject", "reviewer": "pando", "note": "wrong column"},
                 {"measurement_id": ids[1], "verdict": "approve", "reviewer": "pando", "note": ""},
                 {"measurement_id": ids[1], "verdict": "approve", "reviewer": "", "note": "no reviewer: ignored"}])
    assert sheets_sync.sync_verdicts_in(seeded, wb) == {"read": 3, "applied": 2}
    st = {r["measurement_id"]: r["verification_status"] for r in fetch_all(seeded, "SELECT measurement_id, verification_status FROM measurements")}
    assert st[ids[0]] == "N" and st[ids[1]] == "Y"
    assert fetch_all(seeded, "SELECT measurement_id, value_original FROM measurements ORDER BY 1") == before
