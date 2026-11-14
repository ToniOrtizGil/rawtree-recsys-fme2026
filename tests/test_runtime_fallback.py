"""The three fallback levels: a broken, slow or hung model never costs an answer."""
import threading
import time

from rtrec.runtime.testing import SimEnv


class Raising:
    name = "raising"

    def recommend(self, req, state):
        raise ValueError("boom")


class Garbage:
    name = "garbage"

    def recommend(self, req, state):
        return ["nope", "nope", None, 42] + sorted(state.available)[:3] * 3


class Hanging:
    """Hangs once for 1.5 s (real time), then behaves."""
    name = "hanging"

    def __init__(self):
        self.hung = threading.Event()

    def recommend(self, req, state):
        if not self.hung.is_set():
            self.hung.set()
            time.sleep(1.5)
        return state.safe_list[:10]


class Slow:
    name = "slow"

    def recommend(self, req, state):
        time.sleep(0.03)   # 30 ms per request: a batch of >5 overruns the 150 ms budget
        return state.safe_list[:10]


def _served(env, srv, seconds):
    out = []
    orig = srv.serve
    srv.serve = lambda reqs: (lambda r: (out.extend(r), r)[1])(orig(reqs))
    env.run(srv, seconds=seconds)
    return out


def test_raising_policy_falls_back_and_gets_benched():
    env = SimEnv(seed=1)
    srv = env.server(policy=Raising())
    subs = _served(env, srv, 60)
    assert subs and all(len(set(s["items"])) == 10 for s in subs)
    assert all(s["model_version"] == "fallback-recent" for s in subs)
    assert env.store.report()["reliability"] > 0.98
    assert srv.runner.errors >= 20   # breaker tripped after 20 consecutive errors


def test_garbage_is_repaired():
    env = SimEnv(seed=1)
    srv = env.server(policy=Garbage())
    subs = _served(env, srv, 30)
    assert subs and all(len(s["items"]) == 10 and len(set(s["items"])) == 10 for s in subs)
    assert all(s["model_version"] == "garbage" for s in subs)


def test_hung_policy_is_bypassed_then_resumes():
    env = SimEnv(seed=1)
    pol = Hanging()
    srv = env.server(policy=pol)
    t0 = time.perf_counter()
    subs = []
    orig = srv.serve
    srv.serve = lambda reqs: (lambda r: (subs.extend(r), r)[1])(orig(reqs))
    while time.perf_counter() - t0 < 2.5:     # real time so the hung call can finish
        env.clock.advance(0.1)
        srv.step()
        time.sleep(0.01)
    versions = [s["model_version"] for s in subs]
    assert "fallback-recent" in versions          # served while the policy was stuck
    assert versions[-1] == "hanging"              # and back to level 1 afterwards
    assert srv.runner.timeouts >= 1


def test_slow_policy_answers_what_fits_and_falls_back_for_the_rest():
    env = SimEnv(seed=1)
    srv = env.server(policy=Slow())
    reqs = [{"request_id": f"x{i}", "ts": "2025-11-14T09:00:00.000Z", "recent_views": [], "is_new_user": True,
             "user_id": "u", "context": {}} for i in range(20)]
    srv.start()
    srv.cfg.stale_s = 1e12
    t0 = time.perf_counter()
    out = srv.serve(reqs)
    assert time.perf_counter() - t0 < 0.4
    assert len(out) == 20
    mv = [s["model_version"] for s in out]
    assert "slow" in mv and "fallback-recent" in mv
