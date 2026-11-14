"""Built-in policies the runtime can always fall back on.

The real models come from A3 (rtrec/model). These two are the runtime's own safety levels:

  level 1  the configured policy (A3's ranker)              -- may raise, hang or be slow
  level 2  RecentViewsPolicy: shopper's recent views, then   -- tiny, no dependencies, trusted
           best sellers (organisers' example shows recently viewed items converting)
  level 3  finalize([], state): best sellers in stock        -- cannot fail

BaselinePolicy reproduces the organisers' baseline (the score's zero) for comparisons.
"""
from __future__ import annotations

from rtrec.contract import State
from rtrec.schemas import Request


class BaselinePolicy:
    name = "baseline"

    def recommend(self, req: Request, state: State) -> list[str]:
        return state.safe_list[:40]


class RecentViewsPolicy:
    name = "fallback-recent"

    def recommend(self, req: Request, state: State) -> list[str]:
        views = [v.get("product_id") for v in reversed(req.get("recent_views") or []) if isinstance(v, dict)]
        return views + state.safe_list[:40]
