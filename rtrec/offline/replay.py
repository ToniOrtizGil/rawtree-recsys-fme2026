"""Build a `State` from history and keep it updated from a stream, for offline work.

Offline we need the *same* State the live system would have at any moment of the history: to
compute training features "as of" each impression, and to run policies against a simulator. So we
drive A4's live updaters (`rtrec.live`) with the history, exactly as the runtime drives them with
the live tables. One code path for training and serving = no train/serve skew in the live features.

Cost control: replaying 60 days of trends through 2-second snapshots is slow and pointless (trend
half-lives are minutes to hours). Old events (before `full_from`) only update user profiles; the
last hours before the window of interest go through the full live pipeline.
"""
from __future__ import annotations

import pandas as pd

from rtrec import live
from rtrec.contract import State
from rtrec.model.artefacts import build_offline
from rtrec.model.util import to_epoch

REPLAY_SNAPSHOT_S = 60.0   # trend snapshot period in event time during replays (live: 2 s)
WARM_HOURS = 6.0           # hours of full live replay before the window of interest


class LiveFeed:
    """Feeds rows to rtrec.live for one State (same hooks the runtime calls)."""

    def __init__(self, state: State, catalog: pd.DataFrame | list, snapshot_s: float = REPLAY_SNAPSHOT_S):
        self.s = state
        live.init(state, catalog)
        lr = live.learner(state)
        lr.trends.snapshot_every_s = snapshot_s

    def on_events(self, rows) -> None:
        rows = list(rows)
        if rows:
            live.on_events(rows, self.s)

    def on_catalog_updates(self, rows) -> None:
        rows = list(rows)
        if rows:
            live.on_catalog_updates(rows, self.s)

    def on_requests(self, rows) -> None:
        rows = list(rows)
        if rows:
            live.on_requests(rows, self.s)

    def on_feedback(self, rows) -> None:
        rows = list(rows)
        if rows:
            live.on_feedback(rows, self.s)

    def profiles_only(self, rows) -> None:
        """Cheap path for old history: user profiles, nothing else."""
        lr = live.learner(self.s)
        for ev in rows:
            lr.profiles.add(ev, to_epoch(ev["ts"]), self.s)

    def flush(self) -> None:
        live.learner(self.s).flush(self.s)


def state_from_history(catalog: pd.DataFrame, sessions: pd.DataFrame, events: pd.DataFrame,
                       offline: dict | None = None, warm_hours: float = WARM_HOURS) -> tuple[State, LiveFeed]:
    """State at the end of the given history: catalogue, offline artefacts, safe list, profiles, trends."""
    off = offline if offline is not None else build_offline(catalog, sessions, events)
    s = State(offline=off)
    feed = LiveFeed(s, catalog)
    s.safe_list = [p for p in off["best_sellers"] if p in s.available][:200]
    if not events.empty:
        ev = events.assign(_t=events["ts"].map(to_epoch)).sort_values("_t")
        cut = ev._t.iloc[-1] - 3600.0 * warm_hours
        old, recent = ev[ev._t < cut].drop(columns="_t"), ev[ev._t >= cut].drop(columns="_t")
        feed.profiles_only(old.to_dict("records"))
        feed.on_events(recent.to_dict("records"))
        feed.flush()
    return s, feed
