"""Tiny HTTP server for the dashboard: standard library only, one static page that polls a JSON endpoint.

    GET /                  the page (dashboard/static/index.html)
    GET /api/snapshot      all metrics + alerts as JSON   (?window=<minutes> for the conversion tables)
    GET /api/health        one line; HTTP 503 when an alert is critical (usable by any uptime monitor)
    GET /runbook           ops/RUNBOOK.md as plain text (alert links point into it)

Binds to 127.0.0.1 by default: the page shows our team's data, do not expose it. A poller thread reads the
store every `poll_s`; page requests only read the in-memory snapshot, so many viewers cost nothing.
The serving runtime can also embed it in-process with start_in_background(store).
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import alerts
from .collector import Collector

STATIC = Path(__file__).parent / "static"
RUNBOOK = Path(__file__).resolve().parents[1] / "ops" / "RUNBOOK.md"


def full_snapshot(col: Collector, window_min: float = 60) -> dict:
    snap = col.snapshot(window_s=60 * window_min)
    al = alerts.evaluate(snap)
    snap["alerts"] = alerts.as_dicts(al)
    snap["status"] = alerts.overall(al)
    snap["summary"] = alerts.one_line(snap, al)
    return snap


class _Handler(BaseHTTPRequestHandler):
    col: Collector  # set on the subclass made in make_server

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            if url.path in ("/", "/index.html"):
                self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/snapshot":
                window = float(q.get("window", ["60"])[0])
                body = json.dumps(full_snapshot(self.col, window), default=str).encode()
                self._send(200, body, "application/json")
            elif url.path == "/api/health":
                snap = full_snapshot(self.col, 60)
                code = 503 if snap["status"] == "critical" else 200
                self._send(code, (snap["summary"] + "\n").encode(), "text/plain; charset=utf-8")
            elif url.path == "/runbook":
                text = RUNBOOK.read_bytes() if RUNBOOK.exists() else b"ops/RUNBOOK.md not found"
                self._send(200, text, "text/plain; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as e:  # noqa: BLE001 - never kill the server thread
            self._send(500, f"{type(e).__name__}: {e}".encode(), "text/plain")


def make_server(col: Collector, host: str = "127.0.0.1", port: int = 8050) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"col": col})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def poll_forever(col: Collector, poll_s: float, stop: threading.Event) -> None:
    while not stop.is_set():
        t0 = time.monotonic()
        col.poll()
        stop.wait(max(0.1, poll_s - (time.monotonic() - t0)))


def start_in_background(store, host: str = "127.0.0.1", port: int = 8050, poll_s: float = 1.5):
    """Run collector + server in daemon threads. Returns (server, collector, stop_event)."""
    col = Collector(store)
    col.poll()
    stop = threading.Event()
    threading.Thread(target=poll_forever, args=(col, poll_s, stop), daemon=True, name="dash-poll").start()
    srv = make_server(col, host, port)
    threading.Thread(target=srv.serve_forever, daemon=True, name="dash-http").start()
    return srv, col, stop
