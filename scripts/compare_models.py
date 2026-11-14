"""Gate for the Sunday refit: may the refit model replace the current one? (agreed rule, 14-nov)

    python scripts/compare_models.py --data data_final --current artifacts/ranker_v3.txt \\
        --candidate artifacts/ranker_final.txt --ok artifacts/ranker_final.ok

Data: data_final/holdout/ = the last hours of the practice stream, set aside by
scripts/live_to_history.py --holdout-hours, never used to train, tune or pick anything.
Both models answer every held-out request with the State as of that moment (events replayed in order).
Per request we record whether the shopper's clicked / purchased product is in each model's top 10.
Bootstrap over shoppers (user_id) of the difference candidate - current.

The candidate passes only if   P(click-hit better) >= 0.95   and   P(purchase-hit better) >= 0.50
(clicks are the better-powered signal; purchases must at least not point the other way).
Pass -> writes the --ok file (JSON). Fail -> removes it. go_final.py uses the candidate only if --ok exists.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _data import load_parquet  # noqa: E402

from rtrec.model import ranker as rk  # noqa: E402
from rtrec.model.policies import RankerPolicy  # noqa: E402
from rtrec.model.util import clean_id, to_epoch  # noqa: E402
from rtrec.offline.replay import state_from_history  # noqa: E402
from rtrec.offline.trainset import session_to_request  # noqa: E402
from rtrec.validate import finalize  # noqa: E402

P_CLICK, P_BUY = 0.95, 0.50


def boot_p(users: np.ndarray, diff: np.ndarray, reps: int = 2000, seed: int = 0) -> float:
    """P(mean diff > 0) resampling shoppers."""
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({"u": users, "d": diff}).groupby("u")["d"].agg(["sum", "count"])
    s, c = df["sum"].to_numpy(), df["count"].to_numpy()
    if len(s) == 0:
        return 0.5
    idx = rng.integers(0, len(s), (reps, len(s)))
    return float(((s[idx].sum(1) / c[idx].sum(1)) > 0).mean())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data_final")
    ap.add_argument("--current", default="artifacts/ranker_v3.txt")
    ap.add_argument("--candidate", default="artifacts/ranker_final.txt")
    ap.add_argument("--ok", default="artifacts/ranker_final.ok")
    a = ap.parse_args()
    ok = Path(a.ok)
    ok.unlink(missing_ok=True)

    dump = load_parquet(a.data)
    hs = pd.read_parquet(Path(a.data) / "holdout" / "sessions.parquet")
    he = pd.read_parquet(Path(a.data) / "holdout" / "events.parquet")
    state, feed = state_from_history(dump.catalog, dump.history_sessions, dump.history_events)
    models = {"current": rk.load(a.current), "candidate": rk.load(a.candidate)}
    pol = RankerPolicy()

    hs = hs.assign(_t=hs["ts"].map(to_epoch)).sort_values("_t")
    he = he.assign(_t=he["ts"].map(to_epoch)).sort_values("_t")
    ev_t, ev = he["_t"].to_numpy(), he.drop(columns="_t").to_dict("records")
    rows, j = [], 0
    for r in hs.to_dict("records"):
        k = int(np.searchsorted(ev_t, r["_t"]))
        if k > j:
            feed.on_events(ev[j:k])
            j = k
        clk, buy = clean_id(r.get("clicked")), clean_id(r.get("purchased"))
        if not clk and not buy:
            continue
        req = session_to_request(r, state)
        hit = {}
        for name, m in models.items():
            state.offline["ranker"] = m
            top = set(finalize(pol.recommend(req, state), state)[:10])
            hit[name] = (int(clk in top) if clk else None, int(buy in top) if buy else None)
        rows.append((str(r["user_id"]), hit["current"], hit["candidate"]))

    def stat(i):
        sel = [(u, c[i], d[i]) for u, c, d in rows if c[i] is not None]
        if not sel:
            return 0, float("nan"), float("nan"), 0.5
        u = np.array([x[0] for x in sel])
        cur, cand = np.array([x[1] for x in sel], float), np.array([x[2] for x in sel], float)
        return len(sel), cur.mean(), cand.mean(), boot_p(u, cand - cur)

    nc, c_cur, c_cand, p_c = stat(0)
    nb, b_cur, b_cand, p_b = stat(1)
    res = {"holdout_requests": len(hs), "with_click": nc, "with_purchase": nb,
           "click_hit": {"current": c_cur, "candidate": c_cand, "P_candidate_better": p_c},
           "purchase_hit": {"current": b_cur, "candidate": b_cand, "P_candidate_better": p_b},
           "rule": f"P_click >= {P_CLICK} and P_purchase >= {P_BUY}"}
    passed = p_c >= P_CLICK and p_b >= P_BUY
    res["passed"] = passed
    print(json.dumps(res, indent=2, default=float))
    if passed:
        ok.write_text(json.dumps(res, default=float))
    return 0 if passed else 3


if __name__ == "__main__":
    sys.exit(main())
