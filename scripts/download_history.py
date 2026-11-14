"""Download the RawTree history to Parquet, in the layout sim/dump.py also writes:

    data/catalog.parquet
    data/history_sessions/<YYYY-MM-DD>.parquet
    data/history_events/<YYYY-MM-DD>.parquet
    data/SOURCE.json
    data/live/{events,catalog_updates,requests,feedback}/<YYYY-MM-DD>.parquet   (with --live)

    TEAM_ID=team_NN RAWTREE_API_KEY=rt_... python scripts/download_history.py --out data

Why it looks like this:
- `shop` is shared by every team and rate-limited, and results are capped at 100 MiB / 30 s, so we
  pull one day at a time (an hour at a time if a day is refused as too big / too slow), gzip'd.
- Rows are read from `__raw_data` (the original JSON of each row) so nested fields (context,
  recent_views, shown, propensity) come back exactly as documented, without Dynamic-type casts.
  If that column is unavailable, `--columns` switches to explicit casted columns.
- Resumable: a day whose Parquet file exists is skipped, so a crash or a 429 storm costs nothing.

STATUS: validated against api.rawtree.com on 2026-11-14 (full 60-day history downloaded); also
tested offline with a fake transport (tests/test_data_download.py).
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

API = "https://api.rawtree.com"
TABLES = ("history_sessions", "history_events")
LIVE_TABLES = ("events", "catalog_updates", "requests")   # --live: shop.* tables of the event, into data/live/

COLUMNS = {   # fallback when __raw_data is not readable: explicit casts (every column is Dynamic)
    "catalog": "product_id::String AS product_id, category::String AS category, subcategory::String AS subcategory, "
               "brand::String AS brand, price_eur::Float64 AS price_eur, description::String AS description, "
               "toString(created_at) AS created_at, is_active::Bool AS is_active",
    "history_sessions": "request_id::String AS request_id, toString(ts) AS ts, user_id::String AS user_id, "
                        "context, recent_views, shown, propensity, clicked, purchased, "
                        "revenue_eur::Float64 AS revenue_eur",
    "history_events": "toString(ts) AS ts, event_type::String AS event_type, user_id::String AS user_id, "
                      "session_id::String AS session_id, product_id::String AS product_id, "
                      "price_eur::Float64 AS price_eur, device::String AS device, region::String AS region",
}


class QueryError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status


def load_env(path: str = ".env") -> None:
    """Minimal .env reader (KEY=VALUE lines); real environment variables win."""
    p = Path(path)
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def http_query(key: str, base: str = API, max_tries: int = 6) -> Callable[[str], list[dict]]:
    """Returns query(sql) -> rows, over one persistent HTTPS session, with 429/5xx retries."""
    import requests
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {key}", "Accept-Encoding": "gzip", "Content-Type": "application/json"})

    def query(sql: str) -> list[dict]:
        for attempt in range(max_tries):
            r = s.post(f"{base}/v1/query", data=json.dumps({"sql": sql, "format": "JSONEachRow"}), timeout=60)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("retry-after", 2 ** attempt)))
                continue
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if r.status_code != 200:
                raise QueryError(r.status_code, r.text)
            body = r.content
            if body[:2] == b"\x1f\x8b":       # requests normally inflates gzip itself; be safe
                body = gzip.decompress(body)
            return [json.loads(line) for line in body.decode().splitlines() if line.strip()]
        raise QueryError(429, "gave up after retries")
    return query


# Columns every row of a table must have: __raw_data omits keys whose value was null (e.g. no click).
REQUIRED = {
    "history_sessions": ("request_id", "ts", "user_id", "context", "recent_views", "shown", "propensity",
                         "clicked", "purchased", "revenue_eur"),
    "history_events": ("ts", "event_type", "user_id", "session_id", "product_id", "price_eur", "device", "region"),
}


def iso_ms(v):
    """RawTree returns '2026-09-15 07:00:01.456000000'; we store '2026-09-15T07:00:01.456Z' (same as sim/),
    so string comparisons and day partitions behave identically on real and synthetic data."""
    if not isinstance(v, str) or len(v) < 19:
        return v
    if v.endswith("Z") and "T" in v:
        return v
    date, _, time_ = v.replace("T", " ").rstrip("Z").partition(" ")
    hms, _, frac = time_.partition(".")
    return f"{date}T{hms}.{(frac + '000')[:3]}Z"


def normalize(row: dict, table: str | None = None) -> dict:
    for k in ("ts", "created_at"):
        if k in row:
            row[k] = iso_ms(row[k])
    if isinstance(row.get("recent_views"), list):
        row["recent_views"] = [{**v, "ts": iso_ms(v.get("ts"))} for v in row["recent_views"]]
    for k in REQUIRED.get(table, ()):
        row.setdefault(k, None)
    return row


def _rows(raw: list[dict], use_raw: bool, table: str | None = None) -> list[dict]:
    out = []
    for r in raw:
        v = r["__raw_data"] if use_raw else r
        out.append(normalize(json.loads(v) if isinstance(v, str) else dict(v), table))
    return out


def _select(table: str, use_raw: bool) -> str:
    return "__raw_data" if use_raw else COLUMNS[table]


def _fmt(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S")


def fetch_window(query, table: str, t0: datetime, t1: datetime, use_raw: bool, db: str = "shop") -> list[dict]:
    sql = (f"SELECT {_select(table, use_raw)} FROM {db}.{table} "
           f"WHERE ts::DateTime64(3) >= toDateTime64('{_fmt(t0)}', 3, 'UTC') "
           f"AND ts::DateTime64(3) < toDateTime64('{_fmt(t1)}', 3, 'UTC')")
    try:
        return _rows(query(sql), use_raw, table)
    except QueryError as e:
        # result too big (400/413) or too slow (408): split the window in halves, down to 15 minutes
        if e.status in (400, 408, 413) and (t1 - t0) > timedelta(minutes=15):
            mid = t0 + (t1 - t0) / 2
            return fetch_window(query, table, t0, mid, use_raw, db) + fetch_window(query, table, mid, t1, use_raw, db)
        raise


def ts_range(query, table: str, db: str = "shop") -> tuple[datetime, datetime]:
    # feedback starts with a placeholder row (request_id '__init__') whose ts is far in the past
    where = " WHERE request_id::String != '__init__'" if table == "feedback" else ""
    r = query(f"SELECT toString(min(ts::DateTime64(3))) AS lo, toString(max(ts::DateTime64(3))) AS hi "
              f"FROM {db}.{table}{where}")[0]
    parse = lambda s: datetime.fromisoformat(str(s).replace("Z", "").replace("T", " ")[:23]).replace(tzinfo=timezone.utc)
    return parse(r["lo"]), parse(r["hi"])


def download_table(query, out: Path, table: str, use_raw: bool, db: str = "shop", log=print,
                   refresh_last: bool = False) -> int:
    """Day files for one table. With refresh_last the newest (still growing) day is re-downloaded."""
    d = out / table
    d.mkdir(parents=True, exist_ok=True)
    lo, hi = ts_range(query, table, db)
    day = lo.replace(hour=0, minute=0, second=0, microsecond=0)
    n = 0
    while day <= hi:
        f = d / f"{day:%Y-%m-%d}.parquet"
        last = day + timedelta(days=1) > hi
        if not f.exists() or (refresh_last and last):
            rows = fetch_window(query, table, day, day + timedelta(days=1), use_raw, db)
            if table == "feedback":
                rows = [r for r in rows if r.get("request_id") != "__init__"]
            tmp = f.with_suffix(".tmp")
            pd.DataFrame(rows).to_parquet(tmp, index=False)
            tmp.rename(f)            # atomic: a half-written day is never mistaken for a done one
            log(f"{db}.{table} {day:%Y-%m-%d}: {len(rows)} rows")
            n += len(rows)
        day += timedelta(days=1)
    return n


def download(query, out: Path, use_raw: bool = True, log=print) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    stats = {}
    cat = out / "catalog.parquet"
    if not cat.exists():
        rows = _rows(query(f"SELECT {_select('catalog', use_raw)} FROM shop.catalog"), use_raw)
        pd.DataFrame(rows).to_parquet(cat, index=False)
        log(f"catalog: {len(rows)} products")
    for table in TABLES:
        stats[table] = download_table(query, out, table, use_raw, log=log)
    (out / "SOURCE.json").write_text(json.dumps(
        {"source": "rawtree", "downloaded_at": datetime.now(timezone.utc).isoformat(), "raw_data": use_raw}, indent=2))
    return stats


def download_live(query, out: Path, team: str | None, use_raw: bool = True, log=print) -> dict:
    """Snapshot of the live tables (for forensics H6/H7/H10/H11), re-fetching the current day."""
    live = out / "live"
    stats = {t: download_table(query, live, t, use_raw, log=log, refresh_last=True) for t in LIVE_TABLES}
    if team:
        stats["feedback"] = download_table(query, live, "feedback", use_raw, db=team, log=log, refresh_last=True)
    return stats


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data")
    ap.add_argument("--columns", action="store_true", help="explicit casted columns instead of __raw_data")
    ap.add_argument("--base-url", default=API)
    ap.add_argument("--live", action="store_true", help="also snapshot shop.events/catalog_updates/requests and "
                                                         "<TEAM_ID>.feedback into <out>/live/")
    a = ap.parse_args(argv)
    load_env()
    key = os.environ.get("RAWTREE_API_KEY")
    if not key:
        sys.exit("RAWTREE_API_KEY is not set (put it in .env, never commit it)")
    q = http_query(key, a.base_url)
    stats = download(q, Path(a.out), use_raw=not a.columns)
    if a.live:
        stats.update(download_live(q, Path(a.out), os.environ.get("TEAM_ID"), use_raw=not a.columns))
    print("done:", stats)


if __name__ == "__main__":
    main()
