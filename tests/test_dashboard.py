"""Dashboard: metrics, alert rules, collector robustness and the HTTP server (no network beyond localhost)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from dashboard import alerts, metrics
from dashboard.collector import Collector
from dashboard.demo import OUTAGE_EVERY_S, OUTAGE_LEN_S, DemoStore
from dashboard.metrics import FB
from dashboard.server import full_snapshot, make_server

NOW = 1_794_733_200.0      # 2026-11-15 09:00:00 UTC (start of the final), fixed


def fb(i, ok=True, reason="", lat=900.0, buy=False, version="v1", cold=False, ts=None):
    return FB(request_id=f"r{i}", ts=NOW - 10 if ts is None else ts, served_by="team" if ok else "fallback",
              reason=reason, latency_ms=lat, clicked="p1" if buy else "", purchased="p1" if buy else "",
              revenue=10.0 if buy else 0.0, shown=("p1",), version=version, cold=cold)


# ------------------------------------------------------------------ metrics
def test_percentile_matches_linear_interpolation():
    assert metrics.percentile([], 0.5) is None
    assert metrics.percentile([1, 2, 3, 4], 0.5) == 2.5
    assert metrics.percentile(list(range(101)), 0.95) == pytest.approx(95.0)


def test_wilson_interval_contains_rate():
    lo, hi = metrics.wilson(5, 100)
    assert lo < 5 < hi and 0 <= lo and hi <= 100
    assert metrics.wilson(0, 0) == (0.0, 0.0)


def test_light_follows_organisers_rule():
    rows = [fb(i) for i in range(91)] + [fb(100 + i, ok=False, reason="late") for i in range(9)]
    assert metrics.light(rows) == "green"                     # 91 % > 90 %
    rows += [fb(200, ok=False, reason="missing")]
    assert metrics.light(rows) == "green"                     # 91/101 = 90.1 %
    rows += [fb(201, ok=False, reason="missing")]
    assert metrics.light(rows) == "red"                       # 91/102 = 89.2 %
    assert metrics.light(rows[:10]) == "unknown"


def test_feedback_from_row_and_latency_ignores_missing():
    r = FB.from_row({"request_id": "a", "ts": "2026-11-15T09:00:00.500Z", "served_by": "fallback",
                     "fallback_reason": "missing", "latency_ms": None, "clicked": "", "purchased": "",
                     "revenue_eur": 0, "shown": ["p"]})
    assert not r.ok and r.ts == metrics.to_epoch("2026-11-15T09:00:00.5+00:00")
    assert metrics.latency([r])["n"] == 0


def test_by_version_puts_fallback_apart_and_splits_cold():
    rows = [fb(1, buy=True, cold=True), fb(2), fb(3, version="v2"), fb(4, ok=False, reason="late")]
    out = {d["model_version"]: d for d in metrics.by_version(rows)}
    assert out["v1"]["n"] == 2 and out["v1"]["purchases"] == 1 and out["v1"]["cold"]["n"] == 1
    assert out["(fallback)"]["n"] == 1 and out["v2"]["p100"] == 0


def test_per_minute_has_one_point_per_minute():
    rows = [fb(i, ts=NOW - 30 - 60 * (i % 10), buy=i % 4 == 0) for i in range(200)]
    s = metrics.per_minute(rows, NOW, 60)
    assert len(s) == 60 and sum(p["n"] for p in s) == 200 and s[-1]["p100_5m"] is not None


# ------------------------------------------------------------------ alerts
def _snap(**kw):
    s = {"light": {"status": "green", "n": 900, "ok": 900, "ok_pct": 100.0,
                   "reasons": {"missing": 0, "late": 0, "invalid": 0}},
         "ages": {"request": 1.0, "feedback": 5.0, "event": 0.1, "heartbeat": 2.0},
         "latency_5m": {"p50": 800, "p95": 1300, "p99": 1700}, "errors": {}, "uptime_s": 600,
         "conversion": {"p100": 5.0}}
    for k, v in kw.items():
        s[k] = {**s[k], **v} if isinstance(v, dict) else v
    return s


def codes(s):
    return [a.code for a in alerts.evaluate(s)]


def test_quiet_when_all_good():
    assert codes(_snap()) == [] and alerts.overall(alerts.evaluate(_snap())) == "ok"


def test_red_light_names_the_cause_and_runbook():
    s = _snap(light={"status": "red", "ok": 700, "ok_pct": 77.8, "reasons": {"missing": 200, "late": 0, "invalid": 0}})
    al = alerts.evaluate(s)
    assert al[0].code == "LIGHT_RED" and al[0].runbook == "r1-missing" and alerts.overall(al) == "critical"
    assert alerts.one_line(s, al).startswith("CRITICAL light=red")


def test_early_warnings():
    assert codes(_snap(light={"ok": 870, "ok_pct": 96.7, "reasons": {"late": 30}})) == ["RELIABILITY_LOW"]
    assert codes(_snap(light={"ok": 899, "ok_pct": 99.9, "reasons": {"invalid": 1}})) == ["SEEN_INVALID"]
    assert "HEARTBEAT_LOST" in codes(_snap(ages={"heartbeat": 40.0}))
    assert "FEEDBACK_STALLED" in codes(_snap(ages={"feedback": 100.0}))
    assert "REQUESTS_STALLED" in codes(_snap(ages={"request": 45.0}))
    assert "LATENCY_P99" in codes(_snap(latency_5m={"p99": 2800}))
    assert "READ_ERROR" in codes(_snap(errors={"feedback": "HTTPError 429"}))


# ------------------------------------------------------------------ collector on the demo store
class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def test_collector_dedupes_and_resolves_everything():
    clk = Clock(NOW)
    store = DemoStore(seed=1, start=NOW - 600, clock=clk)
    col = Collector(store, sql_every_s=0)
    for _ in range(3):
        col.poll()
    n = len(col.feedback)
    col.poll()                                                # same data again: no duplicates
    assert len(col.feedback) == n > 1000
    snap = full_snapshot(col)
    assert {v["model_version"] for v in snap["versions"]} <= {"demo-v1-session", "demo-v0-bestsellers", "(fallback)"}
    assert snap["segments"]["cold"]["n"] > 0 and snap["segments"]["warm"]["n"] > 0
    assert snap["light"]["status"] == "green" and snap["heartbeat"]["role"] == "primary"
    assert len(snap["series"]) == 60 and snap["feed"] and len(snap["feed"][0]["shown"]) == 10
    json.dumps(snap, default=str)


def test_outage_turns_light_red_and_heartbeat_lost():
    clk = Clock(NOW)
    store = DemoStore(seed=2, start=NOW - 600, clock=clk, chaos=True)
    col = Collector(store, sql_every_s=0)
    col.poll()
    clk.t = store.t0 + OUTAGE_EVERY_S - 2                   # end of the first 75 s outage
    for _ in range(3):
        col.poll()
    snap = full_snapshot(col)
    got = [a["code"] for a in snap["alerts"]]
    assert snap["light"]["reasons"]["missing"] > 0 and "HEARTBEAT_LOST" in got
    assert snap["light"]["status"] == "red" and got[0] == "LIGHT_RED"   # 70 s of 300 s missing -> ~77 %
    assert OUTAGE_LEN_S / 300 > 0.1


class ProtocolOnly:
    """Only the methods of rtrec.store.Store (what A1's FakeStore guarantees), plus a flaky feedback read."""

    def __init__(self, inner, fail_feedback=False):
        self.inner, self.team, self.fail = inner, inner.team, fail_feedback

    def now(self):
        return self.inner.now()

    def catalog(self):
        return self.inner.catalog()

    def requests_since(self, c, limit=500):
        return self.inner.requests_since(c, limit)

    def events_since(self, c, limit=50_000):
        return self.inner.events_since(c, limit)

    def catalog_updates_since(self, c, limit=50_000):
        return self.inner.catalog_updates_since(c, limit)

    def feedback_since(self, c, limit=50_000):
        if self.fail:
            raise ConnectionError("boom")
        return self.inner.feedback_since(c, limit)

    def query(self, sql):
        raise NotImplementedError


def test_protocol_only_store_degrades_gracefully():
    inner = DemoStore(seed=4, start=NOW - 300, clock=Clock(NOW))
    col = Collector(ProtocolOnly(inner), sql_every_s=0)
    col.poll()
    snap = full_snapshot(col)
    assert {v["model_version"] for v in snap["versions"]} <= {"(version not readable)", "(fallback)"}
    assert "NO_HEARTBEAT_SOURCE" not in [a["code"] for a in snap["alerts"]]      # uptime 0 s: too early
    bad = Collector(ProtocolOnly(inner, fail_feedback=True), sql_every_s=0)
    bad.poll()                                                                   # must not raise
    codes_ = [a["code"] for a in full_snapshot(bad)["alerts"]]
    assert "READ_ERROR" in codes_ and "FEEDBACK_STALLED" in codes_


# ------------------------------------------------------------------ HTTP server
def test_server_endpoints():
    import threading
    col = Collector(DemoStore(seed=5, start=NOW - 300, clock=Clock(NOW)), sql_every_s=0)
    col.poll()
    srv = make_server(col, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert b"rtrec live" in urllib.request.urlopen(base + "/").read()
        snap = json.loads(urllib.request.urlopen(base + "/api/snapshot?window=15").read())
        assert snap["window_s"] == 900 and "alerts" in snap and snap["store"] == "demo"
        try:
            health = urllib.request.urlopen(base + "/api/health").read().decode()
        except urllib.error.HTTPError as e:   # 503 when critical
            health = e.read().decode()
        assert "light=" in health
        rb = urllib.request.urlopen(base + "/runbook").read().decode()
        for anchor in {v[1] for v in alerts.CAUSE.values()} | {"r0-triage", "r4-machine", "r5-stalled"}:
            assert f'<a id="{anchor}"></a>' in rb             # every alert link lands on a section
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/nope")
    finally:
        srv.shutdown()
