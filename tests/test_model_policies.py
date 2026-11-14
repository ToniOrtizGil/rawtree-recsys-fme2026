"""A3: policies are pure, fast, valid on empty and on populated State, and beat the baseline on the toy shop."""
import copy
import time

import numpy as np
import pytest

from rtrec.contract import State
from rtrec.model.candidates import generate
from rtrec.model.features import FEATURES, featurize
from rtrec.model.policies import BaselinePolicy, RankerPolicy, SessionHeuristicPolicy
from rtrec.offline.replay import state_from_history
from rtrec.offline.simeval import evaluate_on_sim
from rtrec.offline.toysim import make_simulator
from rtrec.validate import finalize, is_valid


@pytest.fixture(scope="module")
def world():
    sim = make_simulator(seed=3, n_products=250, n_users=150)
    dump = sim.history(days=10, sessions_per_day=150)
    state, feed = state_from_history(dump.catalog, dump.history_sessions, dump.history_events)
    return sim, dump, state, feed


POLICIES = [BaselinePolicy(), SessionHeuristicPolicy(), RankerPolicy()]


@pytest.mark.parametrize("pol", POLICIES, ids=lambda p: p.name)
def test_empty_state_still_valid(pol):
    s = State(available=frozenset(f"p{i}" for i in range(20)))
    req = {"request_id": "r", "ts": "2026-11-15T09:00:00Z", "user_id": "u", "is_new_user": True,
           "context": {"device": "ios", "region": "X", "page": "home"}, "recent_views": []}
    assert is_valid(finalize(pol.recommend(req, s), s), s)


def test_pure_fast_and_valid(world):
    sim, _, state, _ = world
    reqs = sim.advance(sim.now + 30).requests
    before = copy.deepcopy((state.available, state.users, state.safe_list))
    for pol in POLICIES:
        t0 = time.perf_counter()
        for r in reqs:
            assert is_valid(finalize(pol.recommend(r, state), state), state)
        ms = 1000 * (time.perf_counter() - t0) / len(reqs)
        assert ms < 5.0, f"{pol.name}: {ms:.2f} ms/request"
    assert before == (state.available, state.users, state.safe_list)


def test_features_have_no_ids_and_right_shape(world):
    sim, _, state, _ = world
    req = sim.advance(sim.now + 5).requests[0]
    cands = generate(req, state)
    X = featurize(req, state, cands)
    assert X.shape == (len(cands), len(FEATURES)) and np.isfinite(X).all()
    assert not any("id" in f.split("_") for f in FEATURES)


def test_candidates_only_available_and_include_views(world):
    sim, _, state, _ = world
    for req in sim.advance(sim.now + 20).requests:
        c = generate(req, state)
        assert set(c) <= state.available
        for v in req["recent_views"]:
            if v["product_id"] in state.available:
                assert v["product_id"] in c


def test_heuristic_beats_baseline_on_toy(world):
    sim, _, state, feed = world
    rep = evaluate_on_sim(sim, state, feed, [BaselinePolicy(), SessionHeuristicPolicy()], minutes=1.5)
    pol = rep["policies"]
    assert pol["baseline"]["score"] == pytest.approx(0.0, abs=1e-9)
    assert pol["v1-session-heuristic"]["score"] > 10
