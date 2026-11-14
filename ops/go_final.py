"""Switch production from the practice A/B to the single final model (Sunday ~08:15 CET, before the freeze).

    python ops/go_final.py --ab-since <start> --ab-until <decision time> [--dry-run]

1. Decide the policy from the overnight A/B on a pre-registered window (see ab_choice): v2 by default,
   v1 or baseline only if they beat it with one-sided z > 1.645.
2. Pick the model file: artifacts/ranker_final.txt only if it passed the hold-out gate
   (scripts/compare_models.py wrote artifacts/ranker_final.ok) and loads; else the current model.
3. Stop the running server (SIGTERM to the supervisor: it flushes and exits) and start the final one
   detached (nohup), no A/B and no exploration. Wait for "policy ... live" in the log.
Anything that fails before step 3 leaves the current server untouched.
"""
from __future__ import annotations

import argparse
import math
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))
os.chdir(ROOT)
LOG = ROOT / "logs" / "serve-primary.log"


def say(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} go_final: {msg}"
    print(line, flush=True)
    with open(ROOT / "logs" / "go_final.log", "a") as f:
        f.write(line + "\n")


def ab_choice(since: str, until: str) -> str:
    """Pre-registered rule (rtrec/live/decide.py), applied once on the window [since, until):
    simplest arm unless a more complex one wins with P_beta >= 0.95 and P_boot(users) >= 0.90.
    Rows served by OUR fallback are excluded. Returns 'baseline' | 'v1' | 'v2'."""
    try:
        from ab_report import fetch
        from rtrec.live.ab import ABTest
        from rtrec.live.abfactory import ARMS, SALT
        from rtrec.live.decide import decide
        rows = fetch(since, until)
        t = ABTest(arms=ARMS, salt=SALT)
        arms: dict[str, list] = {}
        for r in rows:
            if r["by"] == "team" and str(r.get("mv", "")).startswith("fallback"):
                continue
            arms.setdefault(t.assign(r["rid"]), []).append((r.get("uid") or r["rid"], int(bool(r["bought"]))))
        say("A/B " + " · ".join(f"{a} {100*sum(y for _, y in v)/len(v):.2f}/100 (n={len(v)})" for a, v in arms.items())
            + f"  [{since} -> {until}]")
        name = decide(arms, log=say)
        return {"baseline": "baseline", "v1-session-heuristic": "v1"}.get(name, "v2")
    except Exception as e:  # noqa: BLE001
        say(f"A/B read failed ({e}); keeping v2 (offline winner)")
        return "v2"


def pick_model() -> str | None:
    """First model file that loads and scores a request in < 50 ms."""
    from rtrec.model import ranker as rk
    cur = os.environ.get("RTREC_CURRENT_MODEL", "artifacts/ranker_v3.txt")
    for path in ("artifacts/ranker_final.txt", cur):
        try:
            if not Path(path).exists():
                continue
            if path.endswith("ranker_final.txt") and not Path("artifacts/ranker_final.ok").exists():
                say("refit did not pass the hold-out gate (no artifacts/ranker_final.ok): keeping the current model")
                continue
            m = rk.load(path)
            import numpy as np

            from rtrec.model.features import FEATURES
            t0 = time.perf_counter()
            p = m.predict(np.zeros((20, len(FEATURES)), np.float32))
            ms = 1000 * (time.perf_counter() - t0)
            if p.shape == (20,) and np.isfinite(p).all() and ms < 50:
                say(f"model {path} OK ({ms:.1f} ms / 20 rows)")
                return path
            say(f"model {path} rejected (shape {p.shape}, {ms:.1f} ms)")
        except Exception as e:  # noqa: BLE001
            say(f"model {path} rejected: {e}")
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ab-since", required=True)
    ap.add_argument("--ab-until", required=True, help="pre-registered end of the decision window")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    choice = ab_choice(a.ab_since, a.ab_until)
    env = dict(os.environ)
    if choice == "baseline":
        env["RTREC_POLICY"] = "rtrec.model.policies:BaselinePolicy"
    elif choice == "v1":
        env["RTREC_POLICY"] = "rtrec.model.policies:SessionHeuristicPolicy"
    else:
        model = pick_model()
        if model is None:
            say("no usable v2 model: falling back to v1")
            env["RTREC_POLICY"] = "rtrec.model.policies:SessionHeuristicPolicy"
        else:
            env["RTREC_POLICY"], env["RTREC_MODEL"] = "rtrec.model.policies:RankerPolicy", model
    for k in ("RTREC_EXPLORE", "RTREC_SERVED_LOG", "RTREC_AB_ARMS", "RTREC_AB_SALT"):
        env.pop(k, None)
    say(f"final policy: {env['RTREC_POLICY']} model={env.get('RTREC_MODEL', '-')}")
    if a.dry_run:
        return 0

    pids = subprocess.run(["pgrep", "-f", "rtrec.runtime.supervisor"], capture_output=True, text=True).stdout.split()
    for pid in pids:
        os.kill(int(pid), signal.SIGTERM)
    for _ in range(30):
        if not subprocess.run(["pgrep", "-f", "rtrec.runtime"], capture_output=True).stdout:
            break
        time.sleep(0.5)
    else:
        for pid in subprocess.run(["pgrep", "-f", "rtrec.runtime"], capture_output=True, text=True).stdout.split():
            os.kill(int(pid), signal.SIGKILL)
    say(f"stopped old server {pids}")
    mark = LOG.stat().st_size if LOG.exists() else 0
    subprocess.Popen(["nohup", "ops/run.sh"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    for _ in range(240):
        time.sleep(1)
        if LOG.exists():
            with LOG.open() as f:
                f.seek(mark)
                tail = f.read()
            if re.search(r"policy \S+ live$", tail, re.M):
                say("new server: policy live")
                return 0
            if "policy factory failed" in tail or "Traceback" in tail:
                say("new server: policy FAILED (serving its fallback) - check logs/serve-primary.log")
                return 2
    say("new server: policy not live after 240 s - check logs/serve-primary.log")
    return 1


if __name__ == "__main__":
    sys.exit(main())
