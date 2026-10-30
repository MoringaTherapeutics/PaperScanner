"""Pure-logic tests (no database)."""
import json
from pathlib import Path

import pytest
import requests
import yaml

from src import extraction as ex, openalex_search as oas, quality, sheets_sync, taxonomy, workbook_import
from tests.fakes import FakeOpenAlex, make_work

ROOT = Path(__file__).resolve().parent.parent
needs_workbook = pytest.mark.skipif(
    not (ROOT / "data/workbook_export.txt").exists() or not list((ROOT / "config").glob("workbook_audit_*.json")),
    reason="private workbook files are not in the repo",
)


# ---- taxonomy ----------------------------------------------------------------------------------
def test_query_matrix_covers_names_and_families(registry):
    sp = next(s for s in registry if s.species_id == "SP_CURCUMA_LONGA")
    plan = taxonomy.build_queries(sp)
    families = {q["family"] for q in plan}
    assert families == {"chemical", "methodological", "assay", "taxonomic"}
    assert plan[-1]["family"] == "taxonomic"
    assert all('"Curcuma longa"' in q["query"] and '"turmeric"' in q["query"] for q in plan)
    assert any('"Curcuma domestica"' in q["query"] for q in plan)
    assert not any("(disputed)" in q["query"] for q in plan)
    assert len({q["hash"] for q in plan}) == len(plan)
    assert plan == taxonomy.build_queries(sp)  # deterministic


# ---- OpenAlex helpers --------------------------------------------------------------------------
def test_reconcile():
    assert oas.reconcile(10, 10)["status"] == "reconciled"
    assert oas.reconcile(10, 7, 2) == {"status": "short", "delta": 1}
    assert oas.reconcile(None, 5)["status"] == "no_reported_total"


def test_year_partitions_cover_range_without_overlap():
    for f in oas.plan_filters(10**6, first_year=1900, last_year=2026):
        assert f.startswith("publication_year:")
    years = []
    for f in oas.plan_filters(10**6, 1900, 2026):
        span = f.split(":")[1]
        lo, _, hi = span.partition("-")
        years += list(range(int(lo), int(hi or lo) + 1))
    assert sorted(years) == list(range(1900, 2027)) and len(years) == len(set(years))
    assert oas.plan_filters(10**6, 1900, 2026)[0] == "publication_year:2026"  # newest first
    assert oas.plan_filters(100) == [""]


def test_abstract_and_doi_normalization():
    assert oas.abstract_text({"b": [1], "a": [0]}) == "a b"
    assert oas.norm_doi("https://doi.org/10.1/ABC") == "10.1/abc"
    assert oas.norm_doi(None) is None


def test_client_retries_transient_then_succeeds():
    class S:
        n = 0

        def get(self, *a, **k):
            S.n += 1
            r = requests.Response()
            r.status_code = 429 if S.n < 3 else 200
            r._content = b'{"ok": true}'
            return r

    waits = []
    c = oas.OpenAlexClient("k", session=S(), sleep=waits.append)
    assert c.get({"search": "x"}) == {"ok": True}
    assert c.retries == 2 and waits == [1.0, 2.0]  # bounded exponential backoff


def test_client_does_not_retry_client_errors():
    class S:
        def get(self, *a, **k):
            r = requests.Response()
            r.status_code = 400
            r._content = b"bad"
            return r

    with pytest.raises(oas.OpenAlexError):
        oas.OpenAlexClient("k", session=S(), sleep=lambda s: None).get({})


def test_paginate_walks_every_page():
    fake = FakeOpenAlex([make_work(i) for i in range(5)], page_size=2)
    got = [w for res, _, _ in oas.paginate(fake, "q") for w in res]
    assert len(got) == 5


# ---- extraction verification -------------------------------------------------------------------
DOC = ex.normalize("Table 2. Water extract 12.4 ± 0.8 mg GAE/g dry weight. Hyphen-\nated word. Curcumin 54.3")


def m(**kw):
    base = {"value_original": "12.4 ± 0.8", "unit": "mg GAE/g dry weight", "basis": "dry", "method": "FC",
            "locator": "Table 2", "source_quote": "Water extract 12.4 ± 0.8 mg GAE/g dry weight"}
    base.update(kw)
    return base


def test_judge_verified_when_everything_checks_out():
    assert ex.judge_measurement(m(), DOC) == ("verified", [])


def test_judge_quarantines_quote_not_in_document():
    v, why = ex.judge_measurement(m(source_quote="Water extract 99 mg"), DOC)
    assert v == "quarantine" and any("not found verbatim" in r for r in why)


def test_judge_quarantines_value_missing_from_quote():
    v, why = ex.judge_measurement(m(value_original="13.9"), DOC)
    assert v == "quarantine" and any("value not present" in r for r in why)


def test_judge_quarantines_implausible_and_missing_locator():
    assert ex.judge_measurement(m(value_original="54.3", unit="g/100 g", source_quote="Curcumin 54.3", locator=""), DOC)[0] == "quarantine"
    v, _ = ex.judge_measurement(m(value_original="540", unit="g/100 g", source_quote="Table 2. Water extract 12.4"), DOC)
    assert v == "quarantine"


def test_judge_sends_unknown_basis_to_review_not_verified():
    v, why = ex.judge_measurement(m(basis="unknown"), DOC)
    assert v == "review" and "basis not stated" in why


def test_normalization_handles_unicode_and_hyphenated_linebreaks():
    assert "hyphenated word" in DOC
    assert ex.normalize("12.4 ± 0.8") == ex.normalize("12.4 ± 0.8")
    assert ex.numbers("12,4 ± 0.8") == ["12.4", "0.8"]


def test_ids_are_stable_and_solvent_sensitive():
    base = {"plant_part": "rhizome", "protocol": {"solvent": "water"}}
    other = {"plant_part": "rhizome", "protocol": {"solvent": "ethanol"}}
    assert ex.extract_id("S1", base) == ex.extract_id("S1", dict(base))
    assert ex.extract_id("S1", base) != ex.extract_id("S1", other)


def test_chunking_overlaps_long_text():
    parts = ex.chunks("x" * 100_000)
    assert len(parts) == 3 and all(len(p) <= ex.CHUNK_CHARS for p in parts)
    assert ex.chunks("short") == ["short"]


# ---- quality -----------------------------------------------------------------------------------
def test_quality_aggregation_rule():
    all_low = {d: "low concern" for d in quality.DOMAINS}
    assert quality.aggregate(all_low) == "High"
    assert quality.aggregate({**all_low, "Statistical analysis": "high concern"}) == "Moderate"
    assert quality.aggregate({**all_low, "Calibration and reference standards": "high concern"}) == "Low"
    assert quality.aggregate({d: "insufficient information" for d in quality.DOMAINS}) == "Insufficient"


def test_parse_assessment_fills_missing_domains():
    out = quality.parse_assessment({"domains": [{"domain": "Statistical analysis", "rating": "low concern", "evidence": "x"},
                                                {"domain": "Bogus", "rating": "low concern"}]})
    assert len(out) == len(quality.DOMAINS)
    assert [o["rating"] for o in out].count("insufficient information") == len(quality.DOMAINS) - 1


# ---- sheets guard ------------------------------------------------------------------------------
@pytest.mark.parametrize("tab", ["Studies", "Composition_Values", "Robustness_Screen", "Review_Verdicts"])
def test_sheets_refuses_legacy_and_verdict_tabs(tab):
    with pytest.raises(sheets_sync.RefusedWrite):
        sheets_sync.assert_generated(tab)


def test_to_grid_shapes():
    assert sheets_sync.to_grid([]) == [["(no rows)"]]
    assert sheets_sync.to_grid([{"a": 1, "b": None, "c": {"k": 1}}]) == [["a", "b", "c"], ["1", "", '{"k": 1}']]


# ---- workbook import plan ----------------------------------------------------------------------
def plan():
    return workbook_import.build_plan((ROOT / "data/workbook_export.txt").read_text(),
                                      json.load(open(ROOT / "config/workbook_audit_2026-10-30.json")))


@needs_workbook
def test_import_plan_matches_audit_counts():
    rep = plan().report()
    assert rep["studies_by_citation_status"] == {"confirmed": 11, "unmatched_wrong_doi": 17, "unmatched_not_found": 3}
    assert (rep["values_staged_unverified"], rep["values_quarantined"], rep["values_total"]) == (29, 48, 77)
    assert rep["verified_rows_created"] == 0


@needs_workbook
def test_import_never_marks_anything_verified():
    p = plan()
    assert {s["verification_status"] for s in p.studies} == {"U"}
    quarantined_studies = {v["study_id"] for v in p.quarantined_values}
    assert not quarantined_studies & {v["study_id"] for v in p.staged_values}


@needs_workbook
def test_import_fails_loudly_on_unaudited_study():
    audit = json.load(open(ROOT / "config/workbook_audit_2026-10-30.json"))
    del audit["studies"]["BS001"]
    with pytest.raises(ValueError, match="BS001"):
        workbook_import.build_plan((ROOT / "data/workbook_export.txt").read_text(), audit)


# ---- workflow file -----------------------------------------------------------------------------
def test_daily_workflow_is_wired_safely():
    wf = yaml.safe_load((ROOT / ".github/workflows/daily-scan.yml").read_text())
    triggers = wf.get("on") or wf.get(True)
    assert "workflow_dispatch" in triggers and triggers["schedule"][0]["cron"]
    assert wf["concurrency"]["cancel-in-progress"] is False
    assert wf["jobs"]["scan"]["timeout-minutes"] <= 120
    env = {}
    for step in wf["jobs"]["scan"]["steps"]:
        env.update(step.get("env", {}))
    for secret in ("OPENALEX_API_KEY", "DATABASE_URL", "GOOGLE_SERVICE_ACCOUNT_JSON", "SPREADSHEET_ID",
                   "ANTHROPIC_API_KEY", "MAX_LLM_CALLS"):
        assert secret in env


def test_unit_in_table_header_near_quote_is_accepted_but_absent_unit_is_reviewed():
    doc = ex.normalize("Table 3 (mg/g dry weight) Sample A 5.5 ± 0.1 " + "x " * 600 + " Sample B 9.9 ± 0.2")
    near = {"value_original": "5.5", "unit": "mg/g dry weight", "basis": "dry", "method": "HPLC",
            "locator": "Table 3", "source_quote": "Sample A 5.5 ± 0.1"}
    far = {**near, "value_original": "9.9", "source_quote": "Sample B 9.9 ± 0.2"}
    assert ex.judge_measurement(near, doc)[0] == "verified"
    v, why = ex.judge_measurement(far, doc)
    assert v == "review" and any("unit not found" in r for r in why)


def test_search_uses_title_abstract_filter_not_fulltext_search_param():
    p = oas.search_params('("Curcuma longa" OR "turmeric") AND ("tannin")', "publication_year:2020")
    assert "search" not in p
    assert p["filter"].startswith("title_and_abstract.search:(")
    assert "is_retracted:false" in p["filter"] and p["filter"].endswith("publication_year:2020")
    assert p["sort"] == "relevance_score:desc"
    assert "," not in oas.search_params("a, b")["filter"].split(",is_retracted")[0]  # commas would split the filter


def test_screening_recovers_truncated_and_reports_unparseable_replies():
    from src import screening

    class L:
        def __init__(self, *replies): self.replies, self.calls = list(replies), 0
        def ask(self, *a, **k): self.calls += 1; return self.replies.pop(0)

    cut = '{"decision": "likely_eligible", "reason": "Original study measuring phenolic con'
    assert screening.classify(L(cut), "x", "t", "a")["decision"] == "likely_eligible"
    ok = screening.classify(L("Sure! here you go", '{"decision": "uncertain", "reason": "r"}'), "x", "t", "a")
    assert ok == {"decision": "uncertain", "reason": "r"}
    bad = screening.classify(L("blah", "still blah"), "x", "t", "a")
    assert bad["decision"] == "uncertain" and "unparseable reply: still blah" in bad["reason"]


def test_europe_pmc_is_tried_first_for_pmc_records():
    from src import fulltext

    urls = fulltext.candidate_urls([{"pdf": "https://pub.example/a.pdf", "landing": None, "version": "publishedVersion",
                                     "source": "Pub"}], "PMC3071761")
    assert urls[0]["url"] == "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC3071761/fullTextXML"
    assert urls[1]["url"] == "https://pub.example/a.pdf"
    assert [u["url"] for u in fulltext.candidate_urls([{"pdf": "https://x/y.pdf", "landing": None, "version": None, "source": None}])] == ["https://x/y.pdf"]


def test_abstract_sized_pages_are_not_treated_as_full_text():
    from src import fulltext

    class R:
        status_code, headers, raw = 200, {"content-type": "text/html"}, None
        content = b"<html><body>" + b"Abstract text. " * 200 + b"</body></html>"  # ~3,000 chars

    class S:
        def get(self, *a, **k): return R()

    text, err = fulltext.fetch_text("https://x/y", S())
    assert text is None and "text too short" in err and "abstract page" in err
    R.content = b"<html><body>" + b"Methods and results sentence. " * 400 + b"</body></html>"  # ~12,000 chars
    assert fulltext.fetch_text("https://x/y", S())[0] is not None


# ---- unit rule: only recognized, unambiguous units can be verified ---------------------------------
from src import units  # noqa: E402


@pytest.mark.parametrize("unit,expected", [
    ("mg/100 g", 1.0), ("mg/g", 100.0), ("g/kg", 100.0), ("%", 1000.0), ("g/100 g", 1000.0),
    ("\u00b5g/g", 0.1), ("mcg/g", 0.1), ("ug/100 g", 0.001), ("mg/kg", 0.1), ("mg g-1", 100.0),
    ("mg/g dry weight", 100.0), ("mg/g extract", 100.0), ("mg/100 g fresh weight", 1.0),
])
def test_mass_units_normalize_to_mg_per_100g(unit, expected):
    v, u = units.normalize(1.0, unit)
    assert u == "mg/100 g" and v == pytest.approx(expected)


@pytest.mark.parametrize("unit,cls", [
    ("ppm", "ambiguous"), ("ppb", "ambiguous"), ("mg GAE/g dry weight", "equivalent"), ("\u00b5g/mL", "per_volume"),
    ("IC50 \u00b5g/mL", "activity"), ("", "unknown"), ("furlongs", "unknown"), ("mg", "unknown"),
])
def test_unit_classes(unit, cls):
    assert units.classify(unit)["class"] == cls
    if cls != "unknown" or unit == "":
        pass
    assert units.normalize(5.0, unit) == (None, None)  # never converted


def _row(unit, value="3.40", quote="Calcium 3.40"):
    return {"value_original": value, "unit": unit, "basis": "dry", "method": "AAS", "locator": "Table 1",
            "source_quote": quote, "uncertainty_type": "SD"}


def test_ppm_and_unknown_units_can_never_be_verified():
    doc = ex.normalize("Table 1 (mg/g) Calcium 3.40 ppm")
    for unit in ("ppm", "", "furlongs", "mg"):
        verdict, why = ex.judge_measurement(_row(unit), doc)
        assert verdict == "review" and why, unit
    assert ex.judge_measurement(_row("mg/g"), doc)[0] == "verified"


def test_values_above_100_percent_of_the_sample_are_quarantined_in_any_mass_unit():
    doc = ex.normalize("Table 1 Calcium 150 g/100 g; Iron 2000 mg/g; Zinc 90 %")
    assert ex.judge_measurement(_row("g/100 g", "150", "Calcium 150"), doc)[0] == "quarantine"
    assert ex.judge_measurement(_row("mg/g", "2000", "Iron 2000"), doc)[0] == "quarantine"
    assert ex.judge_measurement(_row("%", "90", "Zinc 90"), doc)[0] != "quarantine"


def test_ranges_and_inequalities_are_not_normalized_or_verified():
    doc = ex.normalize("Table 1 (mg/g) Iron 2.1-3.4 Zinc <0.1")
    for val, q in (("2.1-3.4", "Iron 2.1-3.4"), ("<0.1", "Zinc <0.1")):
        verdict, why = ex.judge_measurement(_row("mg/g", val, q), doc)
        assert verdict == "review" and any("range, inequality" in w for w in why)
