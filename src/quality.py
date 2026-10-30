"""Domain-wise methodological quality, with an explicit, documented aggregation rule."""
from __future__ import annotations

from .llm import LLM, extract_json

DOMAINS = [
    "Botanical identification and authentication", "Sample representativeness",
    "Sampling and storage conditions", "Extraction reproducibility",
    "Analytical method appropriateness", "Calibration and reference standards",
    "Instrument validation", "Recovery measurements", "Blanks and controls",
    "Detection and quantification limits", "Biological replication", "Technical replication",
    "Statistical analysis", "Reporting completeness", "Independence and conflicts of interest",
]
RATINGS = ["low concern", "some concerns", "high concern", "insufficient information"]
CORE = {
    "Botanical identification and authentication", "Analytical method appropriateness",
    "Calibration and reference standards", "Reporting completeness",
}

AGGREGATION_RULE = (
    "Overall analytical evidence strength (NOT clinical evidence): "
    "'Insufficient' if more than a third of all domains, or more than one core domain, are 'insufficient information'; "
    "else 'Low' if any core domain is 'high concern' or more than three domains are 'high concern'; "
    "else 'Moderate' if any domain is 'high concern' or at least five domains are 'some concerns'; "
    "else 'High'. Core domains: " + "; ".join(sorted(CORE)) + ". No score uses journal name or citation count."
)

SYSTEM = (
    "Assess the methodological quality of this analytical chemistry paper, one rating per domain. "
    f"Ratings: {', '.join(RATINGS)}. Base each rating on specific evidence in the text; if the text does "
    "not say, use 'insufficient information'. Never use journal prestige or citations. Do not equate in-vitro "
    "antioxidant activity with therapeutic effect. Reply JSON only: "
    '{"domains": [{"domain": str, "rating": str, "evidence": str}]}'
)


def aggregate(ratings: dict[str, str]) -> str:
    vals = list(ratings.values())
    n = len(DOMAINS)
    insuff = sum(r == "insufficient information" for r in vals)
    core_insuff = sum(ratings.get(d) == "insufficient information" for d in CORE)
    if insuff > n / 3 or core_insuff > 1:
        return "Insufficient"
    high = sum(r == "high concern" for r in vals)
    if any(ratings.get(d) == "high concern" for d in CORE) or high > 3:
        return "Low"
    if high >= 1 or sum(r == "some concerns" for r in vals) >= 5:
        return "Moderate"
    return "High"


def parse_assessment(data: dict | None) -> list[dict]:
    """Validate model output; any missing/invalid domain becomes 'insufficient information'."""
    got = {}
    for d in (data or {}).get("domains", []):
        if d.get("domain") in DOMAINS and d.get("rating") in RATINGS:
            got[d["domain"]] = {"domain": d["domain"], "rating": d["rating"], "evidence": str(d.get("evidence", ""))[:600]}
    return [got.get(dom) or {"domain": dom, "rating": "insufficient information", "evidence": "not assessed"}
            for dom in DOMAINS]


def assess(llm: LLM, text: str, model: str) -> list[dict]:
    reply = llm.ask(SYSTEM, text[:60_000], model, max_tokens=3000)
    return parse_assessment(extract_json(reply))
