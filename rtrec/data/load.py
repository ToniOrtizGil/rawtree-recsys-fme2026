"""Load the history dump (real or synthetic, same layout) into pandas, with parsed time columns.

    h = load_history("data")            # reads data/SOURCE.json to tell real from synthetic
    h.sessions  : history_sessions + t (epoch s), device, region, page, n_views, clicked_slot, purchased_slot
    h.events    : history_events + t
    h.catalog   : catalog + created_t

Nulls: RawTree returns null for "no click" in history and "" in feedback; we normalise both to "".
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


@dataclass
class History:
    catalog: pd.DataFrame
    sessions: pd.DataFrame
    events: pd.DataFrame
    source: dict


def epoch(ts: pd.Series) -> np.ndarray:
    """ISO-8601 strings (or datetimes) -> float epoch seconds."""
    if not len(ts):
        return np.zeros(0)
    dt = pd.to_datetime(pd.Series(ts), utc=True, format="ISO8601")
    # resolution-independent (pandas may parse to ms / us / ns)
    return ((dt - pd.Timestamp(0, tz="UTC")) / pd.Timedelta(seconds=1)).to_numpy(dtype=float)


def _read_dir(d: Path) -> pd.DataFrame:
    files = sorted(d.glob("*.parquet"))
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True) if files else pd.DataFrame()


def _blank(s: pd.Series) -> pd.Series:
    return s.astype(object).where(s.notna(), "").astype(str)


def prepare(catalog: pd.DataFrame, sessions: pd.DataFrame, events: pd.DataFrame, source: dict | None = None) -> History:
    """Add the derived columns every consumer needs (works on sim.history() frames too)."""
    catalog = catalog.copy()
    catalog["created_t"] = epoch(catalog["created_at"])
    s = sessions.copy()
    s["t"] = epoch(s["ts"])
    ctx = s["context"].map(lambda c: c if isinstance(c, dict) else dict(c))
    for k in ("device", "region", "page"):
        s[k] = ctx.map(lambda c: c.get(k))
    s["shown"] = s["shown"].map(list)
    s["propensity"] = s["propensity"].map(lambda p: np.asarray(p, dtype=float))
    s["recent_views"] = s["recent_views"].map(lambda v: [dict(x) for x in v] if v is not None else [])
    s["n_views"] = s["recent_views"].map(len)
    s["clicked"] = _blank(s["clicked"])
    s["purchased"] = _blank(s["purchased"])
    s["clicked_slot"] = [sh.index(c) if c in sh else -1 for sh, c in zip(s["shown"], s["clicked"])]
    s["purchased_slot"] = [sh.index(c) if c in sh else -1 for sh, c in zip(s["shown"], s["purchased"])]
    e = events.copy()
    e["t"] = epoch(e["ts"])
    return History(catalog, s.sort_values("t", kind="stable").reset_index(drop=True),
                   e.sort_values("t", kind="stable").reset_index(drop=True), source or {})


def load_history(data_dir: str | Path = "data") -> History:
    d = Path(data_dir)
    src = json.loads((d / "SOURCE.json").read_text()) if (d / "SOURCE.json").exists() else {"source": "unknown"}
    return prepare(pd.read_parquet(d / "catalog.parquet"), _read_dir(d / "history_sessions"),
                   _read_dir(d / "history_events"), src)


def load_live(data_dir: str | Path = "data") -> dict[str, pd.DataFrame]:
    """data/live/<table>/*.parquet (written by `download_history.py --live` or `sim.dump --live-minutes`)."""
    d = Path(data_dir) / "live"
    out = {}
    for t in ("events", "catalog_updates", "requests", "feedback"):
        if (d / t).exists():
            df = _read_dir(d / t)
            if len(df):
                out[t] = df
    return out


def from_simulator(sim, days: int | None = None) -> History:
    days = days or sim.cfg.history_days
    h = sim.history(days)
    return prepare(h.catalog, h.history_sessions, h.history_events, {"source": "synthetic", "seed": sim.cfg.seed})


def slot_pairs(sessions: pd.DataFrame) -> pd.DataFrame:
    """Long format: one row per (impression, slot): request_id, t, slot (0-based), product_id,
    propensity, clicked, purchased (0/1), plus context columns. ~10 x rows of sessions."""
    n = len(sessions)
    slot = np.tile(np.arange(10), n)
    rep = np.repeat(np.arange(n), 10)
    pid = np.concatenate(sessions["shown"].to_numpy()) if n else np.zeros(0, object)
    prop = np.concatenate(sessions["propensity"].to_numpy()) if n else np.zeros(0)
    cs = sessions["clicked_slot"].to_numpy()[rep]
    ps = sessions["purchased_slot"].to_numpy()[rep]
    out = pd.DataFrame({"request_id": sessions["request_id"].to_numpy()[rep], "t": sessions["t"].to_numpy()[rep],
                        "slot": slot, "product_id": pid, "propensity": prop,
                        "clicked": (cs == slot).astype(int), "purchased": (ps == slot).astype(int)})
    for c in ("user_id", "device", "region", "page", "n_views"):
        if c in sessions:
            out[c] = sessions[c].to_numpy()[rep]
    return out
