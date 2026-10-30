"""Stage 1 (title/abstract) screening. Results are PROVISIONAL, never confirmed exclusions."""
from __future__ import annotations

import os
import re

from .database import fetch_all, upsert
from .llm import LLM, extract_json

SCREEN_MODEL = os.environ.get("SCREEN_MODEL", "claude-haiku-5-5")
SYSTEM = (
    "You screen scientific records for a database of ORIGINAL chemical-composition measurements of "
    "edible plants. Eligible: original experimental studies that measure chemical composition "
    "(proximate, minerals, polyphenols, tannins, bioactives, volatiles, fatty acids, antioxidant "
    "assays, extraction comparisons, processing effects) on an identified plant material or extract. "
    "Ineligible: reviews and meta-analyses (secondary), purely clinical or pharmacological studies "
    "without composition measurements, papers where the plant is incidental. If the abstract is "
    "missing or ambiguous answer 'uncertain'. Reply with JSON only: "
    '{"decision": "likely_eligible"|"likely_ineligible"|"uncertain", "reason": "<one sentence, at most 25 words>"}'
)
VALID = {"likely_eligible", "likely_ineligible", "uncertain"}


_DECISION_RE = re.compile(r'"decision"\s*:\s*"(likely_eligible|likely_ineligible|uncertain)"')
_REASON_RE = re.compile(r'"reason"\s*:\s*"([^"]*)')


def _decide(reply: str) -> dict | None:
    data = extract_json(reply)
    if data and data.get("decision") in VALID:
        return {"decision": data["decision"], "reason": str(data.get("reason", ""))[:500]}
    m = _DECISION_RE.search(reply)  # salvage a truncated or slightly malformed reply
    if m:
        r = _REASON_RE.search(reply)
        return {"decision": m.group(1), "reason": ((r.group(1) if r else "") + " [recovered from malformed reply]")[:500]}
    return None


def classify(llm: LLM, species_name: str, title: str, abstract: str, model: str = SCREEN_MODEL) -> dict:
    user = f"Target species: {species_name}\nTitle: {title}\nAbstract: {abstract or '(none available)'}"
    reply = llm.ask(SYSTEM, user, model, max_tokens=600)
    res = _decide(reply)
    if res is None:  # one retry with a stricter reminder
        reply = llm.ask(SYSTEM + " Output ONLY the JSON object, nothing else.", user, model, max_tokens=600)
        res = _decide(reply)
    return res or {"decision": "uncertain", "reason": ("unparseable reply: " + reply.strip().replace("\n", " "))[:300]}


def pending_stage1(conn, species_id: str, limit: int) -> list[dict]:
    return fetch_all(
        conn,
        "SELECT p.publication_id, p.title, p.raw->>'abstract' AS abstract FROM species_publications sp "
        "JOIN publications p USING (publication_id) "
        "WHERE sp.species_id=%s AND p.duplicate_of IS NULL AND NOT EXISTS ("
        "  SELECT 1 FROM screening s WHERE s.publication_id=p.publication_id "
        "  AND s.species_id=sp.species_id AND s.stage=1) "
        "ORDER BY (SELECT min(r.rank) FROM search_results r WHERE r.publication_id=p.publication_id), "
        "p.publication_id LIMIT %s",
        [species_id, limit],
    )


def run_stage1(conn, llm: LLM, species_id: str, species_name: str, limit: int, model: str = SCREEN_MODEL) -> dict:
    done = {"likely_eligible": 0, "likely_ineligible": 0, "uncertain": 0}
    for pub in pending_stage1(conn, species_id, limit):
        res = classify(llm, species_name, pub["title"], pub["abstract"] or "", model)
        upsert(conn, "screening", {
            "publication_id": pub["publication_id"], "species_id": species_id, "stage": 1,
            "decision": res["decision"], "reason": res["reason"], "reviewer": model,
        }, ["publication_id", "species_id", "stage", "reviewer"])
        conn.commit()
        done[res["decision"]] += 1
    return done
