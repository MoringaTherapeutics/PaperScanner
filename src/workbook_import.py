"""Import the legacy 'per Species Chemical Analysis Robustness Data' workbook.

Rules (see the build spec):
  * Every study is loaded with verification_status = 'U'.
  * Value rows on studies whose citation was confirmed in OpenAlex are staged in
    legacy_values, still 'U'. A confirmed citation does not verify a number.
  * Value rows and robustness scores on studies OpenAlex could not match are moved
    to the quarantine table, never to the verified dataset, and never deleted.
"""
from __future__ import annotations

import csv
import io
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

SPECIES_BY_PREFIX = {
    "BS": "SP_BRASSICA_OLERACEA_GEMMIFERA",
    "GI": "SP_ZINGIBER_OFFICINALE",
    "SS": "SP_ANNONA_MURICATA",
    "CC": "SP_COCOS_NUCIFERA",
    "BM": "SP_MOMORDICA_CHARANTIA",
    "TU": "SP_CURCUMA_LONGA",
    "WM": "SP_CITRULLUS_LANATUS",
    "OR": "SP_CITRUS_SINENSIS",
    "BL": "SP_VACCINIUM_CORYMBOSUM",
    "WA": "SP_JUGLANS_REGIA",
    "OK": "SP_ABELMOSCHUS_ESCULENTUS",
    "CH": None,  # sheet lumps Prunus avium and P. cerasus together; resolve on re-sourcing
}

SHEET_MARK = "## Sheet name: "


def parse_export(text: str) -> dict[str, list[dict[str, str]]]:
    """Split the multi-sheet CSV export into {sheet_name: [row dicts]}."""
    sheets: dict[str, list[dict[str, str]]] = {}
    chunks = text.split(SHEET_MARK)[1:]
    for chunk in chunks:
        name, _, body = chunk.partition("\n")
        body = body.strip("\n")
        sheets[name.strip()] = list(csv.DictReader(io.StringIO(body)))
    return sheets


@dataclass
class ImportPlan:
    studies: list[dict] = field(default_factory=list)
    staged_values: list[dict] = field(default_factory=list)
    quarantined_values: list[dict] = field(default_factory=list)
    staged_robustness: list[dict] = field(default_factory=list)
    quarantined_robustness: list[dict] = field(default_factory=list)

    def report(self) -> dict:
        status = Counter(s["citation_status"] for s in self.studies)
        return {
            "studies_total": len(self.studies),
            "studies_by_citation_status": dict(status),
            "values_total": len(self.staged_values) + len(self.quarantined_values),
            "values_staged_unverified": len(self.staged_values),
            "values_quarantined": len(self.quarantined_values),
            "robustness_staged": len(self.staged_robustness),
            "robustness_quarantined": len(self.quarantined_robustness),
            "verified_rows_created": 0,
        }


def _norm_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    d = doi.strip().lower()
    for p in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(p):
            d = d[len(p):]
    return d if d.startswith("10.") else None


def build_plan(export_text: str, audit: dict) -> ImportPlan:
    sheets = parse_export(export_text)
    audited = audit["studies"]
    plan = ImportPlan()
    status_by_study: dict[str, str] = {}

    for row in sheets["Studies"]:
        sid = row["Study_ID"]
        info = audited.get(sid)
        if info is None:
            raise ValueError(f"{sid} missing from audit file; re-run the audit before importing")
        status = "confirmed" if info["status"] == "confirmed" else info["status"]
        status_by_study[sid] = status
        fixed = info.get("corrected", {})
        claimed_doi = _norm_doi(row.get("DOI"))
        note = {
            "confirmed": "Citation found in OpenAlex; values not yet checked against full text.",
            "unmatched_wrong_doi": "Sheet DOI resolves to a different paper and the title was not found. "
            + f"Resolves to: {info.get('resolved_to', 'n/a')}.",
            "unmatched_not_found": "Neither the DOI nor the title was found in OpenAlex.",
        }[status]
        if info.get("note"):
            note += " " + info["note"]
        plan.studies.append(
            {
                "study_id": sid,
                "species_id": SPECIES_BY_PREFIX.get(sid[:2]),
                "claimed_title": row["Title"],
                "claimed_doi": claimed_doi,
                "verification_status": "U",
                "verification_note": note,
                "origin": "legacy_workbook",
                "citation_status": status,
                "openalex_id": info.get("openalex_id"),
                "corrected_metadata": fixed,
            }
        )

    for i, row in enumerate(sheets["Composition_Values"], start=1):
        sid = row["Study_ID"]
        rec = {
            "study_id": sid,
            "row_no": i,
            "plant_part_or_state": row["Plant_Part_or_State"],
            "endpoint": row["Endpoint_or_Compound"],
            "value_text": row["Numeric_Value_or_Range"],
            "unit_text": row["Reported_Unit"],
            "value_type": row["Value_Type"],
            "basis_text": row["Basis_and_Interpretation"],
            "method": row["Analytical_Method"],
            "source_note": row["Source_Location_Note"],
        }
        if status_by_study.get(sid) == "confirmed":
            plan.staged_values.append(rec)
        else:
            rec["reasons"] = [
                f"study {sid}: {status_by_study.get(sid, 'unknown study')} (see audit 2026-10-30)",
                "value cannot be traced to a source paper",
            ]
            plan.quarantined_values.append(rec)

    for row in sheets["Robustness_Screen"]:
        sid = row["Study_ID"]
        rec = {
            "study_id": sid,
            "domain": row["Domain"],
            "score_0_3": int(row["Provisional_Score_0_to_3"]) if row["Provisional_Score_0_to_3"].isdigit() else None,
            "evidence": row["Evidence_Observed"],
            "caution": row["Missing_or_Caution"],
        }
        if status_by_study.get(sid) == "confirmed":
            plan.staged_robustness.append(rec)
        else:
            rec["reasons"] = [f"study {sid}: {status_by_study.get(sid, 'unknown study')}; score has no verified source"]
            plan.quarantined_robustness.append(rec)
    return plan


def write_dry_run(plan: ImportPlan, out_dir: str | Path) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    def dump(name: str, rows: list[dict]) -> None:
        if not rows:
            (out / name).write_text("")
            return
        cols = list(rows[0])
        with open(out / name, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in r.items()})

    dump("legacy_studies.csv", plan.studies)
    dump("legacy_values_staged.csv", plan.staged_values)
    dump("quarantine_values.csv", plan.quarantined_values)
    dump("legacy_robustness_staged.csv", plan.staged_robustness)
    dump("quarantine_robustness.csv", plan.quarantined_robustness)
    (out / "import_report.json").write_text(json.dumps(plan.report(), indent=2))


def write_to_db(plan: ImportPlan, conn) -> None:
    from .database import upsert

    for s in plan.studies:
        row = {k: v for k, v in s.items() if k != "corrected_metadata"}
        upsert(conn, "studies", row, ["study_id"])  # claimed_doi keeps the ORIGINAL sheet value
        for field_name, new_value in s["corrected_metadata"].items():
            old = s["claimed_doi"] if field_name == "doi" else None
            conn.execute(
                "INSERT INTO corrections(table_name,row_id,field,old_value,new_value,reason,corrected_by) "
                "SELECT %s,%s,%s,%s,%s,%s,%s WHERE NOT EXISTS ("
                "SELECT 1 FROM corrections WHERE table_name=%s AND row_id=%s AND field=%s AND new_value=%s)",
                ["studies", s["study_id"], field_name, old, str(new_value),
                 "OpenAlex audit 2026-10-30", "workbook_import",
                 "studies", s["study_id"], field_name, str(new_value)],
            )
    for v in plan.staged_values:
        upsert(conn, "legacy_values", v, ["study_id", "row_no"])
    for r in plan.staged_robustness:
        upsert(conn, "legacy_robustness", r, ["study_id", "domain"])
    for v in plan.quarantined_values:
        payload = {k: x for k, x in v.items() if k != "reasons"}
        upsert(conn, "quarantine",
               {"kind": "legacy_value", "ref_id": v["study_id"], "payload": payload, "reasons": v["reasons"]},
               ["kind", "ref_id", "payload"], update_cols=())
    for r in plan.quarantined_robustness:
        payload = {k: x for k, x in r.items() if k != "reasons"}
        upsert(conn, "quarantine",
               {"kind": "legacy_robustness", "ref_id": r["study_id"], "payload": payload, "reasons": r["reasons"]},
               ["kind", "ref_id", "payload"], update_cols=())
    conn.commit()
