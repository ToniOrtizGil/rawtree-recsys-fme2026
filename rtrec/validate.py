"""Last line of defence: every answer that leaves the process goes through finalize().

An invalid answer (wrong length, duplicates, one inactive or out-of-stock product) is replaced by
the organisers' fallback and costs reliability. finalize() never fails: it repairs.
Owner: master session.
"""
from __future__ import annotations

from collections.abc import Iterable

from .contract import State
from .schemas import N_ITEMS


def finalize(items: Iterable[str] | None, state: State, n: int = N_ITEMS) -> list[str]:
    """Keep the policy's order, drop duplicates and unavailable ids, pad from safe_list, then from
    anything available. Returns exactly n ids when at least n products are available."""
    out: list[str] = []
    seen: set[str] = set()
    avail = state.available

    def take(pids: Iterable[str]) -> bool:
        for p in pids:
            if p not in seen and p in avail:
                seen.add(p)
                out.append(p)
                if len(out) == n:
                    return True
        return False

    if take(items or ()) or take(state.safe_list) or take(sorted(avail)):
        return out
    return out  # fewer than n products available: nothing valid exists anyway


def is_valid(items: list[str], state: State, n: int = N_ITEMS) -> bool:
    return len(items) == n and len(set(items)) == n and all(p in state.available for p in items)
