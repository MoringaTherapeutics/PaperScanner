# Global plant chemistry scanner

OpenAlex discovery → screening → open-access full text → model-assisted extraction with deterministic
provenance checks → PostgreSQL → one-way sync to a Google Sheet. Built to the "Revised Build Spec".

## What is verified and what is not

| Part | Status |
| --- | --- |
| Schema (2 migrations), upserts, checkpoint resume, crash recovery, reconciliation, DOI dedupe | Tested against a real PostgreSQL 16 (42 tests) |
| Quote-and-number provenance check, quarantine, unit/basis/plausibility rules | Tested with synthetic papers |
| Workbook import: 31 studies, 77 values → 29 staged unverified, 48 quarantined, 0 verified | Tested and dry-run |
| Query syntax | Checked in OpenAlex (turmeric chemical chunk 1 returns 6,756 works) |
| Live OpenAlex API, live model calls, real PDFs, real Google Sheet, Actions run | **Not run.** The build sandbox cannot reach those services. |
| Extraction accuracy on real papers | **Unmeasured.** This is the validation gate below. |

## Private inputs (not in this repo)

`data/workbook_export.txt` and `config/workbook_audit_*.json` hold the legacy workbook and its audit. They are
git-ignored on purpose; `import-workbook` and the 4 import tests need them locally and skip without them.

## Setup

1. Create a **new** Supabase project (not `moringa-prod`). Copy the **connection pooler** string (IPv4).
2. GitHub → Settings → Secrets and variables → Actions. Secrets: `OPENALEX_API_KEY`, `ANTHROPIC_API_KEY`,
   `DATABASE_URL`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `SPREADSHEET_ID`. Variables (optional): `CONTACT_EMAIL`,
   `MAX_LLM_CALLS` (default 150), `PAGE_BUDGET`, `SCREEN_LIMIT`, `EXTRACT_LIMIT`.
3. Share the workbook with the service account (Editor).
4. Locally, with the same variables exported:

```
pip install -r requirements.txt
python main.py import-workbook --dry-run     # inspect data/import_dry_run/*.csv first
python main.py migrate
python main.py import-workbook               # legacy rows enter as unverified / quarantined
python main.py load-species
python main.py scan --species SP_CURCUMA_LONGA --pages 5 --screen 20 --extract 3
```

5. Run the **Daily Plant Research Scan** workflow manually (`workflow_dispatch`) and read the report.
   Only then rely on the schedule.

## Validation gate (set before the first real run)

| Test | Pass line |
| --- | --- |
| Count reconciliation: reported = retrieved + failed, every query | 100% |
| Sentinel recall: `python main.py audit --species ...` (`config/sentinels.json`) | at least 90% |
| Value accuracy: hand-check every value from 20 full-text papers against the source | at least 95% match, no unit/basis errors |
| Kill a run mid-batch, resume, re-run | zero duplicates or lost records |

Do not add species until this passes. Species status never becomes "Complete" automatically; the audit
leaves human-only checks (supplementary data, language limits, citation expansion) as `null`.

## Rules the code enforces

* A value is `Y` only if its verbatim quote is found in the retrieved text **and** the number is inside that
  quote **and** locator, basis, unit and plausibility checks pass. Ambiguous values stay `U` for review;
  failures go to `quarantine`.
* Original values are never overwritten; human verdicts (`Review_Verdicts` tab) set status only.
* The sheet sync writes only `DB_*` tabs. Legacy tabs are never touched.
* Fresh/dry/extract bases are never converted; `value_normalized` is a separate, empty-by-default column.
* A hard LLM-call budget (`MAX_LLM_CALLS`) stops the run cleanly and keeps checkpoints.

## Known gaps

* Citation-network expansion (referenced/citing works) is not automated; the audit reports it as `null`.
* Scanned PDFs without a text layer are logged as failed full text (no OCR).
* Tables that PDF text extraction flattens badly may produce review items rather than verified values.
* Supplementary datasets are not fetched.


## Read-only publication release audit

Run `python main.py validate --species SP_CURCUMA_LONGA` after migration. Exit code
`2` means publication release remains blocked; `0` is reserved for a future
completed, independently documented release certificate. The command never
modifies data.

`src/release_gate.py` adds two distinct gates:

1. **Measurement gate:** a source quote and numerical check alone do not
   establish analytical reliability. A release-eligible record needs independent
   human verification, a source locator and quote, original numerical value,
   recognized basis/unit, method, complete 15-domain quality assessment, and
   no unresolved core quality concern.
2. **Corpus/pilot gate:** every search snapshot must reconcile, at least 20
   full-text papers must be manually reviewed, at least 95% of hand-checked
   measurements must match the originals, there must be zero unit/basis
   errors and unresolved duplicate groups, and the coverage limitations must
   have explicit sign-off. The helper accepts measured counts, not guesses.

**Current status:** the live database and full-text pilot have not been
independently executed or certified by this GitHub update. `validate` will
therefore report `publication_ready: false`, even if some individual
measurements are eligible. GitHub CI tests deterministic synthetic cases;
it cannot independently verify the underlying papers.

### Existing historical turmeric backlog

The prior research workbook contains incomplete OpenAlex discovery paging
(including pages 1–2 missing from the search-hit ledger for the primary
turmeric query) and four suspected duplicate-report pairs. These are
**open review tasks**, not excluded studies or confirmed duplicates.
Do not mark them resolved without fetching the source, matching versions,
checking independent experimental units, and recording a reviewer/verdict.

Do **not** import legacy sheets straight into a production 'verified' table.
Use `import-workbook --dry-run`, then curate the quarantined records and
run the pilot. Leave originals and all user-edited tabs untouched.

### Explicit reviewer checklist

- Resolve DOI/PMID/PMCID and verify study title, year, type, and corrections.
- Verify original species/taxon, part, preparation and independent samples.
- Record each solvent mixture/concentration, extraction temperature/time,
  and sequential fraction as its own experimental unit.
- Match *every* reported value to its page/table and source quote; preserve
  uncertainty, sample basis and original unit. Verify conversion by hand.
- Record whether standards, calibration, reference material, blanks,
  recovery, detection/quantification limits, biological/technical replicates,
  and statistical design were actually reported; missing is **unknown**,
  never zero or successful.
- Confirm potential duplicate reports and preprint/publisher versions without
  discarding distinct experiments.
- Reconcile every cursor/page, archived query/filter and count before
  claiming completion within a stated source/date scope.
- Require explicit human authorization for a research-ready release; never
  equate in-vitro assay activity with therapeutic efficacy.
