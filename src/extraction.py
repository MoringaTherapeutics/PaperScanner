"""Model-assisted extraction with deterministic provenance checks.

The model only reads and structures text. Code decides what is trusted:
  * the quoted source text must appear in the retrieved document,
  * the reported number must appear inside that quote,
  * units/basis/plausibility are checked,
and anything that fails goes to quarantine instead of the verified tables.
"""
from __future__ import annotations

import hashlib
import os
import re
import unicodedata

from . import units
from .database import upsert
from .llm import LLM, extract_json

EXTRACT_MODEL = os.environ.get("EXTRACT_MODEL", "claude-sonnet-5-5")
CHUNK_CHARS = 45_000
CHUNK_OVERLAP = 2_000

SYSTEM = """You extract chemical-composition data from ONE scientific paper about an edible plant.
Rules:
- Each independently prepared extract is its own record: different solvent, solvent concentration,
  temperature, time, technique or plant part = different extract. Never merge solvents. Do not create
  records for technical replicates. For sequential extraction keep each fraction and its order.
- Report only values printed in the text. Copy the value EXACTLY as printed in value_original.
- source_quote must be copied verbatim from the paper (a table row, a sentence) and contain the number.
- locator = page / table / figure / supplement where the quote sits. Page markers look like [[page N]].
- basis: fresh, dry, extract, other or unknown. NEVER convert between bases. If not stated use unknown.
- Do not confuse concentration with extraction yield, recovery, antioxidant activity or bioavailability.
- Use "unknown" for any method parameter not stated. Never infer.
- category is one of: macronutrients, minerals, polyphenols, tannins, bioactives, antioxidant_assays,
  vitamins, amino_acids, fatty_acids, antinutrients, pigments, volatiles.
- If the paper is a review, clinical/pharmacological only, or has no original composition data, set
  eligible=false with a reason and return no extracts.
Reply with JSON only:
{"eligible": bool, "reason": str, "extracts": [{
  "plant_part": str, "cultivar": str, "origin": str, "sample_state": "fresh|dried|other|unknown",
  "preparation": str, "condition": str, "extraction_yield": str,
  "biological_replicates": str, "technical_replicates": str, "method_locator": str,
  "protocol": {"technique": str, "solvent": str, "solvent_concentration": str, "solid_liquid_ratio": str,
               "temperature": str, "duration": str, "cycles": str, "pressure": str, "sequential_order": int|null},
  "measurements": [{"analyte": str, "category": str, "value_original": str, "unit": str, "basis": str,
                    "uncertainty_type": "SD|SE|CI|range|none|unknown", "uncertainty_value": str, "n": int|null,
                    "method": str, "calibration_standard": str, "lod": str, "loq": str,
                    "locator": str, "source_quote": str}]}]}"""

CATEGORY_FLAG = {
    "macronutrients": "flag_macronutrients", "minerals": "flag_minerals", "polyphenols": "flag_polyphenols",
    "tannins": "flag_tannins", "bioactives": "flag_bioactives", "antioxidant_assays": "flag_antioxidant_assays",
}

# ---------- identifiers -------------------------------------------------------------------------
def _h(*parts: object) -> str:
    return hashlib.sha1("|".join("" if p is None else str(p).strip().lower() for p in parts).encode()).hexdigest()[:16]


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "unknown"


def protocol_id(p: dict) -> str:
    keys = ("technique", "solvent", "solvent_concentration", "solid_liquid_ratio", "temperature",
            "duration", "cycles", "pressure", "sequential_order")
    return _h(*(p.get(k) for k in keys))


def extract_id(study_id: str, e: dict) -> str:
    return _h(study_id, e.get("plant_part"), e.get("preparation"), e.get("cultivar"),
              protocol_id(e.get("protocol") or {}), e.get("condition"))


def measurement_id(ext_id: str, analyte: str, basis: str, locator: str, value: str) -> str:
    return _h(ext_id, analyte, basis, locator, value)


# ---------- verification ------------------------------------------------------------------------
def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    t = t.replace("­", "").replace("−", "-").replace("–", "-").replace("—", "-")
    t = t.replace("±", "±").replace("µ", "u").replace("μ", "u")
    t = re.sub(r"-\s*\n\s*", "", t)  # re-join hyphenated line breaks
    return re.sub(r"\s+", " ", t).strip().lower()


_NUM = re.compile(r"\d+(?:[.,]\d+)?")
SIMPLE_VALUE = re.compile(r"^\s*\d+(?:[.,]\d+)?(?:\s*(?:±|\+/-)\s*\d+(?:[.,]\d+)?)?\s*$")


def numbers(s: str) -> list[str]:
    return [n.replace(",", ".") for n in _NUM.findall(normalize(s))]


def to_float(s: str) -> float | None:
    nums = numbers(s)
    try:
        return float(nums[0]) if nums else None
    except ValueError:
        return None


def judge_measurement(m: dict, doc_norm: str) -> tuple[str, list[str]]:
    """Return ('verified'|'review'|'quarantine', reasons). doc_norm = normalize(full text)."""
    hard, soft = [], []
    quote = (m.get("source_quote") or "").strip()
    value = (m.get("value_original") or "").strip()
    unit = (m.get("unit") or "").strip()
    locator = (m.get("locator") or "").strip()

    if not quote:
        hard.append("missing source_quote")
    elif normalize(quote) not in doc_norm:
        hard.append("source_quote not found verbatim in document")
    if not value:
        hard.append("missing value_original")
    elif quote:
        want, have = numbers(value), set(numbers(quote))
        if not want:
            hard.append("value_original contains no number")
        elif not all(n in have for n in want):
            hard.append("value not present in quoted text")
    if not locator:
        hard.append("missing locator")

    v = to_float(value)
    info = units.classify(unit)
    if info["class"] in ("unknown", "ambiguous"):
        soft.append(info["reason"])  # never verified with an unrecognized or ambiguous unit
    elif info["class"] == "mass_fraction" and v is not None:
        norm = units.to_mg_per_100g(v, info)
        if norm is not None and norm > units.MAX_MG_PER_100G:
            hard.append(f"implausible: {v} {unit} exceeds 100 g per 100 g")
    if v is not None and v < 0:
        hard.append("negative concentration")
    if value and not SIMPLE_VALUE.match(value):
        soft.append("value is a range, inequality or has extra text; check before normalizing")

    if (m.get("basis") or "unknown") == "unknown":
        soft.append("basis not stated")
    if unit and info["class"] not in ("unknown", "ambiguous"):
        nu, nq = normalize(unit), normalize(quote)
        if nu not in nq:
            pos = doc_norm.find(nq)  # table units usually sit in the header just above the row
            window = doc_norm[max(0, pos - 400): pos + len(nq) + 100] if pos >= 0 else ""
            if nu not in window:
                soft.append("unit not found in or near the quote")
    if m.get("uncertainty_type") in (None, "", "unknown") and m.get("uncertainty_value"):
        soft.append("uncertainty type unclear")
    if not m.get("method"):
        soft.append("analytical method not stated")

    if hard:
        return "quarantine", hard + soft
    return ("review", soft) if soft else ("verified", [])


# ---------- model call --------------------------------------------------------------------------
def chunks(text: str) -> list[str]:
    if len(text) <= CHUNK_CHARS:
        return [text]
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + CHUNK_CHARS])
        if i + CHUNK_CHARS >= len(text):
            break
        i += CHUNK_CHARS - CHUNK_OVERLAP
    return out


def run_model(llm: LLM, species_name: str, title: str, text: str, model: str = EXTRACT_MODEL) -> dict:
    """Extract from each chunk and merge. eligible is true if any chunk found data."""
    merged = {"eligible": False, "reason": "", "extracts": [], "parse_failures": 0}
    for part in chunks(text):
        reply = llm.ask(SYSTEM, f"Species: {species_name}\nTitle: {title}\n\nPAPER TEXT:\n{part}", model, max_tokens=8000)
        data = extract_json(reply)
        if not data:
            merged["parse_failures"] += 1
            continue
        merged["eligible"] = merged["eligible"] or bool(data.get("eligible"))
        merged["reason"] = merged["reason"] or str(data.get("reason", ""))
        merged["extracts"].extend(data.get("extracts") or [])
    return merged


# ---------- persistence -------------------------------------------------------------------------
def store_extraction(conn, study_id: str, species_id: str, result: dict, doc_text: str, model: str) -> dict:
    """Judge every measurement; verified/review go to measurements, failures to quarantine."""
    doc_norm = normalize(doc_text)
    counts = {"extracts": 0, "verified": 0, "review": 0, "quarantined": 0}
    for e in result.get("extracts", []):
        p = e.get("protocol") or {}
        pid, eid = protocol_id(p), extract_id(study_id, e)
        upsert(conn, "extraction_protocols", {
            "protocol_id": pid, **{k: (str(p[k]) if p.get(k) not in (None, "") else None) for k in
                                    ("technique", "solvent", "solvent_concentration", "solid_liquid_ratio",
                                     "temperature", "duration", "cycles", "pressure")},
            "sequential_order": p.get("sequential_order") if isinstance(p.get("sequential_order"), int) else None,
        }, ["protocol_id"], update_cols=())
        flags: dict[str, str] = {}
        kept = 0
        for m in e.get("measurements") or []:
            verdict, reasons = judge_measurement(m, doc_norm)
            if verdict == "quarantine":
                upsert(conn, "quarantine", {
                    "kind": "measurement", "ref_id": study_id,
                    "payload": {"extract_key": eid, **m}, "reasons": reasons,
                }, ["kind", "ref_id", "payload"], update_cols=())
                counts["quarantined"] += 1
                continue
            if kept == 0:  # parent extract only exists if it has at least one trusted-or-reviewable value
                upsert(conn, "extracts", {
                    "extract_id": eid, "study_id": study_id, "species_id": species_id,
                    "plant_part": e.get("plant_part"), "cultivar": e.get("cultivar"),
                    "origin_geo": e.get("origin"), "sample_state": e.get("sample_state") or "unknown",
                    "preparation": e.get("preparation"), "protocol_id": pid, "condition": e.get("condition"),
                    "extraction_yield": e.get("extraction_yield"),
                    "biological_replicates": e.get("biological_replicates"),
                    "technical_replicates": e.get("technical_replicates"),
                    "method_locator": e.get("method_locator"),
                }, ["extract_id"])
                counts["extracts"] += 1
            kept += 1
            aid = slug(m.get("analyte") or "")
            upsert(conn, "analytes", {"analyte_id": aid, "name": m.get("analyte") or aid,
                                      "category": m.get("category")}, ["analyte_id"], update_cols=())
            basis = m.get("basis") if m.get("basis") in ("fresh", "dry", "extract", "other") else "unknown"
            n = m.get("n") if isinstance(m.get("n"), int) else None
            norm_v, norm_u = (units.normalize(to_float(m["value_original"]), m.get("unit"))
                              if SIMPLE_VALUE.match(m["value_original"]) else (None, None))
            ut = m.get("uncertainty_type") if m.get("uncertainty_type") in ("SD", "SE", "CI", "range", "none") else "unknown"
            upsert(conn, "measurements", {
                "measurement_id": measurement_id(eid, aid, basis, m["locator"], m["value_original"]),
                "extract_id": eid, "analyte_id": aid, "value_original": m["value_original"],
                "value_numeric": to_float(m["value_original"]), "unit_original": m.get("unit"),
                "value_normalized": norm_v, "unit_normalized": norm_u,
                "basis": basis, "uncertainty_type": ut, "uncertainty_value": m.get("uncertainty_value"),
                "n": n, "method": m.get("method"), "calibration_standard": m.get("calibration_standard"),
                "lod": m.get("lod"), "loq": m.get("loq"), "locator": m["locator"],
                "source_quote": m["source_quote"],
                "verification_status": "Y" if verdict == "verified" else "U",
                "verified_by": "quote-check" if verdict == "verified" else None,
                "model_version": model,
            }, ["measurement_id"], update_cols=[
                "value_numeric", "unit_original", "value_normalized", "unit_normalized", "uncertainty_type", "uncertainty_value", "n", "method",
                "model_version"])  # value_original / quote / status are never silently rewritten
            counts["verified" if verdict == "verified" else "review"] += 1
            flag = CATEGORY_FLAG.get(m.get("category") or "")
            if flag:
                flags[flag] = "Y"
        if flags and kept:
            conn.execute(
                "UPDATE extracts SET " + ", ".join(f"{c}=%s" for c in flags) + " WHERE extract_id=%s",
                [*flags.values(), eid],
            )
    conn.commit()
    return counts
