"""Per-arm results of the live A/B (rtrec.live.abfactory) from the organisers' feedback.

    python ops/ab_report.py --since 2026-11-14T17:00:00Z [--until ...]

The arm is recomputed from request_id with the same salt (intention-to-treat: fallbacks count against
the arm that caused them). Purchases/100 with 95 % CI, new-user split, and the z-test vs baseline.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtrec.live.ab import ABTest  # noqa: E402
from rtrec.live.abfactory import ARMS, SALT  # noqa: E402

API = "https://api.rawtree.com/v1/query"


def query(sql: str, team: str, key: str) -> list[dict]:
    r = requests.post(API, params={"database": team}, timeout=60,
                      headers={"Authorization": f"Bearer {key}"}, json={"sql": sql, "format": "JSONEachRow"})
    r.raise_for_status()
    return [json.loads(x) for x in r.text.splitlines() if x.strip()]


def _env() -> dict:
    env = dict(os.environ)
    if Path(".env").exists():
        for line in Path(".env").read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    return env


def fetch(since: str, until: str) -> list[dict]:
    """One row per answered request in [since, until): arm inputs + outcome + shopper."""
    env = _env()
    team, key = env["TEAM_ID"], env["RAWTREE_API_KEY"]
    s, u = since.replace("T", " ").rstrip("Z"), until.replace("T", " ").rstrip("Z")
    return query(f"""
        SELECT f.request_id::String AS rid, f.purchased::String AS bought, f.served_by::String AS by,
               f.clicked::String AS clk, f.model_version::String AS mv,
               r.is_new_user::Bool AS new, r.uid AS uid
        FROM {team}.feedback AS f
        LEFT JOIN (SELECT request_id::String AS rid, is_new_user, user_id::String AS uid FROM shop.requests
                   WHERE ts::DateTime64(3) >= '{s}' AND ts::DateTime64(3) < '{u}') AS r
          ON f.request_id::String = r.rid
        WHERE f.ts::DateTime64(3) >= '{s}' AND f.ts::DateTime64(3) < '{u}'""", team, key)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", default="2100-01-01T00:00:00Z")
    a = ap.parse_args()
    env = dict(os.environ)
    if Path(".env").exists():
        for line in Path(".env").read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    fb = fetch(a.since, a.until)
    t = ABTest(arms=ARMS, salt=SALT)
    agg = {n: {"n": 0, "buys": 0, "fallback": 0, "n_new": 0, "buys_new": 0, "clicks": 0, "own_fb": 0} for n, _ in ARMS}
    for row in fb:
        g = agg[t.assign(row["rid"])]
        b = bool(row["bought"])
        g["n"] += 1
        g["buys"] += b
        g["fallback"] += row["by"] != "team"
        g["clicks"] += bool(row.get("clk"))
        g["own_fb"] += row["by"] == "team" and str(row.get("mv", "")).startswith("fallback")
        if row.get("new"):
            g["n_new"] += 1
            g["buys_new"] += b
    base = agg.get("baseline") or next(iter(agg.values()))  # first arm is the reference
    pb = base["buys"] / base["n"] if base["n"] else 0
    print(f"feedback rows: {len(fb)}  ({a.since} -> {a.until})")
    print(f"{'arm':22s} {'n':>6s} {'buys':>5s} {'buy/100':>8s} {'ci95':>15s} {'new buy/100':>11s} {'fallback%':>9s} {'ownfb%':>7s} {'clk/100':>7s} {'z vs ref':>9s}")
    for name, g in agg.items():
        n = g["n"] or 1
        p = g["buys"] / n
        se = math.sqrt(p * (1 - p) / n)
        pn = 100 * g["buys_new"] / g["n_new"] if g["n_new"] else float("nan")
        se_d = math.sqrt(se ** 2 + pb * (1 - pb) / (base["n"] or 1))
        z = (p - pb) / se_d if se_d and g is not base else float("nan")
        print(f"{name:22s} {g['n']:6d} {g['buys']:5d} {100*p:8.2f} "
              f"[{100*(p-1.96*se):5.2f},{100*(p+1.96*se):5.2f}] {pn:11.2f} {100*g['fallback']/n:9.2f} {100*g['own_fb']/n:7.2f} {100*g['clicks']/n:7.2f} {z:9.2f}")
    # power: requests per arm needed to detect +30 % / +50 % over the reference (alpha 5 % two-sided, power 80 %)
    if pb > 0:
        need = lambda lift: int(7.85 * (pb * (1 - pb) + pb * (1 + lift) * (1 - pb * (1 + lift))) / (pb * lift) ** 2)  # noqa: E731
        print(f"needed per arm: +50 % -> {need(0.5):,}  · +30 % -> {need(0.3):,}  (have ~{base['n']:,}). "
              "Decide once, at the pre-registered time; earlier looks are for health only.")
    from rtrec.live.decide import decide
    arms: dict[str, list] = {}
    for row in fb:
        if row["by"] == "team" and str(row.get("mv", "")).startswith("fallback"):
            continue
        arms.setdefault(t.assign(row["rid"]), []).append((row.get("uid") or row["rid"], int(bool(row["bought"]))))
    print("pre-registered rule (Beta-Binomial + user bootstrap), if the window ended now ->", decide(arms))
    if any(g["own_fb"] for g in agg.values()):
        print("WARNING: some answers came from OUR fallback (model not live) -> those rows are not a model test")


if __name__ == "__main__":
    main()
