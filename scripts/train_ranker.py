"""Train the v2 ranker and export it to artifacts/ranker.txt (+ .json metadata, + metrics).

    python scripts/train_ranker.py --sim --seed 7            # A2's simulator (toy shop if absent)
    python scripts/train_ranker.py --parquet data/            # the real history download (A2 layout)

Steps (rtrec/offline/pipeline.py): artefacts from the first half of the history; training rows from
the next 35 % with features as of each impression; early stopping on the last part of that window;
then an offline evaluation of baseline / v1 / v2 on the final 15 % with IPS, SNIPS, DM and DR
estimates (and, on a simulator, the ground truth next to them).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _data import load_parquet, make_sim  # noqa: E402

from rtrec.model import ranker as rk  # noqa: E402
from rtrec.model.policies import BaselinePolicy, RankerPolicy, SessionHeuristicPolicy  # noqa: E402
from rtrec.offline.pipeline import fit, format_offline, offline_eval, prepare  # noqa: E402


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
    ap.add_argument("--out", default="artifacts/ranker.txt")
    args = ap.parse_args()

    tic = time.time()
    sim = None
    if args.sim:
        sim, kind = make_sim(args.seed, args.toy)
        dump = sim.history(days=args.days) if args.days else sim.history()
        print(f"{kind}: {len(dump.history_sessions)} sessions, {len(dump.history_events)} events")
    else:
        kind, dump = "parquet", load_parquet(args.parquet)
    p = prepare(dump.catalog, dump.history_sessions, dump.history_events, sample=args.sample)
    booster, metrics = fit(p)
    rk.save(booster, args.out, meta={"source": kind, "seed": args.seed, **metrics})
    print(f"saved {args.out}")
    rep = offline_eval(p, [BaselinePolicy(), SessionHeuristicPolicy(), RankerPolicy()], booster=booster,
                       sim=sim, max_impressions=args.eval_impressions)
    print(format_offline(rep))
    out = Path(args.out).with_suffix(".metrics.json")
    out.write_text(json.dumps({"source": kind, "seed": args.seed, "ranker": metrics, "offline": rep,
                               "seconds": round(time.time() - tic, 1)}, indent=2, default=float))
    print(f"metrics -> {out}  ({time.time() - tic:.0f}s)")


if __name__ == "__main__":
    main()
