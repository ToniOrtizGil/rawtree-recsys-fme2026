"""Write the synthetic history in the same Parquet layout as scripts/download_history.py:

    data/catalog.parquet
    data/history_sessions/<YYYY-MM-DD>.parquet
    data/history_events/<YYYY-MM-DD>.parquet
    data/live/{events,catalog_updates,requests,feedback}/<YYYY-MM-DD>.parquet   (--live-minutes N)

so every loader, notebook and training script runs unchanged on synthetic or real data.

The live part runs the stream for N minutes and answers every request with the baseline (the
starter-kit best sellers), so feedback exists for the forensics notebook (H10, H11).
Usage:  python -m sim.dump --out data --seed 7 [--days 60] [--small] [--live-minutes 120]
Also writes data/SOURCE.json saying where the data came from (never mix them up).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from .params import SimConfig
from .shop import make_simulator


def write_dump(dump, out: Path, source: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    dump.catalog.to_parquet(out / "catalog.parquet", index=False)
    for name in ("history_sessions", "history_events"):
        df: pd.DataFrame = getattr(dump, name)
        d = out / name
        d.mkdir(exist_ok=True)
        for old in d.glob("*.parquet"):
            old.unlink()
        for day, part in df.groupby(df["ts"].str.slice(0, 10), sort=True):
            part.to_parquet(d / f"{day}.parquet", index=False)
    (out / "SOURCE.json").write_text(json.dumps(source, indent=2))


def _write_days(df: pd.DataFrame, d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob("*.parquet"):
        old.unlink()
    for day, part in df.groupby(df["ts"].str.slice(0, 10), sort=True):
        part.to_parquet(d / f"{day}.parquet", index=False)


def write_live(sim, minutes: float, out: Path, step_s: float = 10.0) -> dict:
    """Run the live stream, answer each request with the baseline 1 s after its ts, dump the tables."""
    rows = {"events": [], "catalog_updates": [], "requests": [], "feedback": []}
    t, end = sim.stream_start, sim.stream_start + 60 * minutes
    pending: list[dict] = []
    while t < end:
        t = min(t + step_s, end)
        tk = sim.advance(t)
        for k in ("events", "catalog_updates", "requests"):
            rows[k] += getattr(tk, k)
        pending += tk.requests
        base = [sim.world.pid[i] for i in sim.baseline_items()]
        for r in pending:
            rows["feedback"].append(sim.judge({"request_id": r["request_id"], "items": base, "model_version": "baseline"},
                                              sim._live[r["request_id"]][2] + 1.0))
        pending = []
    live = out / "live"
    for k, v in rows.items():
        if v:
            _write_days(pd.DataFrame(v), live / k)
    return {k: len(v) for k, v in rows.items()}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--small", action="store_true", help="tiny world (tests / quick experiments)")
    ap.add_argument("--live-minutes", type=float, default=0, help="also dump N minutes of live tables")
    a = ap.parse_args(argv)
    cfg = (SimConfig.small() if a.small else SimConfig()).with_(history_days=a.days)
    sim = make_simulator(a.seed, cfg)
    dump = sim.history(a.days)
    write_dump(dump, Path(a.out), {"source": "synthetic", "seed": a.seed, "days": a.days, "small": a.small})
    print(f"wrote {len(dump.catalog)} products, {len(dump.history_sessions)} sessions, "
          f"{len(dump.history_events)} events to {a.out}/")
    if a.live_minutes:
        print("live:", write_live(sim, a.live_minutes, Path(a.out)))


if __name__ == "__main__":
    main()
