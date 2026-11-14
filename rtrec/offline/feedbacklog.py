"""Training rows from the tables RawTree keeps for us: requests + our feedback + the shop stream.

This is how v2 learns from the practice round (and from the final round, if we retrain during it):
nothing has to be logged in the hot path. Afterwards we read back
    shop.requests            what each shopper looked like (context, recent views)
    team_NN.feedback         what we showed (`shown`), and what was clicked / bought
    shop.events, shop.catalog_updates   the stream, to rebuild the live State as of each request
and replay them in time order through `rtrec.live` (same code as serving). Every shown (product,
slot) becomes a row with the features we had at request time and its click / purchase label.
Fallback answers (`served_by = fallback`) are kept: they are best sellers shown to everyone, a
useful contrast to our personalised lists.
"""
from __future__ import annotations

import numpy as np

from rtrec.contract import State
from rtrec.model.features import FEATURES, featurize
from rtrec.model.util import clean_id, to_epoch

from .replay import LiveFeed


def rows_from_logs(state: State, feed: LiveFeed, requests: list[dict], feedback: list[dict], events: list[dict],
                   catalog_updates: list[dict], feedback_delay_s: float = 5.0):
    """Returns X, y (purchase), c (click), group. `state`/`feed` hold everything before the first request."""
    fb = {f["request_id"]: f for f in feedback if f.get("request_id") and f["request_id"] != "__init__"}
    stream = [(to_epoch(e["ts"]), 0, e) for e in catalog_updates]
    stream += [(to_epoch(e["ts"]), 1, e) for e in events]
    stream += [(to_epoch(r["ts"]), 2, r) for r in requests if r.get("request_id") in fb]
    # feedback becomes visible a few seconds after the request (the organisers' delay)
    stream += [(to_epoch(r["ts"]) + feedback_delay_s, 3, fb[r["request_id"]]) for r in requests if r.get("request_id") in fb]
    stream.sort(key=lambda x: (x[0], x[1]))
    Xs, ys, cs, gs = [], [], [], []
    buf: dict[int, list] = {0: [], 1: [], 3: []}

    def flush() -> None:
        feed.on_catalog_updates(buf[0])
        feed.on_events(buf[1])
        feed.on_feedback(buf[3])
        for k in buf:
            buf[k] = []

    for t, kind, row in stream:
        if kind != 2:
            buf[kind].append(row)
            continue
        flush()
        f = fb[row["request_id"]]
        shown = [str(p) for p in list(f.get("shown") or [])[:10]]
        if not shown:
            continue
        Xs.append(featurize(row, state, shown, slot=list(range(len(shown))), now=t))
        pur, clk = clean_id(f.get("purchased")), clean_id(f.get("clicked"))
        ys.append(np.array([1.0 if p == pur else 0.0 for p in shown]))
        cs.append(np.array([1.0 if p == clk else 0.0 for p in shown]))
        gs.append(np.full(len(shown), len(gs)))
    flush()
    if not Xs:
        return np.zeros((0, len(FEATURES)), np.float32), np.zeros(0), np.zeros(0), np.zeros(0, int)
    return np.vstack(Xs), np.concatenate(ys), np.concatenate(cs), np.concatenate(gs)
