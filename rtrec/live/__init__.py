"""Live learning: turns the shop's live tables and our own feedback into `State` (see docs/LIVE.md).

The runtime (A1) owns threads and I/O; it only calls these hooks with the rows it just read:

    from rtrec import live
    live.init(state, catalog_rows)          # once, after loading shop.catalog
    live.seed_history(state, history_events) # optional, once: long-run shares for trend lifts
    live.on_catalog_updates(rows, state)    # shop.catalog_updates   (every <= 0.5 s)
    live.on_events(rows, state)             # shop.events            (~50 rows/s, bursts of thousands)
    live.on_requests(rows, state)           # shop.requests          (optional: enables per-user / cold A/B stats)
    live.on_feedback(rows, state)           # team_NN.feedback

All hooks are incremental, idempotent (duplicates from the cursor overlap are ignored) and safe with
out-of-order rows. They keep their working memory in `state.live["_learner"]` (a LiveLearner).
Calls must come from one thread at a time (A1's single updater thread); policies may read State
concurrently because every published structure is either replaced whole or only item-assigned.
"""
from __future__ import annotations

from rtrec.contract import State

from .learner import LiveLearner

__all__ = ["LiveLearner", "learner", "init", "on_events", "on_catalog_updates", "on_feedback",
           "on_requests", "seed_history"]


def learner(state: State) -> LiveLearner:
    lr = state.live.get("_learner")
    if lr is None:
        lr = state.live["_learner"] = LiveLearner()
    return lr


def init(state: State, catalog_rows) -> None:
    learner(state).init(state, catalog_rows)


def seed_history(state: State, history_events) -> None:
    learner(state).seed_history(state, history_events)


def on_events(rows, state: State) -> None:
    learner(state).on_events(rows, state)


def on_catalog_updates(rows, state: State) -> None:
    learner(state).on_catalog_updates(rows, state)


def on_feedback(rows, state: State) -> None:
    learner(state).on_feedback(rows, state)


def on_requests(rows, state: State) -> None:
    learner(state).on_requests(rows, state)
