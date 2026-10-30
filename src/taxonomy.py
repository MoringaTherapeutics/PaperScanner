"""Species registry loading and the search-query matrix (taxonomic x chemical x method x assay)."""
from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path

CHEMICAL = [
    "macronutrient", "proximate", "mineral", "trace element", "vitamin", "polyphenol", "tannin",
    "flavonoid", "phenolic acid", "terpenoid", "alkaloid", "glycoside", "carotenoid", "phytosterol",
    "fatty acid", "volatile compound", "essential oil", "antinutrient", "amino acid", "anthocyanin",
]
METHODOLOGICAL = [
    "chemical composition", "phytochemical profiling", "metabolomics", "proximate analysis",
    "chromatography", "HPLC", "UHPLC", "LC-MS/MS", "GC-MS", "ICP-MS", "ICP-OES", "AAS",
    "spectrophotometry", "solvent extraction", "Soxhlet", "maceration", "ultrasound-assisted extraction",
    "microwave-assisted extraction", "supercritical fluid extraction", "pressurized liquid extraction",
    "deep eutectic solvent", "aqueous extraction", "food processing",
]
ASSAYS = [
    "DPPH", "ABTS", "FRAP", "ORAC", "CUPRAC", "total phenolic content", "total flavonoid content",
    "tannin assay", "lipid oxidation", "peroxide value",
]
FAMILIES = {"chemical": CHEMICAL, "methodological": METHODOLOGICAL, "assay": ASSAYS}
CHUNK = 10  # terms per query keeps URLs short and results interpretable


@dataclass(frozen=True)
class Species:
    species_id: str
    accepted_name: str
    authority: str
    priority: int
    synonyms: tuple[str, ...]
    common_names: tuple[str, ...]

    @property
    def all_names(self) -> list[str]:
        seen, out = set(), []
        for n in [self.accepted_name, *self.synonyms, *self.common_names]:
            n = n.split(" (disputed)")[0].strip()
            if n and n.lower() not in seen:
                seen.add(n.lower())
                out.append(n)
        return out


def load_registry(path: str | Path) -> list[Species]:
    out = []
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            out.append(
                Species(
                    r["species_id"], r["accepted_name"], r["authority"], int(r["priority"]),
                    tuple(x.strip() for x in r["synonyms"].split(";") if x.strip()),
                    tuple(x.strip() for x in r["common_names"].split(";") if x.strip()),
                )
            )
    return sorted(out, key=lambda s: s.priority)


def _quote(term: str) -> str:
    return '"' + term.replace('"', "") + '"'


def _or(terms: list[str]) -> str:
    return "(" + " OR ".join(_quote(t) for t in terms) + ")"


def query_hash(species_id: str, query: str, filters: str = "") -> str:
    return hashlib.sha1(f"{species_id}|{query}|{filters}".encode()).hexdigest()[:16]


def build_queries(sp: Species) -> list[dict]:
    """Return the planned search matrix for a species, narrowest/highest-yield first.

    Each item: {family, query, hash}. The taxonomic-only query comes last because it is
    the broadest; it is always run with year partitioning (see openalex_search.plan_filters).
    """
    names = _or(sp.all_names)
    plan = []
    for family, terms in FAMILIES.items():
        for i in range(0, len(terms), CHUNK):
            q = f"{names} AND {_or(terms[i:i + CHUNK])}"
            plan.append({"family": family, "query": q, "hash": query_hash(sp.species_id, q)})
    q = names
    plan.append({"family": "taxonomic", "query": q, "hash": query_hash(sp.species_id, q)})
    return plan
