"""Run our RawTree SQL offline: an embedded ClickHouse (chdb) loaded with a Parquet dump.

RawTree is a ClickHouse fork, so the same files in rtrec/sql/ run here unchanged. What this does
NOT reproduce: RawTree's Dynamic columns (here columns have plain types, so missing casts would
pass locally and fail live), its limits and its rate limiting. Optional dependency: `pip install chdb`.

    shop = LocalShop("data")                 # catalog.parquet, history_sessions/, history_events/
    shop.add_table("catalog_updates", df)   # optional live tables from the simulator
    shop.query(render("position_bias", since=..., until=...))  -> list[dict]
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd


class LocalShop:
    def __init__(self, data_dir: str | Path | None = None):
        import chdb.session
        self._tmp = tempfile.TemporaryDirectory()
        self.s = chdb.session.Session(self._tmp.name)
        self.s.query("CREATE DATABASE IF NOT EXISTS shop")
        if data_dir is not None:
            d = Path(data_dir)
            self._load("catalog", str(d / "catalog.parquet"))
            for t in ("history_sessions", "history_events"):
                if (d / t).exists():
                    self._load(t, str(d / t / "*.parquet"))

    def _load(self, name: str, glob: str) -> None:
        cols = self.s.query(f"DESCRIBE file('{glob}', Parquet)", "DataFrame")["name"].tolist()
        conv = []
        for c in ("ts", "created_at"):
            if c in cols:
                conv.append(f"parseDateTime64BestEffort(toString({c}), 3, 'UTC') AS {c}")
        sel = f"* REPLACE ({', '.join(conv)})" if conv else "*"
        self.s.query(f"DROP TABLE IF EXISTS shop.{name}")
        self.s.query(f"CREATE TABLE shop.{name} ENGINE = MergeTree ORDER BY tuple() AS "
                     f"SELECT {sel} FROM file('{glob}', Parquet)")

    def add_table(self, name: str, df: pd.DataFrame) -> None:
        path = Path(self._tmp.name) / f"{name}.parquet"
        df.to_parquet(path, index=False)
        self._load(name, str(path))

    def query(self, sql: str) -> list[dict]:
        return self.query_df(sql).to_dict("records")

    def query_df(self, sql: str) -> pd.DataFrame:
        return self.s.query(sql, "DataFrame")
