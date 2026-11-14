"""Feature queries in RawTree SQL (ClickHouse dialect), one file per feature.

    from rtrec.sql import render
    sql = render("popularity_by_context", table="history_events", since="2026-09-15", until="2026-11-14")
    rows = store.query(sql)                      # RawTreeStore (live) ...
    rows = rtrec.sql.local.LocalShop(dir).query(sql)   # ... or chdb on a Parquet dump (offline)

Every file has a pandas twin in rtrec/data/features.py with the same name and output columns;
tests/test_data_sql.py checks they agree on the synthetic dump. Parameters are `$name`
(string.Template), always our own values, never user input.
RawTree quirks handled here: every column is Dynamic, so join keys, ARRAY JOIN arrays and
argMax arguments are cast (`::String`, `::Array(String)`, `::DateTime64(3)`).
"""
from __future__ import annotations

from pathlib import Path
from string import Template

DIR = Path(__file__).parent


def names() -> list[str]:
    return sorted(p.stem for p in DIR.glob("*.sql"))


def render(name: str, **params) -> str:
    text = (DIR / f"{name}.sql").read_text()
    return Template(text).substitute(**{k: str(v) for k, v in params.items()})
