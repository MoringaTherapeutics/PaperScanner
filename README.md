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
