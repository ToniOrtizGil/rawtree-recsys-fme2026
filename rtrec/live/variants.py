"""Named policy variants for scripts/arbiter.py (`module:Class`, built with (state, dump)), so each
live-learning piece is measured on its own against the same base:

    python scripts/arbiter.py rtrec.model.policies:SessionHeuristicPolicy \\
        rtrec.live.variants:HeurCold rtrec.live.variants:HeurColdRerank rtrec.live.variants:HeurAll
"""
from __future__ import annotations

from rtrec import live

from .policy import LiveSessionPolicy, LiveWrapper


def _seed(state, dump):
    """The arbiter only calls live.init; seed long-run shares so group lifts are sharp."""
    if state is not None and dump is not None and not live.learner(state).trends.baseline:
        live.seed_history(state, dump.history_events)


def _heur(state, dump):
    from rtrec.model.policies import SessionHeuristicPolicy
    _seed(state, dump)
    return SessionHeuristicPolicy(state, dump)


def _ranker(state, dump):
    from rtrec.model.policies import RankerPolicy
    _seed(state, dump)
    return RankerPolicy(state, dump)


class HeurSeeded:
    """A3's heuristic unchanged, but with history-seeded (sharper) subcat/brand lifts as features."""
    def __init__(self, state=None, dump=None):
        self.base = _heur(state, dump)
        self.name = "v1-seeded"

    def recommend(self, req, state):
        return self.base.recommend(req, state)


class LiveSession(LiveSessionPolicy):
    def __init__(self, state=None, dump=None):
        super().__init__()


def _variant(base_fn, name, **kw):
    class V(LiveWrapper):
        def __init__(self, state=None, dump=None):
            super().__init__(base_fn(state, dump), name=name, **kw)
    V.__name__ = name
    return V


HeurCold = _variant(_heur, "v1+cold", rerank=False)
HeurRerank = _variant(_heur, "v1+rerank", cold_merge=False)
HeurTrend = _variant(_heur, "v1+trend", cold_merge=False, alpha=0.0)
HeurCalib = _variant(_heur, "v1+calib", cold_merge=False, beta=0.0)
HeurColdRerank = _variant(_heur, "v1+cold+rerank")
HeurExplore = _variant(_heur, "v1+explore", cold_merge=False, rerank=False, explore=True)
HeurGroupTrend = _variant(_heur, "v1+grouptrend", cold_merge=False, alpha=0.0, beta=0.0, group_beta=1.0, gamma=1.0)
HeurGroupTrendSoft = _variant(_heur, "v1+grouptrend-soft", cold_merge=False, alpha=0.0, beta=0.0, group_beta=0.5,
                              gamma=1.5)
HeurAll = _variant(_heur, "v1+all", explore=True)
RankCold = _variant(_ranker, "v2+cold", rerank=False)
RankTrend = _variant(_ranker, "v2+trend", cold_merge=False, alpha=0.0)
RankExplore = _variant(_ranker, "v2+explore", cold_merge=False, rerank=False, explore=True)
RankAll = _variant(_ranker, "v2+all", alpha=0.0, explore=True)


# --- ablations: the same A3 policy with some live keys hidden, to measure what each A4 signal adds
_GROUPS = {
    "trend": ("trend_rate", "trend_lift", "trend_top", "trend_rising", "trend_top_by_subcat", "subcat_lift",
              "buy_rate", "trend_global_lift"),
    "ctx": ("ctx_top", "device_top"),
    "coview": ("coview",),
    "newprod": ("new_neighbours", "similar_new"),
    "calib": ("calib_product", "calib_subcat", "calib_posterior"),
    "price": ("price",),
}


class _Hide:
    def __init__(self, base, hide: tuple, name: str):
        self.base, self.hide, self.name = base, set(hide), name

    def recommend(self, req, state):
        from dataclasses import replace
        view = replace(state, live={k: v for k, v in state.live.items() if k not in self.hide})
        return self.base.recommend(req, view)


def _ablation(base_fn, name, groups):
    keys = tuple(k for g in groups for k in _GROUPS[g])

    class A(_Hide):
        def __init__(self, state=None, dump=None):
            super().__init__(base_fn(state, dump), keys, name)
    A.__name__ = name
    return A


HeurNoLive = _ablation(_heur, "v1-nolive", list(_GROUPS))
HeurNoTrend = _ablation(_heur, "v1-notrend", ["trend"])
HeurNoCtx = _ablation(_heur, "v1-noctx", ["ctx"])
HeurNoCoview = _ablation(_heur, "v1-nocoview", ["coview"])
HeurNoNewProd = _ablation(_heur, "v1-nonewprod", ["newprod"])
RankNoLive = _ablation(_ranker, "v2-nolive", list(_GROUPS))
RankNoTrend = _ablation(_ranker, "v2-notrend", ["trend"])
RankNoCalib = _ablation(_ranker, "v2-nocalib", ["calib"])
