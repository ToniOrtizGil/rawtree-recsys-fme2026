"""A3: the training pipeline runs end to end on the toy shop, exports a loadable model, and the ranker policy uses it."""
import importlib.util
import sys
from pathlib import Path

import numpy as np

from rtrec.contract import State
from rtrec.model import ranker as rk
from rtrec.model.features import FEATURES
from rtrec.model.policies import BaselinePolicy, RankerPolicy, SessionHeuristicPolicy
from rtrec.offline.pipeline import fit, offline_eval, prepare
from rtrec.offline.toysim import make_simulator
from rtrec.validate import finalize, is_valid


def test_train_export_load_and_serve(tmp_path):
    sim = make_simulator(seed=5, n_products=200, n_users=120)
    dump = sim.history(days=12, sessions_per_day=150)
    p = prepare(dump.catalog, dump.history_sessions, dump.history_events, log=lambda *_: None)
    assert p.X.shape[1] == len(FEATURES) and p.y.sum() > 0
    booster, metrics = fit(p, rounds=50, log=lambda *_: None)
    path = tmp_path / "ranker.txt"
    rk.save(booster, path, metrics)
    loaded = rk.load(path)
    assert np.allclose(rk.predict(loaded, p.X[:50]), rk.predict(booster, p.X[:50]))

    rep = offline_eval(p, [BaselinePolicy(), SessionHeuristicPolicy(), RankerPolicy()], booster=booster,
                       sim=sim, max_impressions=300, log=lambda *_: None)
    for row in rep["policies"].values():
        assert np.isfinite(row["truth"]) and np.isfinite(row["dr_emp"])

    # policy built like the arbiter does: Policy(state, dump), model from a path
    state = State()
    from rtrec import live
    live.init(state, dump.catalog)
    pol = RankerPolicy(state, dump, model_path=path)
    assert state.offline.get("ranker") is not None and state.users
    req = sim.advance(sim.now + 5).requests[0]
    assert is_valid(finalize(pol.recommend(req, state), state), state)


def test_arbiter_accepts_our_policies():
    spec = importlib.util.spec_from_file_location("arbiter", Path(__file__).parents[1] / "scripts" / "arbiter.py")
    arb = importlib.util.module_from_spec(spec)
    sys.modules["arbiter"] = arb
    spec.loader.exec_module(arb)
    sim = make_simulator(seed=2, n_products=200, n_users=100)
    rep = arb.run(arb.load("rtrec.model.policies:SessionHeuristicPolicy"), sim, 30, 60, sim.now)
    d = rep.as_dict()
    assert d["requests"] > 50 and d["policy_errors"] == 0 and d["invalid_before_finalize"] == 0
