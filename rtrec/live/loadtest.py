"""Throughput and memory of the live hooks over a simulated day.

    python -m rtrec.live.loadtest --hours 24 --rate 50 --burst 20000

Feeds synthetic shop.events at `rate` rows/s of event time (5,000 products, Zipf popularity, a
growing population of shoppers and sessions), catalogue updates and feedback at realistic rates,
plus a burst of `burst` rows every 6 h (a backfill after a restart). Reports CPU ms per 1,000 rows,
the slowest chunk, and peak RSS: memory must plateau, not grow with time.
"""
from __future__ import annotations

import argparse
import random
import resource
import time

from rtrec import live
from rtrec.contract import State


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def run(hours: float = 24.0, rate: float = 50.0, burst: int = 20_000, chunk: int = 2000,
        n_products: int = 5000, seed: int = 0, t0: float = 1_794_000_000.0, report_every_h: float = 3.0):
    rng = random.Random(seed)
    s = State()
    live.init(s, [{"product_id": f"p{i}", "category": f"c{i % 10}", "subcategory": f"s{i % 90}",
                   "brand": f"b{i % 300}", "price_eur": 5.0 + i % 200, "description": "soft cotton shirt",
                   "created_at": "", "is_active": True} for i in range(n_products)])
    weights = [1.0 / (i + 1) ** 0.9 for i in range(n_products)]
    pids = [f"p{i}" for i in range(n_products)]
    lr = live.learner(s)
    sessions = [(f"u{i}", f"x{i}") for i in range(2000)]
    nuser = [2000]
    out = []

    def make_events(t_start: float, n: int, dt: float):
        rows = []
        picks = rng.choices(pids, weights, k=n)
        for i in range(n):
            if rng.random() < 0.03:                      # session ends, a new shopper arrives
                nuser[0] += 1
                sessions[rng.randrange(len(sessions))] = (f"u{nuser[0]}", f"x{nuser[0]}")
            uid, sid = sessions[rng.randrange(len(sessions))]
            r = rng.random()
            rows.append({"ts": t_start + i * dt, "event_type": "view" if r < 0.9 else ("add_to_cart" if r < 0.97 else "purchase"),
                         "user_id": uid, "session_id": sid, "product_id": picks[i], "price_eur": 10.0,
                         "device": rng.choice(("web", "ios", "android")), "region": f"R{rng.randrange(17)}"})
        return rows

    cpu = 0.0
    rows_done = 0
    worst_ms = 0.0
    t = t0
    end = t0 + hours * 3600
    step = chunk / rate                                   # event-time seconds per chunk
    next_report, next_burst = t0 + report_every_h * 3600, t0 + 6 * 3600
    nfb = 0
    while t < end:
        evs = make_events(t, chunk, 1.0 / rate)
        ups = [{"product_id": rng.choice(pids), "category": "c", "subcategory": "s1", "brand": "b",
                "price_eur": float(rng.randrange(5, 50)), "description": "", "created_at": "", "is_active": True,
                "ts": t + rng.random() * step, "change": rng.choice(("price_change", "out_of_stock", "back_in_stock"))}
               for _ in range(int(step * 0.2))]
        fbs = []
        for _ in range(int(step * 3)):                    # ~3 answers/s
            nfb += 1
            shown = rng.sample(pids[:500], 10)
            b = shown[0] if rng.random() < 0.05 else ""
            fbs.append({"request_id": f"r{nfb}", "ts": t, "shown": shown, "served_by": "team",
                        "fallback_reason": "", "latency_ms": 900, "clicked": b, "purchased": b, "revenue_eur": 0})
        batches = [(live.on_events, evs), (live.on_catalog_updates, ups), (live.on_feedback, fbs)]
        if t >= next_burst:
            next_burst += 6 * 3600
            batches.append((live.on_events, make_events(t - 600, burst, 600 / burst)))
        for fn, rows in batches:
            for i in range(0, len(rows), chunk):
                c0 = time.process_time()
                fn(rows[i:i + chunk], s)
                dt = time.process_time() - c0
                cpu += dt
                worst_ms = max(worst_ms, 1000 * dt)
                rows_done += len(rows[i:i + chunk])
        t += step
        if t >= next_report:
            next_report += report_every_h * 3600
            out.append({"hour": (t - t0) / 3600, "rows": rows_done, "cpu_ms_per_1k": 1000 * cpu / rows_done * 1000,
                        "worst_chunk_ms": worst_ms, "rss_mb": _rss_mb(), "users": len(s.users),
                        "trend_keys": len(lr.trends.p_long.d), "sessions": len(lr.coview.sessions)})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--rate", type=float, default=50)
    ap.add_argument("--burst", type=int, default=20_000)
    a = ap.parse_args(argv)
    for r in run(a.hours, a.rate, a.burst):
        print("  ".join(f"{k}={v:.1f}" if isinstance(v, float) else f"{k}={v}" for k, v in r.items()))


if __name__ == "__main__":
    main()
