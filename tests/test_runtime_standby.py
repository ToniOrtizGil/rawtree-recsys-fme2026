"""Standby-by-heartbeat: silent while the primary beats, takes over within seconds when it dies."""
from rtrec.runtime.policies import BaselinePolicy
from rtrec.runtime.testing import SimEnv


def test_standby_silent_then_takes_over_then_hands_back():
    env = SimEnv(seed=11)
    primary = env.server(policy=BaselinePolicy(), role="primary", instance="A")
    standby = env.server(policy=BaselinePolicy(), role="standby", instance="B")
    env.run([primary, standby], seconds=60)
    assert standby.metrics.c["submitted"] == 0
    assert standby.metrics.c["shadow_answers"] > 0       # warm: computing every answer
    t_death = env.clock.now()
    env.run([None, standby], seconds=60)                 # primary dies (no more steps, no heartbeats)
    assert standby.heartbeat.active and standby.heartbeat.takeovers == 1
    assert standby.metrics.c["submitted"] > 0
    rep_gap = env.store.report(since=t_death, until=t_death + 60)
    # the takeover window (2.5 s + poll) may lose a handful; shadow answers cover most of it
    assert rep_gap["reasons"].get("missing", 0) <= 3
    # primary restarts under a new instance id: standby goes quiet again
    primary2 = env.server(policy=BaselinePolicy(), role="primary", instance="A2")
    env.run([primary2, standby], seconds=30)
    assert not standby.heartbeat.active
    rep = env.store.report()
    assert rep["conflicting_answers"] == 0


def test_standby_alone_serves_after_grace_period():
    env = SimEnv(seed=12)
    standby = env.server(policy=BaselinePolicy(), role="standby", instance="B")
    env.run(standby, seconds=30)
    assert standby.heartbeat.active
    assert env.store.report()["reliability"] > 0.85     # only the first ~3 s are lost
