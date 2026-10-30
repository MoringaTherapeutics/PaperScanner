"""Test doubles: an OpenAlex client serving cursor pages, and a scripted LLM."""
import json

import requests


def make_work(i: int, doi: str | None = None, title: str | None = None) -> dict:
    return {
        "id": f"https://openalex.org/W{1000 + i}",
        "doi": f"https://doi.org/{doi or f'10.1000/test.{i}'}",
        "title": title or f"Chemical composition of turmeric rhizome extracts {i}",
        "publication_year": 2020,
        "type": "article",
        "authorships": [{"author": {"display_name": "A. Author"}}],
        "primary_location": {"source": {"display_name": "Food Chemistry"}},
        "locations": [{"is_oa": True, "pdf_url": f"https://example.org/{i}.pdf", "landing_page_url": None,
                       "version": "publishedVersion", "source": {"display_name": "Publisher"}}],
        "open_access": {"is_oa": True},
        "ids": {"pmid": f"https://pubmed.ncbi.nlm.nih.gov/{i}"},
        "abstract_inverted_index": {"Turmeric": [0], "extracts": [1], "analysed": [2]},
        "referenced_works": [],
        "cited_by_api_url": "x",
        "is_retracted": False,
    }


class FakeOpenAlex:
    """Serves `works` in pages of `page_size`; cursor tokens are 'p<N>'. Optionally crashes once."""

    def __init__(self, works, page_size=2, crash_on_call=None):
        self.works, self.page_size, self.crash_on_call = works, page_size, crash_on_call
        self.calls, self.retries = 0, 0

    def get(self, params):
        self.calls += 1
        if self.crash_on_call == self.calls:
            raise requests.ConnectionError("simulated network failure")
        if "cursor" not in params:  # count-only request
            return {"meta": {"count": len(self.works)}, "results": []}
        cur = params["cursor"]
        start = 0 if cur == "*" else int(cur[1:]) * self.page_size
        chunk = self.works[start:start + self.page_size]
        more = start + self.page_size < len(self.works)
        nxt = f"p{start // self.page_size + 1}" if more else None
        return {"meta": {"count": len(self.works), "next_cursor": nxt}, "results": chunk}


class FakeLLM:
    """Scripted: screens by title keyword; extracts from a fixed JSON payload."""

    def __init__(self, extraction_payload: dict, max_calls=None):
        self.payload, self.calls, self.max_calls = extraction_payload, 0, max_calls

    def ask(self, system, user, model, max_tokens=4000):
        from src.llm import BudgetExceeded

        if self.max_calls is not None and self.calls >= self.max_calls:
            raise BudgetExceeded("budget")
        self.calls += 1
        if system.startswith("You screen"):
            ok = "composition" in user.lower()
            return json.dumps({"decision": "likely_eligible" if ok else "likely_ineligible", "reason": "kw"})
        if system.startswith("You extract"):
            return json.dumps(self.payload)
        if system.startswith("Assess"):
            return json.dumps({"domains": [{"domain": "Statistical analysis", "rating": "low concern", "evidence": "ANOVA"}]})
        raise AssertionError("unexpected prompt")


DOC = """[[page 1]] Turmeric rhizomes (Curcuma longa L.) were extracted with water, or with 50% ethanol.
[[page 4]] Table 2. Total phenolic content (mg GAE/g dry weight) Water extract 12.4 ± 0.8
50% ethanol extract 31.2 ± 1.1 Curcumin (mg/g extract) 54.3 ± 2.0 """ + ("filler text " * 400)


def extraction_payload():
    def ex(solvent, conc, measurements):
        return {"plant_part": "rhizome", "cultivar": "unknown", "origin": "unknown", "sample_state": "dried",
                "preparation": "ground", "condition": "", "extraction_yield": "unknown",
                "biological_replicates": "3", "technical_replicates": "unknown", "method_locator": "p.1",
                "protocol": {"technique": "maceration", "solvent": solvent, "solvent_concentration": conc,
                             "solid_liquid_ratio": "unknown", "temperature": "unknown", "duration": "unknown",
                             "cycles": "unknown", "pressure": "unknown", "sequential_order": None},
                "measurements": measurements}

    def m(analyte, cat, val, unit, quote, basis="dry", loc="Table 2"):
        return {"analyte": analyte, "category": cat, "value_original": val, "unit": unit, "basis": basis,
                "uncertainty_type": "SD", "uncertainty_value": "", "n": 3, "method": "Folin-Ciocalteu",
                "calibration_standard": "gallic acid", "lod": "", "loq": "", "locator": loc, "source_quote": quote}

    return {"eligible": True, "reason": "original data", "extracts": [
        ex("water", "100%", [m("Total phenolic content", "polyphenols", "12.4 ± 0.8", "mg GAE/g dry weight",
                               "Water extract 12.4 ± 0.8")]),
        ex("ethanol", "50%", [
            m("Total phenolic content", "polyphenols", "31.2 ± 1.1", "mg GAE/g dry weight", "50% ethanol extract 31.2 ± 1.1"),
            m("Curcumin", "bioactives", "54.3", "mg/g extract", "Curcumin (mg/g extract) 54.3 ± 2.0", basis="extract"),
            m("Tannins", "tannins", "9.9", "mg/g", "Tannins 9.9 mg/g"),   # quote is NOT in the paper -> quarantine
        ]),
    ]}
