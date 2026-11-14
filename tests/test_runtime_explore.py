"""Practice-round exploration: reproducible, top slots untouched, propensities logged with every answer."""
import time

from rtrec.contract import State
from rtrec.runtime.explore import Explore, ServedLog
from rtrec.runtime.testing import SimEnv


class Ranked:
    name = "ranked"

    def recommend(self, req, state):
        return sorted(state.available)[:50]


def _state():
    return State(available=frozenset(f"p{i:03d}" for i in range(100)))


def test_explore_is_pure_keeps_top3_and_logs_propensities():
    st, ex = _state(), Explore(Ranked(), eps=0.5)
    req = {"request_id": "r1"}
    a, b = ex.recommend(req, st), ex.recommend(req, st)
    assert a == b and len(set(a)) == 10                        # seeded by request_id
    assert a[:3] == sorted(st.available)[:3]
    info = ex.info("r1", a)
    assert info["propensity"][:3] == [1.0, 1.0, 1.0]
    for p, e in zip(info["propensity"][3:], info["explored"][3:]):
        assert p == (0.5 / 30 if e else 0.5)    # pool = top-40 minus the 10 shown
    rates = [sum(ex.info(f"r{i}", ex.recommend({"request_id": f"r{i}"}, st))["explored"]) / 7 for i in range(400)]
    assert 0.4 < sum(rates) / len(rates) < 0.6


def test_served_rows_reach_scratch_with_propensities():
    env = SimEnv(seed=2)
    ex = Explore(Ranked(), eps=0.2)
    srv = env.server(policy=ex)
    log = ServedLog(env.store, get_policy=lambda: srv.runner.policy, flush_s=0.05)
    srv.hooks.add(log)
    env.run(srv, seconds=30)
    time.sleep(0.3)
    rows = [r for r in env.store.scratch if r.get("kind") == "served"]
    assert len(rows) == srv.metrics.c["submitted"] > 50
    r = rows[-1]
    assert len(r["items"]) == len(r["propensity"]) == 10 and r["model_version"] == "ranked+explore20"
