"""Regression for the master's finding: a real-time FakeStore run (as `serve --store fake` does it) must
be on time. Uses make_store exactly like the CLI and the dashboard."""
import time

from rtrec.runtime.policies import BaselinePolicy
from rtrec.runtime.serve import ServeConfig, Server
from rtrec.stores import make_store


def test_realtime_fake_store_60s_on_time(tmp_path, monkeypatch):
    monkeypatch.setenv("RTREC_SIM", "shop")
    store = make_store("fake", seed=3, small=True)
    srv = Server(store, BaselinePolicy(), config=ServeConfig(state_dir=str(tmp_path)))
    t0 = store.clock.now()
    srv.run(duration=60)
    time.sleep(8)   # deadline + feedback delay of the last requests
    rep = store.report(since=t0 + 2, until=t0 + 58)
    print(rep["reasons"], rep["latency_received_s"])
    assert rep["requests"] > 120
    assert rep["reliability"] >= 0.98
    assert rep["latency_received_s"]["p99"] < 1.8
