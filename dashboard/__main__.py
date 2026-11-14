"""Command line.

    python -m dashboard serve    [--store demo|fake|rawtree] [--port 8050] [--host 127.0.0.1] [--chaos]
    python -m dashboard serve --store fake --with-runtime [--policy module:factory]
                                 runs A1's serving loop on the FakeStore in this process and watches it
                                 (the FakeStore is in memory: a separate process would see nothing)
    python -m dashboard check    [--store ...] [--warmup 3]      one line, exit 0 ok / 1 warning / 2 critical
    python -m dashboard sqlcheck [--store rawtree]               run every SQL twin once, report errors
"""
from __future__ import annotations

import argparse
import importlib
import sys
import threading
import time

from . import alerts
from .collector import Collector
from .server import full_snapshot, start_in_background
from .stores import default_kind, open_store


def start_runtime(store, policy_spec: str = "", state_dir: str = "artifacts/dashboard-runtime"):
    """A1's Server in a daemon thread, on the same store object as the dashboard (demo of the full loop)."""
    from rtrec.runtime.serve import ServeConfig, Server
    factory = None
    if policy_spec:
        mod, _, name = policy_spec.partition(":")
        obj = getattr(importlib.import_module(mod), name)
        factory = obj if callable(obj) and not hasattr(obj, "recommend") else (lambda: obj)
    server = Server(store, policy_factory=factory, config=ServeConfig(role="primary", state_dir=state_dir))
    threading.Thread(target=server.run, daemon=True, name="runtime").start()
    return server


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m dashboard")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "check", "sqlcheck"):
        p = sub.add_parser(name)
        p.add_argument("--store", default=default_kind(), help="demo | fake | rawtree (default $RTREC_STORE)")
        p.add_argument("--chaos", action="store_true", help="demo store: 75 s outage every 20 min")
        if name == "serve":
            p.add_argument("--host", default="127.0.0.1")
            p.add_argument("--port", type=int, default=8050)
            p.add_argument("--poll", type=float, default=1.5, help="seconds between store reads")
            p.add_argument("--with-runtime", action="store_true", help="fake store: also run the serving loop")
            p.add_argument("--policy", default="", help="module:factory for --with-runtime (default: built-in)")
        if name == "check":
            p.add_argument("--warmup", type=float, default=3.0, help="seconds to read before judging")
    a = ap.parse_args(argv)
    store = open_store(a.store, chaos=a.chaos)

    if a.cmd == "serve":
        if getattr(a, "with_runtime", False):
            start_runtime(store, a.policy)
        srv, col, _ = start_in_background(store, a.host, a.port, a.poll)
        print(f"dashboard on http://{a.host}:{a.port}  (store={a.store}, team={col.team}) — Ctrl+C to stop")
        try:
            while True:
                time.sleep(30)
                print(time.strftime("%H:%M:%S"), full_snapshot(col)["summary"], flush=True)
        except KeyboardInterrupt:
            srv.shutdown()
        return 0

    col = Collector(store, sql_every_s=0)
    if a.cmd == "sqlcheck":
        if not col.sql_capable():
            print(f"store {a.store} cannot run SQL ({col.errors.get('sql', 'NotImplemented')})")
            return 1
        col.run_sql()
        bad = 0
        for name, r in col.sql_results.items():
            ok = r["error"] is None
            bad += not ok
            print(f"{'OK ' if ok else 'ERR'} {name:<11} {r['ms'] or '-':>5} ms  "
                  f"{(r['rows'][:1] if ok else r['error'])}")
        return 1 if bad else 0

    end = time.time() + a.warmup
    while True:
        col.poll()
        if time.time() >= end:
            break
        time.sleep(1.0)
    snap = full_snapshot(col)
    al = alerts.evaluate(snap)
    print(snap["summary"])
    for x in al:
        if x.level != "info":
            print(f"  [{x.level}] {x.title} — {x.detail} (RUNBOOK #{x.runbook})")
    return alerts.LEVELS[alerts.overall(al)]


if __name__ == "__main__":
    sys.exit(main())
