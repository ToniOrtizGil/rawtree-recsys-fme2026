"""Tiny HTTP server for the dashboard: standard library only, one static page that polls a JSON endpoint.

    GET /                  the page (dashboard/static/index.html)
    GET /api/snapshot      all metrics + alerts as JSON   (?window=<minutes> for the conversion tables)
    GET /api/health        one line; HTTP 503 when an alert is critical (usable by any uptime monitor)
    GET /runbook           ops/RUNBOOK.md (plain text in a page whose section anchors the alert links use)

Binds to 127.0.0.1 by default: the page shows our team's data, do not expose it. A poller thread reads the
store every `poll_s`; page requests only read the in-memory snapshot, so many viewers cost nothing.
The serving runtime can also embed it in-process with start_in_background(store).
"""
from __future__ import annotations

import html
import json
import re
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


def runbook_page() -> str:
    """The runbook as escaped text, keeping only its `<a id="...">` anchors live (no markdown renderer)."""
    text = RUNBOOK.read_text(encoding="utf-8") if RUNBOOK.exists() else "ops/RUNBOOK.md not found"
    body = re.sub(r'&lt;a id=&quot;([\w-]+)&quot;&gt;&lt;/a&gt;', r'<a id="\1"></a>', html.escape(text))
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            "<title>Runbook</title><style>body{margin:0;padding:16px;font:14px/1.5 ui-monospace,monospace;"
            "background:#fcfcfb;color:#0b0b0b}@media(prefers-color-scheme:dark){body{background:#1a1a19;"
            "color:#f0efec}}pre{white-space:pre-wrap;max-width:110ch}</style><pre>" + body + "</pre>")


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
                self._send(200, runbook_page().encode(), "text/html; charset=utf-8")
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
    srv = make_server(col, host, port)          # listen first: the first live read takes ~10 s
    threading.Thread(target=srv.serve_forever, daemon=True, name="dash-http").start()
    stop = threading.Event()
    threading.Thread(target=poll_forever, args=(col, poll_s, stop), daemon=True, name="dash-poll").start()
    return srv, col, stop
