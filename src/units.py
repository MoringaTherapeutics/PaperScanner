"""Unit recognition and safe normalization.

A value can only be verified if its unit is recognized and unambiguous.
Conversion happens only between units that mean the same thing (e.g. ug/g -> mg/100 g,
% -> mg/100 g). Fresh/dry/extract bases are never converted; that needs a moisture measurement.

Classes:
  mass_fraction  mass of analyte per mass of sample or extract  -> normalized to mg/100 g
  equivalent     assay equivalents (mg GAE/g, mg QE/g, mg TE/g ...) -> recognized, NOT normalized
  per_volume     per mL / per L                                  -> recognized, NOT normalized
  activity       IC50, EC50, umol Fe(II), % inhibition, etc.     -> recognized, NOT normalized
  ambiguous      ppm, ppb, mg%  (meaning depends on the matrix)  -> review
  unknown        anything else / empty                           -> review
"""
from __future__ import annotations

import re

MASS_TO_MG = {"ng": 1e-6, "ug": 1e-3, "mg": 1.0, "g": 1e3, "kg": 1e6}
DEN_TO_G = {"100 g": 100.0, "g": 1.0, "kg": 1000.0, "100 mg": 0.1, "mg": 1e-3}
NORMALIZED_UNIT = "mg/100 g"

_WEIGHT_TAIL = r"(?:\s*(?:of\s+)?(?:fresh|dry|dried|fw|dw|dm|f\.?w\.?|d\.?w\.?|extract|sample|material|powder|tissue|matter|mass|weight|wt|basis|[a-z]*\s*extract))*"
_MASS_FRACTION = re.compile(
    rf"^(?P<num>ng|ug|mg|g|kg)\s*(?:/|per)\s*(?P<den>100\s*g|100\s*mg|kg|g|mg){_WEIGHT_TAIL}$"
)
_PERCENT = re.compile(rf"^%(?:\s*(?:w\s*/\s*w|wt|g\s*/\s*100\s*g))?{_WEIGHT_TAIL}$")
_EQUIVALENT = re.compile(r"\b(gae|qe|te|ce|re|aae|cae|equiv\w*|equivalents?)\b")
_ACTIVITY = re.compile(r"(ic\s*50|ec\s*50|um\b|umol|mmol|mol\b|inhibition|scaveng|trolox|fe\s*\[?(ii|2\+)\]?|abts|dpph|frap|orac|meq|\bmeq\b)")
_PER_VOLUME = re.compile(r"/\s*(?:ml|l|dl|100\s*ml)\b|\bper\s+(?:ml|l)\b")
_AMBIGUOUS = re.compile(r"^(?:ppm|ppb|ppt|mg\s*%|mg%)\b")


def clean(unit: str | None) -> str:
    u = (unit or "").strip().lower()
    u = u.replace("µ", "u").replace("μ", "u").replace("mcg", "ug")
    u = re.sub(r"\s+", " ", u)
    u = u.replace("^-1", "").replace("−1", "")
    # "mg g-1", "mg.g-1", "mg kg-1" -> "mg/g", "mg/kg"
    u = re.sub(r"\b(ng|ug|mg|g|kg)\s*[.·]?\s*(100\s*g|100\s*mg|g|kg|mg)\s*-\s*1\b", r"\1/\2", u)
    u = re.sub(r"\s*/\s*", "/", u)
    return u


def classify(unit: str | None) -> dict:
    """Return {"class", "unit_clean", "num", "den", "reason"}."""
    u = clean(unit)
    if not u:
        return {"class": "unknown", "unit_clean": u, "reason": "unit missing"}
    if _AMBIGUOUS.match(u):
        return {"class": "ambiguous", "unit_clean": u,
                "reason": f"'{unit}' is ambiguous (mg/kg vs mg/L depends on the matrix); needs the paper's stated basis"}
    if _EQUIVALENT.search(u):
        return {"class": "equivalent", "unit_clean": u}
    if _ACTIVITY.search(u):
        return {"class": "activity", "unit_clean": u}
    if _PER_VOLUME.search(u):
        return {"class": "per_volume", "unit_clean": u}
    pm = _PERCENT.match(u)
    if pm:
        return {"class": "mass_fraction", "unit_clean": u, "num": "g", "den": "100 g"}
    m = _MASS_FRACTION.match(u.replace("100g", "100 g").replace("100mg", "100 mg"))
    if m:
        den = re.sub(r"\s+", " ", m.group("den"))
        return {"class": "mass_fraction", "unit_clean": u, "num": m.group("num"), "den": den}
    return {"class": "unknown", "unit_clean": u, "reason": f"unrecognized unit '{unit}'"}


def to_mg_per_100g(value: float, info: dict) -> float | None:
    if info.get("class") != "mass_fraction":
        return None
    return value * MASS_TO_MG[info["num"]] * (100.0 / DEN_TO_G[info["den"]])


def normalize(value: float | None, unit: str | None) -> tuple[float | None, str | None]:
    """(value_in_mg_per_100g, 'mg/100 g') for mass fractions only; otherwise (None, None)."""
    if value is None:
        return None, None
    v = to_mg_per_100g(value, classify(unit))
    return (v, NORMALIZED_UNIT) if v is not None else (None, None)


MAX_MG_PER_100G = 100_000.0  # 100 g per 100 g: nothing can exceed its own mass
