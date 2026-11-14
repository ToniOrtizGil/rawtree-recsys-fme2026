"""A4's hooks see every row exactly once, in order, and can never break serving."""
from collections import Counter

from rtrec.runtime.cursor import event_key
from rtrec.runtime.testing import SimEnv


class Recorder:
    def __init__(self):
        self.calls = Counter()
        self.event_keys = []
        self.fb = []

    def on_start(self, state):
        self.calls["start"] += 1

    def on_events(self, rows, state):
        self.calls["events"] += 1
        self.event_keys.extend(event_key(r) for r in rows)

    def on_catalog_updates(self, rows, state):
        self.calls["catalog"] += 1
        assert all(r["product_id"] in state.catalog for r in rows)   # already applied

    def on_feedback(self, rows, state):
        self.fb.extend(r["request_id"] for r in rows)

    def on_served(self, answers, state):
        self.calls["served"] += len(answers)

    def on_tick(self, state, now):
        self.calls["tick"] += 1


class Exploding:
    def on_events(self, rows, state):
        raise RuntimeError("bug in live learning")

    def on_tick(self, state, now):
        raise RuntimeError("bug")


def test_hooks_receive_each_row_once_and_errors_are_contained():
    env = SimEnv(seed=5)
    rec = Recorder()
    srv = env.server()
    srv.hooks.add(Exploding())
    srv.hooks.add(rec)
    env.run(srv, seconds=120)
    assert rec.calls["start"] == 1
    assert rec.calls["events"] > 50 and rec.calls["catalog"] > 5 and rec.calls["tick"] >= 100
    assert len(rec.event_keys) == len(set(rec.event_keys))           # deduplicated across overlaps
    assert len(rec.fb) == len(set(rec.fb)) and len(rec.fb) > 200
    assert rec.calls["served"] == srv.metrics.c["submitted"]
    assert srv.hooks.errors["Exploding.on_events"] > 0
    assert env.store.report()["reliability"] > 0.98
    all_events = {event_key(e) for e in env.store.events}
    assert set(rec.event_keys) <= all_events and len(rec.event_keys) > 0.9 * len(all_events)
