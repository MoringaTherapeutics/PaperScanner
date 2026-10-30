import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def conn():
    """Fresh schema per test. Needs TEST_DATABASE_URL; skipped otherwise."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    from src import database

    c = database.connect(url)
    c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    database.apply_migrations(c, ROOT / "migrations")
    c.commit()
    yield c
    c.rollback()
    c.close()


@pytest.fixture()
def registry():
    from src.taxonomy import load_registry

    return load_registry(ROOT / "config/species_registry.csv")


@pytest.fixture()
def seeded(conn, registry):
    from src import pipeline

    pipeline.load_species(conn, registry)
    return conn
