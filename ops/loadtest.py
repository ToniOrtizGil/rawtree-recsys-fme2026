"""Load test of the production serving path at 10x the nominal request rate, in real time.

    python ops/loadtest.py                       # 3 scenarios x 120 s, writes ops/reports/loadtest.md
    python ops/loadtest.py --duration 30 --scenarios http

Everything runs locally against the simulated shop (FakeStore, optionally behind the HTTP emulator so
the real RawTreeStore client is in the loop). Network latency, latency spikes, errors and 429s are
injected. The numbers say how much margin the *loop* has; they are not measurements of the live
platform (see the feedback `latency_ms` once we have a key).

Scenarios (each at 1x = 3 req/s and 10x = 30 req/s)
  inproc   threaded Server + FakeStore in this process, 80-200 ms per store call, 2% spikes of +1 s
           (the simulated organisers share our CPU here: a pessimistic case)
  http     RawTreeStore -> HTTP -> emulator in a separate process (real client code, like production)
  faults   http + 5% read errors, 5% insert errors, 2% ambiguous inserts, 1% 429s
Simulator: A2's sim/shop.py by default (--sim stub for the runtime's tiny one). Policy: A3's
SessionHeuristicPolicy by default (--policy busy = 2 ms CPU stand-in).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtrec.runtime.clock import RealClock  # noqa: E402
from rtrec.runtime.serve import ServeConfig, Server  # noqa: E402
from rtrec.stores.fake import Faults, FakeStore  # noqa: E402
from rtrec.stores.fake_http import FAULT_PROFILES  # noqa: E402
from rtrec.stores.rawtree import RawTreeStore  # noqa: E402
from rtrec.stores.stub_sim import StubParams, StubSimulator  # noqa: E402

NOMINAL_RATE = 3.0


class BusyPolicy:
    """Burns ~ms_per_request of CPU, then returns best sellers shuffled by request."""
    name = "busy-2ms"

    def __init__(self, ms_per_request: float = 2.0):
        self.s = ms_per_request / 1000

    def recommend(self, req, state):
        t = time.perf_counter() + self.s
        x = 0
        while time.perf_counter() < t:
            x += 1
        k = hash(req["request_id"]) % 20
        return state.safe_list[k:k + 10] + state.safe_list[:10]


def _make_sim(kind: str, multiplier: float, start: float):
    if kind == "shop":   # A2's realistic simulator (organisers' side: it also burns CPU in this process)
        from rtrec.stores import make_simulator, warm_up
        sim = make_simulator(seed=7, start=start, rate_multiplier=multiplier)
        warm_up(sim)
        return sim, NOMINAL_RATE * multiplier
    params = StubParams(n_products=5000, request_rate=NOMINAL_RATE * multiplier, event_rate=50.0 * multiplier,
                        update_rate=1.0 * multiplier)
    return StubSimulator(seed=7, start=start, params=params), params.request_rate


def _make_policy(spec: str, srv: Server, sim_kind: str, multiplier: float):
    """Built synchronously before serving (production builds it in the background). Its history dump comes
    from a twin of the organisers' simulator (same seed): the history is what we would have downloaded."""
    if spec == "busy":
        return BusyPolicy()
    from rtrec.runtime.serve import _load_obj, _wants_args, as_factory
    factory = as_factory(_load_obj(spec))
    if not _wants_args(factory):
        return factory()
    twin, _ = _make_sim(sim_kind, multiplier, time.time())
    dump = twin.history(days=getattr(getattr(twin, "cfg", None), "history_days", 60))
    return factory(srv.state, dump)


def _remote_emulator(sim_kind: str, multiplier: float, faults: str):
    """The organisers' side in its own process, as in reality: its CPU never competes with our GIL."""
    import socket
    import subprocess
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    cmd = [sys.executable, "-m", "rtrec.stores.fake_http", "--port", str(port), "--seed", "7", "--sim", sim_kind,
           "--rate-multiplier", str(multiplier), "--faults", faults, "--api-key", "k"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    line = proc.stdout.readline()          # printed once the simulator is built and serving
    if "http://" not in line:
        proc.kill()
        raise RuntimeError(f"emulator failed to start: {line!r}")
    return proc, f"http://127.0.0.1:{port}"


def scenario(name: str, duration: float, multiplier: float, sim_kind: str = "stub", policy: str = "busy") -> dict:
    import requests as rq
    faults_name = "flaky" if name == "faults" else "latency"
    proc = fake = None
    if name == "inproc":
        sim, rate = _make_sim(sim_kind, multiplier, time.time())
        store = fake = FakeStore(sim, RealClock(), faults=Faults(seed=3, **FAULT_PROFILES[faults_name]),
                                 visibility_delay=0.4)
    else:
        proc, url = _remote_emulator(sim_kind, multiplier, faults_name)
        rate = NOMINAL_RATE * multiplier
        store = RawTreeStore("team_00", "k", url)
    try:
        with tempfile.TemporaryDirectory() as d:
            srv = Server(store, None, config=ServeConfig(state_dir=d))
            srv.start()
            pol = _make_policy(policy, srv, sim_kind, multiplier)
            srv.runner.set_policy(pol)
            t0 = time.time()
            srv.run(duration=duration)
            time.sleep(8)   # deadline + feedback delay for the last requests
            # exclude the first 3 s (requests that queued while the policy was built) and the last 1.5 s
            since, until = t0 + 3, t0 + duration - 1.5
            if fake is not None:
                rep = fake.report(since=since, until=until)
            else:
                rep = rq.get(f"{url}/report", params={"since": since, "until": until}, timeout=120).json()
            st = srv.status()
    finally:
        if proc:
            proc.kill()
    return {"scenario": name, "sim": sim_kind, "policy": getattr(pol, "name", policy), "rate_req_s": rate,
            "duration_s": duration, "report": rep,
            "runtime": {k: st[k] for k in ("ack_latency_s", "compute_ms_per_batch", "visible_lag_s")},
            "counters": st["counters"], "policy_timeouts": st["policy_timeouts"]}


def to_markdown(results: list[dict]) -> str:
    lines = [
        "# Load test: serving loop at 1x and 10x nominal rate (SIMULATED)",
        "",
        f"Generated by `python ops/loadtest.py` on {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}. "
        "Local simulated shop; injected network latency 80-200 ms per call, 2% spikes of +1 s; "
        "policy costs ~2 ms CPU per request. Organisers' rules emulated: visible 0.4 s after `ts`, "
        "judged every 0.5 s, deadline 3 s. **Not a measurement of the live platform.**",
        "",
        "| scenario | sim | policy | req/s | requests | on time & valid | missing | late | invalid (stock race) | dup | "
        "received p50 / p95 / p99 / max (s) | ack p99 (s) | compute p99 (ms/batch) | insert errors |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        rep, rs = r["report"], r["report"]["reasons"]
        lat = rep["latency_received_s"]
        c = r["counters"]
        lines.append(
            f"| {r['scenario']} | {r['sim']} | {r['policy']} | {r['rate_req_s']:.0f} | {rep['requests']} | {100 * rep['reliability']:.2f}% | "
            f"{rs.get('missing', 0)} | {rs.get('late', 0)} | {rs.get('invalid', 0)} | {rep['duplicate_answers']} | "
            f"{lat.get('p50')} / {lat.get('p95')} / {lat.get('p99')} / {lat.get('max')} | "
            f"{r['runtime']['ack_latency_s'].get('p99')} | {r['runtime']['compute_ms_per_batch'].get('p99')} | "
            f"{c.get('insert_errors', 0)} |")
    lines += ["", "`received` = time from request `ts` to the organisers' poll that sees our answer (what the "
              "3 s rule judges). `invalid` answers come from products going out of stock between our last "
              "catalogue refresh and the judge's poll (unavoidable race, ~1 s window).", "",
              "<details><summary>raw results</summary>", "", "```json", json.dumps(results, indent=1), "```",
              "", "</details>", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=120.0)
    ap.add_argument("--scenarios", default="inproc,http,faults")
    ap.add_argument("--sim", default="shop", choices=["shop", "stub"])
    ap.add_argument("--policy", default="rtrec.model.policies:SessionHeuristicPolicy",
                    help="module:Class/factory, or 'busy' (2 ms CPU stand-in)")
    ap.add_argument("--rates", default="1,10", help="multipliers of the nominal 3 req/s")
    ap.add_argument("--out", default=str(Path(__file__).parent / "reports" / "loadtest.md"))
    a = ap.parse_args()
    logging.basicConfig(level="WARNING", format="%(asctime)s %(levelname)s %(name)s %(message)s")
    results = []
    runs = [(float(m), name) for m in a.rates.split(",") for name in a.scenarios.split(",")]
    for mult, name in runs:
        print(f"running {name} for {a.duration:.0f} s at {mult:g}x ({a.sim}, {a.policy}) ...", flush=True)
        results.append(scenario(name, a.duration, mult, a.sim, a.policy))
        rep = results[-1]["report"]
        print(f"  reliability {100 * rep['reliability']:.2f}%  reasons {rep['reasons']}  "
              f"received {rep['latency_received_s']}", flush=True)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(to_markdown(results))
    print(f"report: {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
