"""Write the synthetic history in the same Parquet layout as scripts/download_history.py:

    data/catalog.parquet
    data/history_sessions/<YYYY-MM-DD>.parquet
    data/history_events/<YYYY-MM-DD>.parquet

so every loader, notebook and training script runs unchanged on synthetic or real data.
Usage:  python -m sim.dump --out data --seed 7 [--days 60] [--small]
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


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--small", action="store_true", help="tiny world (tests / quick experiments)")
    a = ap.parse_args(argv)
    cfg = (SimConfig.small() if a.small else SimConfig()).with_(history_days=a.days)
    sim = make_simulator(a.seed, cfg)
    dump = sim.history(a.days)
    write_dump(dump, Path(a.out), {"source": "synthetic", "seed": a.seed, "days": a.days, "small": a.small})
    print(f"wrote {len(dump.catalog)} products, {len(dump.history_sessions)} sessions, "
          f"{len(dump.history_events)} events to {a.out}/")


if __name__ == "__main__":
    main()
