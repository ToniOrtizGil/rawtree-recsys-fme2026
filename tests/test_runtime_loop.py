"""End-to-end serving on a simulated clock: every request answered once, on time, valid."""
import time

from rtrec.runtime.policies import BaselinePolicy
from rtrec.runtime.testing import SimEnv
from rtrec.stores.fake import Faults
from rtrec.stores.stub_sim import StubParams


def test_every_request_answered_once_on_time():
    env = SimEnv(seed=2)
    srv = env.server(policy=BaselinePolicy())
    env.run(srv, seconds=300)
    rep = env.store.report()
    assert rep["requests"] > 700
    assert rep["reasons"].get("missing", 0) == 0
    assert rep["reasons"].get("late", 0) == 0
    assert rep["duplicate_answers"] == 0
    assert rep["reliability"] >= 0.99     # only stock-outs racing the judge can cost anything
    assert rep["latency_received_s"]["p99"] < 1.8
    assert rep["model_versions"] == {"baseline": rep["requests"]}


def test_every_answer_has_ten_distinct_available_items_when_served():
    env = SimEnv(seed=4)
    srv = env.server()
    bad = []
    orig = srv.serve

    def serve(reqs):
        out = orig(reqs)
        for s in out:
            items = s["items"]
            if len(items) != 10 or len(set(items)) != 10 or not set(items) <= srv.state.available:
                bad.append(s)
        return out

    srv.serve = serve
    env.run(srv, seconds=120)
    assert not bad
    assert env.store.report()["requests"] > 200


def test_ten_times_nominal_rate_on_sim_clock():
    env = SimEnv(seed=5, params=StubParams(request_rate=30.0, event_rate=200.0))
    srv = env.server(policy=BaselinePolicy())
    t0 = time.perf_counter()
    env.run(srv, seconds=120)
    cpu = time.perf_counter() - t0
    rep = env.store.report()
    assert rep["requests"] > 3000
    assert rep["reasons"].get("missing", 0) == 0 and rep["reasons"].get("late", 0) == 0
    assert cpu < 60   # the loop's own overhead is far below real time


def test_flaky_store_still_on_time():
    """10% of reads and writes fail, 5% of inserts fail *after* writing: retries must cover it."""
    env = SimEnv(seed=6, faults=Faults(read_error=0.1, insert_error=0.1, ambiguous_insert=0.05, seed=1))
    srv = env.server(policy=BaselinePolicy())
    env.run(srv, seconds=300)
    rep = env.store.report()
    assert rep["reliability"] >= 0.98
    assert rep["conflicting_answers"] == 0          # retries never send different items
    assert srv.metrics.c["insert_errors"] > 0
