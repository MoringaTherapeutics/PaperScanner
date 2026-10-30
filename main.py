"""Command line entry point.

  python main.py migrate
  python main.py import-workbook [--dry-run] [--out data/import_dry_run]
  python main.py load-species
  python main.py scan [--species SP_CURCUMA_LONGA] [--pages 25] [--screen 200] [--extract 10] [--no-sheets]
  python main.py audit --species SP_CURCUMA_LONGA
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    sub.add_parser("load-species")
    iw = sub.add_parser("import-workbook")
    iw.add_argument("--dry-run", action="store_true")
    iw.add_argument("--export", default=str(ROOT / "data/workbook_export.txt"))
    iw.add_argument("--audit", default=str(ROOT / "config/workbook_audit_2026-10-30.json"))
    iw.add_argument("--out", default=str(ROOT / "data/import_dry_run"))
    sc = sub.add_parser("scan")
    sc.add_argument("--species")
    sc.add_argument("--pages", type=int, default=int(os.environ.get("PAGE_BUDGET", 25)))
    sc.add_argument("--screen", type=int, default=int(os.environ.get("SCREEN_LIMIT", 200)))
    sc.add_argument("--extract", type=int, default=int(os.environ.get("EXTRACT_LIMIT", 10)))
    sc.add_argument("--no-sheets", action="store_true")
    sc.add_argument("--no-quality", action="store_true")
    au = sub.add_parser("audit")
    au.add_argument("--species", required=True)
    args = ap.parse_args(argv)

    from src import pipeline, workbook_import
    from src.taxonomy import load_registry

    if args.cmd == "import-workbook":
        plan = workbook_import.build_plan(Path(args.export).read_text(), json.load(open(args.audit)))
        if args.dry_run:
            workbook_import.write_dry_run(plan, args.out)
            print(json.dumps(plan.report(), indent=2))
            return 0

    from src import database

    conn = database.connect()
    registry = load_registry(ROOT / "config/species_registry.csv")
    if args.cmd == "migrate":
        print("applied:", database.apply_migrations(conn, ROOT / "migrations"))
        conn.commit()
    elif args.cmd == "load-species":
        pipeline.load_species(conn, registry)
        print(f"{len(registry)} species loaded")
    elif args.cmd == "import-workbook":
        pipeline.load_species(conn, registry)
        workbook_import.write_to_db(plan, conn)
        print(json.dumps(plan.report(), indent=2))
    elif args.cmd == "scan":
        from src.llm import LLM
        from src.openalex_search import OpenAlexClient

        oa = OpenAlexClient(os.environ.get("OPENALEX_API_KEY"))
        llm = LLM()
        wb = None
        if not args.no_sheets and os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
            from src import sheets_sync

            wb = sheets_sync.open_workbook()
        report = pipeline.run_scan(
            conn, oa, llm, registry, args.species, page_budget=args.pages, screen_limit=args.screen,
            extract_limit=args.extract, do_quality=not args.no_quality, sheets_wb=wb)
        print(json.dumps(report, indent=2, default=str))
        return 1 if report["errors"] else 0
    elif args.cmd == "audit":
        sp = next(s for s in registry if s.species_id == args.species)
        print(json.dumps(pipeline.coverage_audit(conn, sp, str(ROOT / "config/sentinels.json")), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
