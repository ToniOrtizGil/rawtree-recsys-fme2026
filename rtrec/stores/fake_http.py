"""A local HTTP stand-in for api.rawtree.com, backed by FakeStore.

Why: RawTreeStore cannot be tested live (no key, host blocked). This emulator lets the *real*
HTTP client code run end to end: request building, auth header, JSON parsing, timeouts, 429s,
dedup tokens, and process-level crash/restart tests (serve in one process, "organisers" in this one).

It does NOT execute SQL. It recognises the handful of queries RawTreeStore sends (by table name and
the toDateTime64 cursor literal) and answers them from FakeStore. Passing here proves our HTTP
plumbing, not that RawTree accepts our SQL: that needs ops/smoke_rawtree.py with a real key.

    python -m rtrec.stores.fake_http --port 8765 --rate-multiplier 1
    RAWTREE_API_URL=http://127.0.0.1:8765 TEAM_ID=team_00 RAWTREE_API_KEY=x RTREC_STORE=rawtree \
        python -m rtrec.runtime.serve
    curl -s http://127.0.0.1:8765/report      # what the organisers would score
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from rtrec.runtime.clock import parse_ts, to_sql_dt

from .fake import FakeStore, RateLimited, StoreError

_CURSOR = re.compile(r"toDateTime64\('([^']+)'")
_LIMIT = re.compile(r"LIMIT\s+(\d+)", re.I)
_KIND = re.compile(r"kind::String\s*=\s*'([A-Za-z0-9_]+)'")


def _sqlts(r: dict) -> dict:
    r = dict(r)
    if r.get("ts"):
        r["ts"] = to_sql_dt(parse_ts(r["ts"]))   # what toString(ts::DateTime64(3)) returns
    return r


class Emulator:
    def __init__(self, store: FakeStore, api_key: str | None = None):
        store.overlap = 0.0          # RawTreeStore applies its own overlap in the SQL cursor
        self.store = store
        self.api_key = api_key
        self.queries = 0

    def answer_query(self, sql: str) -> list[dict]:
        self.queries += 1
        m = _CURSOR.search(sql)
        cursor = parse_ts(m.group(1)) if m else 0.0
        lm = _LIMIT.search(sql)
        limit = int(lm.group(1)) if lm else 1_000_000
        s = self.store
        if ".submissions" in sql:     # before shop.requests: its subquery mentions that table
            return s.submissions_since(cursor, limit)
        if ".scratch" in sql:
            k = _KIND.search(sql)
            rows = s.scratch_since(cursor, kind=k.group(1) if k else None)
            return sorted(rows, key=lambda r: -parse_ts(r.get("ts")))[:limit]
        if "FROM shop.requests" in sql:
            out = []
            for r in s.requests_since(cursor, limit):
                r = _sqlts(r)
                ctx = r.pop("context", {}) or {}
                r.update(device=ctx.get("device"), region=ctx.get("region"), page=ctx.get("page"))
                out.append(r)
            return out
        if "FROM shop.events" in sql:
            return [_sqlts(r) for r in s.events_since(cursor, limit)]
        if "FROM shop.catalog_updates" in sql:
            return [_sqlts(r) for r in s.catalog_updates_since(cursor, limit)]
        if "FROM shop.catalog" in sql:
            return s.catalog()
        if "FROM shop.history_events" in sql:
            return [{"product_id": p, "n": str(1000 - i)} for i, p in enumerate(s.best_sellers())]
        if ".feedback" in sql:
            return [_sqlts(r) for r in s.feedback_since(cursor, limit)]
        raise ValueError("emulator does not understand this query")

    def insert(self, table: str, rows: list[dict], token: str | None) -> int:
        if table == "submissions":
            self.store.insert_submissions(rows, dedup_token=token)
        elif table == "scratch":
            self.store.insert_scratch(rows)
        else:
            raise ValueError(f"unknown table {table}")
        return len(rows)

    def handler(self):
        emu = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # quiet
                pass

            def setup(self):
                super().setup()
                # header and body go out in separate writes: without this, Nagle + delayed ACK add 40 ms
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            def _send(self, code: int, obj, headers: dict | None = None):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                url = urlparse(self.path)
                if url.path == "/report":
                    q = {k: float(v[0]) for k, v in parse_qs(url.query).items() if k in ("since", "until")}
                    return self._send(200, emu.store.report(**q), )
                self._send(404, {"error": "not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                if emu.api_key and self.headers.get("Authorization") != f"Bearer {emu.api_key}":
                    return self._send(401, {"error": "unauthorized"})
                url = urlparse(self.path)
                try:
                    body = json.loads(raw or b"null")
                    if url.path == "/v1/query":
                        data = emu.answer_query(body["sql"])
                        return self._send(200, {"meta": [], "data": data, "rows": len(data), "hints": []})
                    if url.path.startswith("/v1/tables/"):
                        q = parse_qs(url.query)
                        token = (q.get("insert_deduplication_token") or [None])[0]
                        rows = body if isinstance(body, list) else [body]
                        w = emu.insert(url.path.rsplit("/", 1)[-1], rows, token)
                        return self._send(200, {"written_rows": str(w)})
                    return self._send(404, {"error": "not found"})
                except RateLimited as e:
                    return self._send(429, {"error": "rate_limit_exceeded"}, {"retry-after": str(e.retry_after)})
                except StoreError as e:
                    return self._send(503, {"error": str(e)})
                except (ValueError, KeyError) as e:
                    return self._send(400, {"error": f"UNKNOWN_IDENTIFIER-like: {e}"})

        return H

    def serve(self, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
        srv = _QuietServer((host, port), self.handler())
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, name="fake-rawtree", daemon=True).start()
        return srv


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # clients that time out and hang up (our own short timeouts) are expected, not errors
        import sys
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            super().handle_error(request, client_address)


FAULT_PROFILES = {
    "none": {},
    "latency": dict(latency=0.08, latency_jitter=0.12, spike_prob=0.02, spike_s=1.0),
    "flaky": dict(latency=0.08, latency_jitter=0.12, spike_prob=0.02, spike_s=1.0, read_error=0.05,
                  insert_error=0.05, ambiguous_insert=0.02, rate_limit=0.01),
}


def main() -> None:
    import os

    from rtrec.runtime.clock import RealClock

    from . import make_simulator, warm_up
    from .fake import Faults
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--rate-multiplier", type=float, default=1.0)
    ap.add_argument("--sim", default=os.environ.get("RTREC_SIM", "shop"), choices=["shop", "stub"])
    ap.add_argument("--faults", default="none", choices=sorted(FAULT_PROFILES))
    ap.add_argument("--api-key", default=None)
    a = ap.parse_args()
    os.environ["RTREC_SIM"] = a.sim
    sim = make_simulator(seed=a.seed, rate_multiplier=a.rate_multiplier)
    warm_up(sim)
    store = FakeStore(sim, RealClock(), faults=Faults(seed=a.seed, **FAULT_PROFILES[a.faults]))
    srv = Emulator(store, a.api_key).serve(port=a.port)
    print(f"fake RawTree on http://127.0.0.1:{srv.server_address[1]}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print(json.dumps(store.report(), indent=2))


if __name__ == "__main__":
    main()
