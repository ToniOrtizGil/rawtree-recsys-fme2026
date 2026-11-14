"""Crash safety: a restart neither re-answers differently nor forgets an answer it had decided."""
from rtrec.runtime.policies import BaselinePolicy
from rtrec.runtime.testing import SimEnv
from rtrec.stores.fake import Faults, StoreError


class Counting:
    """Different items on every call: any re-answer after a restart would show up as a conflict."""
    name = "counting"

    def __init__(self):
        self.n = 0

    def recommend(self, req, state):
        self.n += 1
        lst = sorted(state.available)
        k = self.n % max(1, len(lst) - 10)
        return lst[k:k + 10]


def test_crash_and_restart_no_conflicting_answers(tmp_path):
    env = SimEnv(seed=3)
    srv = env.server(policy=Counting(), state_dir=tmp_path)
    env.run(srv, seconds=60)
    # crash: no stop(), no snapshot flush; a new process starts 1 s later with the same state dir
    env.clock.advance(1.0)
    srv2 = env.server(policy=Counting(), state_dir=tmp_path)
    env.run(srv2, seconds=60)
    rep = env.store.report()
    assert rep["conflicting_answers"] == 0
    assert rep["duplicate_answers"] == 0
    assert rep["reliability"] >= 0.98


def test_decided_but_unsent_answers_are_resent_identically(tmp_path):
    env = SimEnv(seed=3)
    srv = env.server(policy=Counting(), state_dir=tmp_path)
    env.run(srv, seconds=20)

    def down(rows):
        raise StoreError("network down")

    srv.store = srv.writer.store = None   # this "process" can no longer reach the store
    srv.writer.store = type("Down", (), {"insert_submissions": staticmethod(down)})()
    srv.store = env.store
    env.run(srv, seconds=0.5)             # decides answers, cannot send them, then crashes
    pending = dict(srv.journal.unacked)
    assert pending
    srv2 = env.server(policy=Counting(), state_dir=tmp_path)
    env.run(srv2, seconds=20)
    written = {}
    for _, row in env.store.submissions:
        written.setdefault(row["request_id"], row["items"])
    for rid, (a, _) in pending.items():
        assert written.get(rid) == a["items"]
    assert env.store.report()["conflicting_answers"] == 0


def test_snapshot_restores_catalog_and_learnt_state(tmp_path):
    env = SimEnv(seed=8)
    srv = env.server(policy=BaselinePolicy(), state_dir=tmp_path)
    env.run(srv, seconds=120)
    srv.state.live["trend"] = {"x": 1.0}
    srv.save_snapshot()
    avail = srv.state.available
    srv2 = env.server(policy=BaselinePolicy(), state_dir=tmp_path)
    srv2.start()
    assert srv2.state.live["trend"] == {"x": 1.0}
    # same catalogue view (modulo updates in the last instant)
    assert len(avail ^ srv2.state.available) <= 2


def test_flaky_restart_loop(tmp_path):
    """Crash every 15 s for 2 minutes with a flaky store: still no conflicts, few misses."""
    env = SimEnv(seed=9, faults=Faults(read_error=0.05, insert_error=0.05, ambiguous_insert=0.05, seed=2))
    for _ in range(8):
        srv = env.server(policy=Counting(), state_dir=tmp_path)
        env.run(srv, seconds=15)
        env.clock.advance(0.5)   # restart takes half a second
    rep = env.store.report()
    assert rep["conflicting_answers"] == 0
    assert rep["reliability"] >= 0.95
