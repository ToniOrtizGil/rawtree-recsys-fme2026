"""Shared loader for the A3 scripts: a history dump from a simulator or from the Parquet download."""
from __future__ import annotations

import glob
from pathlib import Path

import pandas as pd

from sim.base import HistoryDump


def make_sim(seed: int, toy: bool = False):
    """A2's simulator (sim/shop.py) when available, else the small toy shop used by A3's tests."""
    if not toy:
        try:
            from sim.shop import make_simulator  # A2
            return make_simulator(seed=seed), "sim.shop"
        except ImportError:
            pass
    from rtrec.offline.toysim import make_simulator
    return make_simulator(seed=seed), "toysim"


def load_parquet(root: str | Path) -> HistoryDump:
    """data/catalog.parquet, data/history_sessions/*.parquet, data/history_events/*.parquet (A2 layout)."""
    root = Path(root)
    read = lambda pat: pd.concat([pd.read_parquet(f) for f in sorted(glob.glob(str(root / pat)))], ignore_index=True)  # noqa: E731
    return HistoryDump(catalog=pd.read_parquet(root / "catalog.parquet"),
                       history_sessions=read("history_sessions/*.parquet"),
                       history_events=read("history_events/*.parquet"))
