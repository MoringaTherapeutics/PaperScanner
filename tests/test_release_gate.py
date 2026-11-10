"""Fail-closed release criteria tests. No external services required."""
from src.release_gate import check_search_runs, evaluate_measurement, evaluate_pilot
from src.quality import DOMAINS


def measurement(**changes):
    m = {"verification_status": "Y", "verified_by": "human-reviewer",
         "source_quote": "Curcumin 5 mg/g", "locator": "Table 1",
         "unit_original": "mg/g", "basis": "dry", "method": "HPLC",
         "value_numeric": 5}
    m.update(changes)
    return m


def all_low_concern():
    return {domain: "low concern" for domain in DOMAINS}


def test_quote_only_is_not_independent_human_validation():
    result = evaluate_measurement(measurement(verified_by="quote-check"), all_low_concern())
    assert not result["pass"]
    assert "requires independent human verification" in result["issues"]


def test_absent_quality_domain_fails_closed():
    result = evaluate_measurement(measurement(), {"Statistical analysis": "low concern"})
    assert not result["pass"]
    assert "quality assessment incomplete" in result["issues"]


def test_core_methodological_concern_blocks_release():
    ratings = all_low_concern()
    ratings["Calibration and reference standards"] = "high concern"
    assert not evaluate_measurement(measurement(), ratings)["pass"]


def test_complete_human_checked_measurement_passes_release_gate():
    assert evaluate_measurement(measurement(), all_low_concern())["pass"]


def test_search_gate_rejects_cursor_and_mismatch():
    runs = [{"run_id": "a", "pagination_complete": False,
             "reported_total": 50, "retrieved_count": 40, "failed_count": 0}]
    result = check_search_runs(runs)
    assert not result["pass"] and len(result["issues"]) == 2


def test_search_gate_rejects_empty_and_missing_total():
    assert not check_search_runs([])["pass"]
    assert not check_search_runs([{"run_id": "b", "pagination_complete": True,
                                   "retrieved_count": 5}])["pass"]


def test_search_gate_accepts_reconciled_completed_runs():
    assert check_search_runs([{"run_id": "a", "pagination_complete": True,
                               "reported_total": 50, "retrieved_count": 50,
                               "failed_count": 0, "errors": None}])["pass"]


def test_pilot_needs_full_text_hand_checks_and_signoff():
    s = {"pass": True}
    assert not evaluate_pilot(19, 100, 100, 0, 0, s, True)["pass"]
    assert not evaluate_pilot(20, 100, 94, 0, 0, s, True)["pass"]
    assert not evaluate_pilot(20, 100, 100, 1, 0, s, True)["pass"]
    assert not evaluate_pilot(20, 100, 100, 0, 1, s, True)["pass"]
    assert not evaluate_pilot(20, 100, 100, 0, 0, s, False)["pass"]
    assert evaluate_pilot(20, 100, 95, 0, 0, s, True)["pass"]
