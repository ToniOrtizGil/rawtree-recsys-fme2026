"""RawTreeStore: the Store protocol over api.rawtree.com.

Written from the documented API before we had a key; first checked live on 14-nov 12:24 UTC
(ops/smoke_rawtree.py: every reader and the scratch insert work on team_36; measured round trips
~230-370 ms per call from the cloud container). Also exercised against rtrec/stores/fake_http.py.

API used
  POST /v1/query                     {"sql": ..., "format": "JSON"}  -> {"data": [...], "hints": [...]}
  POST /v1/tables/{table}?database=  JSON array body                -> {"written_rows": "n"}
  Authorization: Bearer $RAWTREE_API_KEY; responses gzip-compressed (Accept-Encoding: gzip).

Design choices
  * one persistent requests.Session per thread (keep-alive; Session is not guaranteed thread-safe);
  * short timeouts: an insert that has not answered in ~1 s is retried by the writer, not awaited;
  * inserts carry insert_deduplication_token = sha1(rows) so a retried identical batch is a no-op;
    reads never retry here (the pollers just poll again);
  * 429 -> RateLimited(retry_after) so the writer can wait exactly as long as asked;
  * every column is Dynamic: SELECTs cast to concrete types; timestamps filtered via DateTime64(3);
  * readers tolerate both nested (context.device) and object-valued columns, and JSON-as-string.
SQL fragments that might need adjusting on day one are module constants (TS, the *_SQL strings).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from pathlib import Path

import requests

from rtrec.runtime.clock import to_sql_dt

from .fake import RateLimited, StoreError

log = logging.getLogger("rtrec.rawtree")

TS = "ts::DateTime64(3)"   # the organisers' own cast (docs/upstream/recommender.py)


def _after(cursor: float) -> str:
    return f"{TS} > toDateTime64('{to_sql_dt(max(cursor, 0.0))}', 3, 'UTC')"


REQUESTS_SQL = """
SELECT request_id::String AS request_id, toString({ts}) AS ts, user_id::String AS user_id,
       is_new_user, context.device::String AS device, context.region::String AS region,
       context.page::String AS page, recent_views
FROM shop.requests
WHERE {after}
ORDER BY {ts} ASC
LIMIT {limit}"""

EVENTS_SQL = """
SELECT toString({ts}) AS ts, event_type::String AS event_type, user_id::String AS user_id,
       session_id::String AS session_id, product_id::String AS product_id, price_eur::Float64 AS price_eur,
       device::String AS device, region::String AS region
FROM shop.events
WHERE {after}
ORDER BY {ts} ASC
LIMIT {limit}"""

PRODUCT_COLS = """product_id::String AS product_id, category::String AS category,
       subcategory::String AS subcategory, brand::String AS brand, price_eur::Float64 AS price_eur,
       description::String AS description, toString(created_at) AS created_at, is_active"""

CATALOG_SQL = f"SELECT {PRODUCT_COLS} FROM shop.catalog"

UPDATES_SQL = """
SELECT {cols}, toString({ts}) AS ts, change::String AS change
FROM shop.catalog_updates
WHERE {after}
ORDER BY {ts} ASC
LIMIT {limit}"""

FEEDBACK_SQL = """
SELECT request_id::String AS request_id, toString({ts}) AS ts, shown, served_by::String AS served_by,
       fallback_reason::String AS fallback_reason, latency_ms::Float64 AS latency_ms,
       clicked::String AS clicked, purchased::String AS purchased, revenue_eur::Float64 AS revenue_eur
FROM {team}.feedback
WHERE request_id::String != '__init__' AND {after}
ORDER BY {ts} ASC
LIMIT {limit}"""

SCRATCH_SQL = """
SELECT * FROM {team}.scratch
WHERE {kind_filter}{after}
ORDER BY {ts} DESC
LIMIT {limit}"""

# submissions have no ts in RawTree: select ours for the requests that arrived after the cursor
SUBMISSIONS_SQL = """
SELECT request_id::String AS request_id, items, model_version::String AS model_version
FROM {team}.submissions
WHERE request_id::String IN (SELECT request_id::String FROM shop.requests WHERE {after})
LIMIT {limit}"""

BEST_SELLERS_SQL = """
SELECT product_id::String AS product_id, count() AS n
FROM shop.history_events
WHERE event_type::String = 'purchase'
GROUP BY product_id
ORDER BY n DESC, product_id DESC"""

_IDENT = re.compile(r"^[A-Za-z0-9_]+$")


class RawTreeStore:
    def __init__(self, team: str, api_key: str, base_url: str = "https://api.rawtree.com", *,
                 overlap_requests: float = 3.0, overlap_rows: float = 5.0, overlap_feedback: float = 30.0,
                 read_timeout: float = 2.0, insert_timeout: float = 1.5, scratch_timeout: float = 2.0):
        if not _IDENT.match(team):
            raise ValueError(f"bad team id {team!r}")
        self.team = team
        self.base = base_url.rstrip("/")
        self._key = api_key
        self.overlap_requests = overlap_requests
        self.overlap_rows = overlap_rows
        self.overlap_feedback = overlap_feedback
        self.read_timeout = read_timeout
        self.insert_timeout = insert_timeout
        self.scratch_timeout = scratch_timeout
        self._local = threading.local()
        self.last_hints: list = []

    @classmethod
    def from_env(cls) -> "RawTreeStore":
        env = dict(os.environ)
        dotenv = Path(".env")
        if dotenv.exists():
            for line in dotenv.read_text().splitlines():
                k, _, v = line.partition("=")
                if k.strip() and not k.strip().startswith("#"):
                    env.setdefault(k.strip(), v.strip())
        return cls(env["TEAM_ID"], env.get("RAWTREE_API_KEY", "proxy-injected"),
                   env.get("RAWTREE_API_URL", "https://api.rawtree.com"))

    # ------------------------------------------------------------------ HTTP
    @property
    def _http(self) -> requests.Session:
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"Authorization": f"Bearer {self._key}", "Accept-Encoding": "gzip",
                              "User-Agent": "rtrec/0.1"})
            adapter = requests.adapters.HTTPAdapter(pool_connections=2, pool_maxsize=4, max_retries=0)
            s.mount("https://", adapter)
            s.mount("http://", adapter)
            self._local.s = s
        return s

    def _post(self, path: str, *, params=None, body=None, timeout: float) -> dict:
        try:
            r = self._http.post(self.base + path, params=params, data=json.dumps(body, separators=(",", ":")).encode(),
                                headers={"Content-Type": "application/json"}, timeout=(min(1.0, timeout), timeout))
        except requests.RequestException as e:
            raise StoreError(f"{path}: {type(e).__name__}: {e}") from e
        if r.status_code == 429:
            try:
                ra = float(r.headers.get("retry-after", "0.5"))
            except ValueError:
                ra = 0.5
            raise RateLimited(ra)
        if r.status_code >= 500 or r.status_code in (408, 409):
            raise StoreError(f"{path}: HTTP {r.status_code}: {r.text[:200]}")
        if r.status_code >= 400:
            raise QueryError(r.status_code, r.text[:500])
        try:
            return r.json()
        except ValueError as e:
            raise StoreError(f"{path}: non-JSON response") from e

    def query(self, sql: str, timeout: float | None = None) -> list[dict]:
        out = self._post("/v1/query", body={"sql": sql, "format": "JSON"}, timeout=timeout or self.read_timeout)
        self.last_hints = out.get("hints") or []
        return out.get("data") or []

    def _query_or_empty(self, sql: str, timeout: float | None = None) -> list[dict]:
        """Live tables are empty before the stream starts and fail with UNKNOWN_IDENTIFIER: treat as no rows."""
        try:
            return self.query(sql, timeout)
        except QueryError as e:
            # (verified live 14-nov: an empty raw table answers "Empty list of columns in projection")
            if "UNKNOWN_IDENTIFIER" in e.body or "Unknown expression identifier" in e.body \
                    or "UNKNOWN_TABLE" in e.body or "Empty list of columns" in e.body:
                return []
            raise

    def _insert(self, table: str, rows: list[dict], timeout: float, token: str | None = None) -> int:
        params = {"database": self.team}
        if token:
            params["insert_deduplication_token"] = token
        out = self._post(f"/v1/tables/{table}", params=params, body=rows, timeout=timeout)
        try:
            return int(out.get("written_rows", len(rows)))
        except (TypeError, ValueError):
            return len(rows)

    # ------------------------------------------------------------------ Store protocol: reads
    def catalog(self) -> list[dict]:
        return [_product(r) for r in self.query(CATALOG_SQL, timeout=30)]

    def requests_since(self, cursor: float, limit: int = 500) -> list[dict]:
        sql = REQUESTS_SQL.format(ts=TS, after=_after(cursor - self.overlap_requests), limit=int(limit))
        return [_request(r) for r in self._query_or_empty(sql)]

    def events_since(self, cursor: float, limit: int = 50_000) -> list[dict]:
        sql = EVENTS_SQL.format(ts=TS, after=_after(cursor - self.overlap_rows), limit=int(limit))
        return [_num(r, "price_eur") for r in self._query_or_empty(sql, timeout=10)]

    def catalog_updates_since(self, cursor: float, limit: int = 50_000) -> list[dict]:
        sql = UPDATES_SQL.format(cols=PRODUCT_COLS, ts=TS, after=_after(cursor - self.overlap_rows), limit=int(limit))
        return [{**_product(r), "ts": r.get("ts"), "change": r.get("change")} for r in self._query_or_empty(sql, 10)]

    def feedback_since(self, cursor: float, limit: int = 50_000) -> list[dict]:
        sql = FEEDBACK_SQL.format(team=self.team, ts=TS, after=_after(cursor - self.overlap_feedback),
                                  limit=int(limit))
        out = []
        for r in self._query_or_empty(sql, timeout=10):
            r["shown"] = _list(r.get("shown"))
            out.append(_num(r, "latency_ms", "revenue_eur"))
        return out

    def scratch_since(self, cursor: float, limit: int = 50_000, kind: str | None = None) -> list[dict]:
        if kind is not None and not _IDENT.match(kind):
            raise ValueError(f"bad scratch kind {kind!r}")
        kind_filter = f"kind::String = '{kind}' AND " if kind else ""
        sql = SCRATCH_SQL.format(team=self.team, kind_filter=kind_filter, after=_after(cursor - self.overlap_rows),
                                 ts=TS, limit=int(limit))
        return self._query_or_empty(sql, timeout=self.scratch_timeout)

    def submissions_since(self, cursor: float, limit: int = 50_000) -> list[dict]:
        sql = SUBMISSIONS_SQL.format(team=self.team, after=_after(cursor - self.overlap_requests), limit=int(limit))
        return [{**r, "items": [str(x) for x in _list(r.get("items"))]} for r in self._query_or_empty(sql, 10)]

    def best_sellers(self) -> list[str]:
        return [str(r["product_id"]) for r in self.query(BEST_SELLERS_SQL, timeout=30)]

    # ------------------------------------------------------------------ Store protocol: writes
    def insert_submissions(self, rows: list[dict]) -> None:
        clean = [{"request_id": r["request_id"], "items": list(r["items"]),
                  "model_version": r.get("model_version", "")} for r in rows]
        token = hashlib.sha1(json.dumps(clean, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self._insert("submissions", clean, timeout=self.insert_timeout, token=token)

    def insert_scratch(self, rows: list[dict]) -> None:
        self._insert("scratch", rows, timeout=self.scratch_timeout)


class QueryError(StoreError):
    """4xx other than 429: a bug in our SQL or request; retrying will not help."""

    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


# ---------------------------------------------------------------------- row normalisation
def _list(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    return list(v) if isinstance(v, (list, tuple)) else []


def _bool(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "t", "yes")
    return bool(v)


def _num(r: dict, *keys) -> dict:
    for k in keys:
        try:
            r[k] = float(r[k]) if r.get(k) not in (None, "") else 0.0
        except (TypeError, ValueError):
            r[k] = 0.0
    return r


def _product(r: dict) -> dict:
    return {"product_id": str(r.get("product_id")), "category": r.get("category"),
            "subcategory": r.get("subcategory"), "brand": r.get("brand"),
            "price_eur": float(r["price_eur"]) if r.get("price_eur") not in (None, "") else None,
            "description": r.get("description"), "created_at": r.get("created_at"),
            "is_active": _bool(r.get("is_active", True))}


def _request(r: dict) -> dict:
    ctx = r.get("context")
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except ValueError:
            ctx = None
    if not isinstance(ctx, dict):
        ctx = {"device": r.get("device"), "region": r.get("region"), "page": r.get("page")}
    views = []
    for v in _list(r.get("recent_views")):
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                continue
        if isinstance(v, dict) and v.get("product_id") is not None:
            views.append({"product_id": str(v["product_id"]), "ts": v.get("ts")})
    return {"request_id": str(r.get("request_id")), "ts": r.get("ts"), "user_id": str(r.get("user_id")),
            "is_new_user": _bool(r.get("is_new_user")), "context": ctx, "recent_views": views}
