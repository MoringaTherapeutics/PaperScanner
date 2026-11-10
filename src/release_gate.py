"""Fail-closed, read-only quality/release audit.

'Quote verified' means the reported text was found; it is NOT analytical-method
validation. This module never changes a measurement or automatically declares a
species complete. Human decisions and coverage are separate gates.
"""
from __future__ import annotations

from collections import Counter
from .database import fetch_all
from .quality import DOMAINS, CORE, aggregate

MIN_PAPERS = 20
MIN_ACCURACY = 0.95


def evaluate_measurement(m: dict, domains: dict[str, str] | None = None) -> dict:
    """Check one observation for eligibility in a publication-ready export."""
    issues = []
    if m.get("verification_status") != "Y":
        issues.append("not source-verified")
    if m.get("verified_by") in (None, "", "quote-check"):
        issues.append("requires independent human verification")
    if not m.get("source_quote") or not m.get("locator"):
        issues.append("missing source quote or locator")
    if not m.get("unit_original"):
        issues.append("missing unit")
    if m.get("basis") in (None, "", "unknown"):
        issues.append("unknown measurement basis")
    if not m.get("method"):
        issues.append("missing analytical method")
    if m.get("value_numeric") is None:
        issues.append("not a simple numerical value")
    # Missing LOD/LOQ, recovery, calibration, n, or uncertainty are appraisal
    # concerns, not invented zero values. Require documented domain assessment.
    domains = domains or {}
    if set(domains) != set(DOMAINS):
        issues.append("quality assessment incomplete")
    else:
        strength = aggregate(domains)
        if strength in ("Low", "Insufficient"):
            issues.append(f"methodological evidence {strength.lower()}")
        if any(domains.get(d) in ("high concern", "insufficient information") for d in CORE):
            issues.append("core analytical quality unresolved")
    return {"pass": not issues, "issues": issues}


def check_search_runs(runs: list[dict]) -> dict:
    """Reconcile only completed, immutable run snapshots; never sum query hits as unique works."""
    issues = []
    for r in runs:
        rid = r["run_id"]
        if not r.get("pagination_complete"):
            issues.append(f"{rid}: cursor not exhausted")
        if r.get("reported_total") is None:
            issues.append(f"{rid}: missing reported total")
        elif r["reported_total"] != (r.get("retrieved_count") or 0) + (r.get("failed_count") or 0):
            issues.append(f"{rid}: reported/retrieved/failed mismatch")
        if r.get("errors"):
            issues.append(f"{rid}: unresolved retrieval error")
    if not runs:
        issues.append("no logged discovery runs")
    return {"pass": not issues, "issues": issues, "runs_checked": len(runs)}


def evaluate_pilot(papers_reviewed: int, values_checked: int, values_matching: int,
                   unit_basis_errors: int, duplicates_unresolved: int,
                   search: dict, coverage_signoff: bool = False) -> dict:
    """Research validation thresholds; no unsupported claim of universal completeness."""
    issues = []
    if papers_reviewed < MIN_PAPERS:
        issues.append(f"hand-reviewed full texts: {papers_reviewed}/{MIN_PAPERS}")
    if values_checked == 0 or values_matching / values_checked < MIN_ACCURACY:
        issues.append("hand-checked value accuracy below 95% or unmeasured")
    if unit_basis_errors:
        issues.append(f"{unit_basis_errors} unit/basis errors")
    if duplicates_unresolved:
        issues.append(f"{duplicates_unresolved} unresolved duplicate groups")
    if not search.get("pass"):
        issues.append("discovery reconciliation failed")
    if not coverage_signoff:
        issues.append("human coverage/limitations sign-off missing")
    return {"pass": not issues, "issues": issues,
            "accuracy": values_matching / values_checked if values_checked else None}


def audit_database(conn, species_id: str) -> dict:
    """Produce concrete counts without editing legacy workbooks or database records."""
    runs = fetch_all(conn, "SELECT * FROM search_runs WHERE species_id=%s", [species_id])
    search = check_search_runs(runs)
    values = fetch_all(conn, """SELECT m.measurement_id, m.verification_status, m.verified_by,
        m.source_quote, m.locator, m.unit_original, m.basis, m.method, m.value_numeric,
        s.study_id FROM measurements m JOIN extracts e USING (extract_id)
        JOIN studies s USING (study_id) WHERE s.species_id=%s""", [species_id])
    quality_rows = fetch_all(conn, """SELECT qa.study_id, qa.domain, qa.rating FROM quality_assessments qa
        JOIN studies s USING (study_id) WHERE s.species_id=%s""", [species_id])
    quality_by_study = {}
    for row in quality_rows:
        quality_by_study.setdefault(row["study_id"], {})[row["domain"]] = row["rating"]
    reasons = Counter()
    approved = 0
    for v in values:
        check = evaluate_measurement(v, quality_by_study.get(v["study_id"]))
        if check["pass"]:
            approved += 1
        else:
            reasons.update(check["issues"])
    unresolved = fetch_all(conn, """SELECT count(*) AS n FROM publications p
        JOIN species_publications sp ON sp.publication_id=p.publication_id
        WHERE sp.species_id=%s AND p.duplicate_of IS NOT NULL""", [species_id])[0]["n"]
    # Those linked by DOI are canonicalized, not necessarily unresolved; the
    # published duplicate-review audit must separately document title/version pairs.
    return {"species_id": species_id, "search": search,
            "measurements": len(values), "release_eligible_measurements": approved,
            "blocked_measurements": len(values) - approved,
            "blocker_counts": dict(reasons), "linked_duplicate_reports": unresolved,
            "human_pilot_verified": False,
            "publication_ready": False,
            "note": "Publication-ready status requires a documented 20-paper human pilot, "
                    "95% accuracy, zero unit/basis errors, duplicate resolution and coverage sign-off."}
