"""Snapshot / restore of everything the live layer learnt (trends, profiles, co-views, calibration,
A/B counts), so a restart is warm in seconds instead of re-learning for hours.

A1's runtime already pickles `state.users` and `state.live` (which contains the LiveLearner) every
60 s; these helpers do the same for standalone use and are what the tests exercise:

    blob = live.persist.dumps(state)          # call on the thread that runs the hooks
    live.persist.loads(blob, state)           # before serving; then resume the cursors

Call dumps() on the hook thread (or between batches): pickling while a hook mutates a dict can
fail with "dictionary changed size", which A1's snapshotter already treats as "try next time".
"""
from __future__ import annotations

import os
import pickle
from pathlib import Path

from rtrec.contract import State

VERSION = 1


def dumps(state: State) -> bytes:
    lr = state.live.get("_learner")
    lock = getattr(lr, "_lock", None)
    if lock is not None:
        with lock:
            return pickle.dumps({"v": VERSION, "live": state.live, "users": state.users},
                                protocol=pickle.HIGHEST_PROTOCOL)
    return pickle.dumps({"v": VERSION, "live": state.live, "users": state.users},
                        protocol=pickle.HIGHEST_PROTOCOL)


def loads(blob: bytes, state: State) -> None:
    snap = pickle.loads(blob)
    if snap.get("v") != VERSION:
        raise ValueError(f"live snapshot version {snap.get('v')} != {VERSION}")
    state.live = snap["live"]
    state.users = snap["users"]


def save(state: State, path: str | Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = dumps(state)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, path)          # atomic: a crash mid-write never leaves a broken snapshot
    return len(blob)


def load(state: State, path: str | Path) -> bool:
    path = Path(path)
    if not path.exists():
        return False
    loads(path.read_bytes(), state)
    return True
