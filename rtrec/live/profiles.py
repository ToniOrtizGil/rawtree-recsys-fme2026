"""Live shopper profiles and live co-views, both fed by shop.events.

Profiles: the final round brings new shoppers whose only history is what they do during the event,
so every view / cart / purchase is appended to `state.users[user_id]` (the contract's UserProfile),
kept sorted by ts (out-of-order safe) and capped so memory stays flat.

Co-views: two products viewed in the same session within `window_s` are neighbours. Offline
co-views (A2/A3) cannot know products created during the event; these live counts can, which is
what makes new products reachable from the session within minutes. Counts are decayed so the
neighbourhood follows trends. Published as immutable tuples per product (lookup-only dict).
"""
from __future__ import annotations

import bisect
import heapq
import math
from dataclasses import dataclass, field

from rtrec.contract import State, UserProfile

_LIST = {"view": "views", "add_to_cart": "carts", "purchase": "purchases"}


@dataclass
class ProfileTracker:
    max_per_list: int = 200

    def add(self, ev: dict, t: float, state: State) -> None:
        uid = ev.get("user_id")
        attr = _LIST.get(ev.get("event_type"))
        if not uid or attr is None:
            return
        prof = state.users.get(uid)
        if prof is None:
            prof = state.users[uid] = UserProfile()
        _insert(getattr(prof, attr), (t, str(ev["product_id"])), self.max_per_list)

    def add_carousel(self, uid: str, t: float, clicked: str, purchased: str, state: State) -> None:
        """Carousel outcome from our feedback (the events table may not include carousel purchases)."""
        prof = state.users.get(uid)
        if prof is None:
            prof = state.users[uid] = UserProfile()
        prof.carousel_shown += 1
        if clicked:
            _insert(prof.carousel_clicks, (t, clicked), self.max_per_list)
        if purchased and (t, purchased) not in prof.purchases:
            _insert(prof.purchases, (t, purchased), self.max_per_list)


def _insert(lst: list, item: tuple, cap: int) -> None:
    if not lst or item >= lst[-1]:
        lst.append(item)
    else:
        bisect.insort(lst, item)
    if len(lst) > cap:
        del lst[: len(lst) - cap]


@dataclass
class CoviewTracker:
    window_s: float = 30 * 60
    last_k: int = 5                  # pair a view with the session's last k distinct views
    half_life_s: float = 6 * 3600
    top_k: int = 30
    max_sessions: int = 200_000
    co: dict[str, dict[str, list]] = field(default_factory=dict)   # a -> b -> [value, t]
    sessions: dict[str, list[tuple[float, str]]] = field(default_factory=dict)
    dirty: set = field(default_factory=set)

    def add(self, ev: dict, t: float) -> None:
        if ev.get("event_type") != "view":
            return
        sid = ev.get("session_id") or ev.get("user_id")
        if not sid:
            return
        b = str(ev["product_id"])
        hist = self.sessions.get(sid)
        if hist is None:
            if len(self.sessions) >= self.max_sessions:   # drop the oldest-inserted session
                self.sessions.pop(next(iter(self.sessions)))
            hist = self.sessions[sid] = []
        for (ta, a) in hist[-self.last_k:]:
            if a != b and abs(t - ta) <= self.window_s:
                self._bump(a, b, t)
                self._bump(b, a, t)
        if not any(p == b for _, p in hist[-self.last_k:]):
            hist.append((t, b))
            if len(hist) > 2 * self.last_k:
                del hist[: len(hist) - self.last_k]

    def _bump(self, a: str, b: str, t: float) -> None:
        k = math.log(2) / self.half_life_s
        nb = self.co.setdefault(a, {})
        cur = nb.get(b)
        if cur is None:
            nb[b] = [1.0, t]
        elif t >= cur[1]:
            cur[0] = cur[0] * math.exp(-(t - cur[1]) * k) + 1.0
            cur[1] = t
        else:
            cur[0] += math.exp(-(cur[1] - t) * k)
        self.dirty.add(a)
        if len(nb) > 4 * self.top_k:                  # keep neighbour lists short
            keep = heapq.nlargest(2 * self.top_k, nb.items(), key=lambda kv: kv[1][0])
            self.co[a] = dict(keep)

    def publish(self, state: State, now: float) -> None:
        out = state.live.setdefault("coview", {})
        k = math.log(2) / self.half_life_s
        for a in self.dirty:
            nb = self.co.get(a) or {}
            scored = ((b, v * math.exp(-max(now - tb, 0.0) * k)) for b, (v, tb) in nb.items())
            out[a] = tuple(heapq.nlargest(self.top_k, scored, key=lambda x: x[1]))
        self.dirty.clear()
