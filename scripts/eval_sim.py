"""Score baseline / v1 / v2 on the simulator with ground truth, and check the off-policy estimators.

    python scripts/eval_sim.py --seed 7 --minutes 20                 # paired policy scores (overall / cold)
    python scripts/eval_sim.py --seed 7 --check-estimators 30        # + IPS/SNIPS/DM/DR vs truth

Paired: every request is answered by every policy on the same State, so differences between policies
are not blurred by different shoppers (the master's scripts/arbiter.py runs one policy per simulator
instead, which is the official procedure for promotion). Writes artifacts/eval_sim.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _data import make_sim  # noqa: E402

from rtrec.model import ranker as rk  # noqa: E402
from rtrec.model.policies import BaselinePolicy, RankerPolicy, SessionHeuristicPolicy  # noqa: E402
from rtrec.offline.estimator_check import PopularityLogger, check, format_check  # noqa: E402
from rtrec.offline.replay import state_from_history  # noqa: E402
from rtrec.offline.simeval import evaluate_on_sim, format_report  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--toy", action="store_true")
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--minutes", type=float, default=20)
    ap.add_argument("--warmup-min", type=float, default=5)
    ap.add_argument("--max-requests", type=int, default=None)
    ap.add_argument("--model", default="artifacts/ranker.txt")
    ap.add_argument("--check-estimators", type=float, default=0, help="minutes of logged stream for the check")
    ap.add_argument("--out", default="artifacts/eval_sim.json")
    args = ap.parse_args()

    sim, kind = make_sim(args.seed, args.toy)
    dump = sim.history(days=args.days) if args.days else sim.history()
    state, feed = state_from_history(dump.catalog, dump.history_sessions, dump.history_events)
    booster = rk.load(args.model) if Path(args.model).exists() else None
    if booster is not None:
        state.offline["ranker"] = booster
    print(f"{kind} seed={args.seed}: model={'yes' if booster else 'NO (v2 falls back to v1)'}")
    policies = [BaselinePolicy(), SessionHeuristicPolicy(), RankerPolicy()]
    result = {"sim": kind, "seed": args.seed}
    if args.warmup_min:  # let the live state (trends, profiles) fill before scoring
        evaluate_on_sim(sim, state, feed, [], minutes=args.warmup_min, oracle=False, judge_with=None)
    rep = evaluate_on_sim(sim, state, feed, policies, minutes=args.minutes, max_requests=args.max_requests,
                          judge_with=2)
    print(format_report(rep))
    result["scores"] = rep
    if args.check_estimators:
        logger = PopularityLogger(state.offline["best_sellers"], seed=args.seed)
        chk = check(sim, state, feed, policies, logger, minutes=args.check_estimators, booster=booster)
        print(format_check(chk))
        result["estimator_check"] = chk
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2, default=float))
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
