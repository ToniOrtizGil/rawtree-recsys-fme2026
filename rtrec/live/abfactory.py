"""Live A/B factory for the practice round: baseline vs v1 vs v2 on the real stream.

    RTREC_POLICY=rtrec.live.abfactory:baseline_v1_v2 ops/run.sh

Why: offline IPS/SNIPS on the real history had ~3k matched slots and +-2..6 purchases/100 intervals,
too wide to pick a model. The organisers' feedback is the ground truth, so we split live traffic by
crc32(salt + request_id) (rtrec.live.ab) and compare purchases/100 per arm afterwards
(ops/ab_report.py recomputes the arm from request_id, so it does not depend on runtime internals).
"""
from __future__ import annotations

from rtrec.contract import State

from .ab import ABRouter, ABTest, install

import os

SALT = os.environ.get("RTREC_AB_SALT", "ab-live-1")
_ALL = {"baseline": "baseline", "v1": "v1-session-heuristic", "v2": "v2-lgbm"}
# RTREC_AB_ARMS=v1,v2 -> two arms (more power per arm); default: all three
ARMS = [(_ALL[a.strip()], 1.0) for a in os.environ.get("RTREC_AB_ARMS", "baseline,v1,v2").split(",") if a.strip()]


def baseline_v1_v2(state: State | None = None, dump=None) -> ABRouter:
    from rtrec.model.policies import BaselinePolicy, RankerPolicy, SessionHeuristicPolicy

    v2 = RankerPolicy(state, dump)            # builds state.offline once (artefacts + ranker + profiles)
    v1 = SessionHeuristicPolicy(state, dump)  # reuses them
    base = BaselinePolicy(state, dump)
    test = ABTest(arms=ARMS, salt=SALT)
    if state is not None:
        install(state, test)
    pols = {"baseline": base, "v1-session-heuristic": v1, "v2-lgbm": v2}
    return ABRouter(test, {name: pols[name] for name, _ in ARMS})
