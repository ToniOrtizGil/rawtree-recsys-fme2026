"""Train the v2 ranker and export it to artifacts/ranker.txt (+ .json metadata, + metrics).

    python scripts/train_ranker.py --sim --seed 7            # A2's simulator (toy shop if absent)
    python scripts/train_ranker.py --parquet data/            # the real history download (A2 layout)

Steps (rtrec/offline/pipeline.py), chronological 70/20/10 split: artefacts from [0,35 %) and training rows
from [35,70 %) (train), early stopping on [70,80 %) (validation), offline evaluation of
baseline / v1 / v2 on the untouched last 20 % (test) with IPS, SNIPS, DM and DR, split by new/cold/warm
estimates (and, on a simulator, the ground truth next to them).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _data import load_parquet, make_sim  # noqa: E402

from rtrec.model import ranker as rk  # noqa: E402
from rtrec.model.policies import BaselinePolicy, RankerPolicy, SessionHeuristicPolicy  # noqa: E402
from rtrec.offline.pipeline import fit, format_offline, offline_eval, prepare  # noqa: E402
from rtrec.offline.replay import state_from_history  # noqa: E402
from rtrec.offline.streamlog import collect  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--sim", action="store_true")
    src.add_argument("--parquet")
    ap.add_argument("--toy", action="store_true", help="force the toy shop")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--days", type=int, default=None, help="history days to ask the simulator for")
    ap.add_argument("--sample", type=float, default=1.0, help="fraction of training impressions")
    ap.add_argument("--eval-impressions", type=int, default=20000)
    ap.add_argument("--stream-minutes", type=float, default=0,
                    help="simulator only: also serve this many minutes with v1 + exploration and train on the feedback")
    ap.add_argument("--explore", type=float, default=0.2)
    ap.add_argument("--out", default="artifacts/ranker.txt")
    ap.add_argument("--final", action="store_true",
                    help="production refit once the design is validated: same recipe, window moved to the most "
                         "recent data (artefacts [0,50 %), rows [50,100 %] incl. the live stream, no test); "
                         "boosting rounds taken from --rounds-from (the validated run's early stopping)")
    ap.add_argument("--ipw-clip", type=float, default=0.0,
                    help="inverse-propensity row weights min(1/p, clip) against the old carousel's bias (0 = off)")
    ap.add_argument("--rounds-from", default="artifacts/ranker_v3.metrics.json")
    args = ap.parse_args()

    tic = time.time()
    sim = None
    if args.sim:
        sim, kind = make_sim(args.seed, args.toy)
        dump = sim.history(days=args.days) if args.days else sim.history()
        print(f"{kind}: {len(dump.history_sessions)} sessions, {len(dump.history_events)} events")
    else:
        kind, dump = "parquet", load_parquet(args.parquet)
    cuts = dict(a=0.50, v=1.0, b=1.0) if args.final else {}
    fixed = None
    if args.final:
        r = json.loads(Path(args.rounds_from).read_text())["ranker"]
        fixed = (max(50, int(r["click_best_iter"] * 1.1)), max(30, int(r["buy_best_iter"] * 1.1)))  # +10 %: more data
        print(f"final refit with fixed rounds {fixed} from {args.rounds_from}")
    if args.final:
        args.eval_impressions = 0
    p = prepare(dump.catalog, dump.history_sessions, dump.history_events, sample=args.sample, ipw_clip=args.ipw_clip, **cuts)
    if args.stream_minutes and sim is not None:
        # practice-round analogue: serve v1 with exploration on the live stream, learn from the feedback
        state, feed = state_from_history(dump.catalog, dump.history_sessions, dump.history_events)
        Xs, ys, cs, gs = collect(sim, state, feed, SessionHeuristicPolicy(), args.stream_minutes, eps=args.explore,
                                 seed=args.seed)
        p = replace(p, X=np.vstack([p.X, Xs]), y=np.concatenate([p.y, ys]), c=np.concatenate([p.c, cs]),
                    groups=np.concatenate([p.groups, gs + (p.groups.max() + 1 if len(p.groups) else 0)]))
    booster, metrics = fit(p, fixed_rounds=fixed)
    rk.save(booster, args.out, meta={"source": kind, "seed": args.seed, **metrics})
    print(f"saved {args.out}")
    Path(args.out).with_suffix(".metrics.json").write_text(json.dumps({"source": kind, "ranker": metrics}, default=float))
    if not args.eval_impressions:
        return
    rep = offline_eval(p, [BaselinePolicy(), SessionHeuristicPolicy(), RankerPolicy()], booster=booster,
                       sim=sim, max_impressions=args.eval_impressions)
    print(format_offline(rep))
    out = Path(args.out).with_suffix(".metrics.json")
    out.write_text(json.dumps({"source": kind, "seed": args.seed, "ranker": metrics, "offline": rep,
                               "seconds": round(time.time() - tic, 1)}, indent=2, default=float))
    print(f"metrics -> {out}  ({time.time() - tic:.0f}s)")


if __name__ == "__main__":
    main()
