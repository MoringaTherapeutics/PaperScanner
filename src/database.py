"""Thin PostgreSQL helpers. All writes are idempotent upserts."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Sequence


def connect(url: str | None = None):
    import psycopg  # imported lazily so pure-logic tests need no driver

    url = url or os.environ["DATABASE_URL"]
    return psycopg.connect(url, autocommit=False, prepare_threshold=None)  # pgbouncer (Supabase pooler) has no prepared statements


def apply_migrations(conn, directory: str | Path) -> list[str]:
    """Apply *.sql files in name order. Each file is written to be re-runnable."""
    applied = []
    for path in sorted(Path(directory).glob("*.sql")):
        with conn.transaction():
            conn.execute(path.read_text())
        applied.append(path.name)
    return applied


# Columns stored as jsonb. Any other list (e.g. quarantine.reasons) is a native Postgres array.
JSONB_COLS = {"authors", "oa_urls", "raw", "payload", "checks", "report"}


def _adapt(col: str, value: Any):
    from psycopg.types.json import Jsonb

    if col in JSONB_COLS and isinstance(value, (dict, list)):
        return Jsonb(value)
    if isinstance(value, dict):
        raise TypeError(f"dict value for non-jsonb column {col!r}")
    return value


def upsert(
    conn,
    table: str,
    row: dict[str, Any],
    conflict_cols: Sequence[str],
    update_cols: Iterable[str] | None = None,
) -> None:
    """INSERT ... ON CONFLICT (...) DO UPDATE. update_cols=() means DO NOTHING."""
    cols = list(row)
    placeholders = ", ".join(["%s"] * len(cols))
    if update_cols is None:
        update_cols = [c for c in cols if c not in conflict_cols]
    update_cols = list(update_cols)
    if update_cols:
        sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
        action = f"DO UPDATE SET {sets}"
    else:
        action = "DO NOTHING"
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(conflict_cols)}) {action}"
    )
    conn.execute(sql, [_adapt(c, row[c]) for c in cols])


def fetch_all(conn, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    cur = conn.execute(sql, params)
    names = [d.name for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]
