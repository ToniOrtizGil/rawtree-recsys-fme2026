"""Pure metric functions over feedback records. No I/O, so they are easy to test and to explain.

Every number here has a SQL twin in dashboard/queries.py; tests check both agree on the same rows.
Definitions follow the organisers:
- valid & on time  = feedback.served_by == "team" (otherwise fallback_reason says missing / late / invalid)
- conversion       = purchases per 100 requests (observed; the organisers score the expected value, which
                     matches on average)
- cold request     = new shopper, or a recent view of a product added during the event
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

LIGHT_THRESHOLD_PCT = 90.0      # organisers' green light
LIGHT_WINDOW_S = 300            # ... over the last 5 minutes


def to_epoch(ts) -> float:
    """ISO-8601 (with or without Z) or epoch number -> epoch seconds (UTC)."""
    if ts is None or ts == "":
        return 0.0
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, datetime):
        dt = ts
    else:
        s = str(ts).strip().replace(" ", "T")
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def to_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass(slots=True)
class FB:
    """One feedback row, compacted, joined with what we know about its request."""
    request_id: str
    ts: float
    served_by: str
    reason: str
    latency_ms: float
    clicked: str
    purchased: str
    revenue: float
    shown: tuple[str, ...]
    version: str | None = None      # model_version from our submission (None = not resolved yet)
    cold: bool | None = None        # None = request not seen by the dashboard

    @property
    def ok(self) -> bool:
        return self.served_by == "team" and not self.reason

    @classmethod
    def from_row(cls, r: dict) -> "FB":
        return cls(
            request_id=str(r.get("request_id", "")),
            ts=to_epoch(r.get("ts")),
            served_by=str(r.get("served_by") or ""),
            reason=str(r.get("fallback_reason") or ""),
            latency_ms=float(r.get("latency_ms") or 0.0),
            clicked=str(r.get("clicked") or ""),
            purchased=str(r.get("purchased") or ""),
            revenue=float(r.get("revenue_eur") or 0.0),
            shown=tuple(r.get("shown") or ()),
        )


def window(rows: list[FB], now: float, seconds: float) -> list[FB]:
    lo = now - seconds
    return [r for r in rows if lo < r.ts <= now]


def percentile(values: list[float], q: float) -> float | None:
    """Linear interpolation between closest ranks (numpy's default, same as ClickHouse quantileExact-ish)."""
    if not values:
        return None
    v = sorted(values)
    pos = (len(v) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95 % Wilson interval for a proportion, returned in the same unit as the rate (per 100)."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (100 * max(0.0, c - h), 100 * min(1.0, c + h))


def reliability(rows: list[FB]) -> dict:
    """Share of answers valid and on time, plus why the fallback served the rest."""
    n = len(rows)
    ok = sum(r.ok for r in rows)
    reasons = Counter(r.reason or "fallback?" for r in rows if not r.ok)
    return {"n": n, "ok": ok, "ok_pct": (100 * ok / n) if n else None,
            "reasons": {k: reasons.get(k, 0) for k in ("missing", "late", "invalid")} | (
                {"other": reasons["fallback?"]} if reasons.get("fallback?") else {})}


def light(rows_5m: list[FB], min_n: int = 20) -> str:
    """Our estimate of the organisers' light: green / red, or unknown if too few answers to judge."""
    r = reliability(rows_5m)
    if r["n"] < min_n:
        return "unknown"
    return "green" if r["ok_pct"] > LIGHT_THRESHOLD_PCT else "red"


def latency(rows: list[FB]) -> dict:
    """Organisers' latency (ts of request -> our row visible). Missing answers have no latency."""
    vals = [r.latency_ms for r in rows if r.reason != "missing" and r.latency_ms > 0]
    return {"n": len(vals), "p50": percentile(vals, 0.5), "p95": percentile(vals, 0.95),
            "p99": percentile(vals, 0.99), "max": max(vals) if vals else None}


def conversion(rows: list[FB]) -> dict:
    n = len(rows)
    k = sum(1 for r in rows if r.purchased)
    c = sum(1 for r in rows if r.clicked)
    lo, hi = wilson(k, n)
    return {"n": n, "purchases": k, "p100": (100 * k / n) if n else None, "ci": [lo, hi],
            "ctr_pct": (100 * c / n) if n else None,
            "revenue_per_req": (sum(r.revenue for r in rows) / n) if n else None}


def by_version(rows: list[FB]) -> list[dict]:
    """Conversion per model_version, each split cold / warm. Fallback-served rows form their own group
    (whatever we submitted, the shopper saw the organisers' best sellers)."""
    groups: dict[str, list[FB]] = {}
    for r in rows:
        key = "(fallback)" if not r.ok else (r.version or "(unresolved)")
        groups.setdefault(key, []).append(r)
    out = []
    for key, g in groups.items():
        row = {"model_version": key, **conversion(g)}
        row["cold"] = conversion([r for r in g if r.cold is True])
        row["warm"] = conversion([r for r in g if r.cold is False])
        out.append(row)
    out.sort(key=lambda d: (d["model_version"].startswith("("), -d["n"]))
    return out


def segments(rows: list[FB]) -> dict:
    return {"cold": conversion([r for r in rows if r.cold is True]),
            "warm": conversion([r for r in rows if r.cold is False]),
            "unknown": conversion([r for r in rows if r.cold is None])}


def per_minute(rows: list[FB], now: float, minutes: int = 60) -> list[dict]:
    """One point per minute (oldest first): answers, % ok, p95 latency, purchases/100, and purchases/100 over
    the trailing 5 minutes (one minute is only ~180 requests: too noisy to read a conversion trend)."""
    start = (math.floor(now / 60) - minutes + 1) * 60
    buckets: dict[int, list[FB]] = {}
    for r in rows:
        if r.ts >= start:
            buckets.setdefault(int((r.ts - start) // 60), []).append(r)
    out = []
    for i in range(minutes):
        g = buckets.get(i, [])
        rel, lat, conv = reliability(g), latency(g), conversion(g)
        roll = conversion([r for j in range(max(0, i - 4), i + 1) for r in buckets.get(j, [])])
        out.append({"t": start + 60 * i, "n": rel["n"], "ok_pct": rel["ok_pct"], "p95": lat["p95"],
                    "p100": conv["p100"], "p100_5m": roll["p100"]})
    return out
