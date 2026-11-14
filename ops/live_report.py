"""What the organisers measured for our answers, from team.feedback (read-only, aggregated in RawTree SQL).

    python ops/live_report.py --minutes 10          # last 10 minutes of feedback
    python ops/live_report.py --since 2026-11-14T12:26:00Z --until 2026-11-14T12:35:00Z

latency_ms in feedback is the organisers' clock: request ts -> their poll that saw our answer. It is the
only latency that counts for the 3 s rule, so this is the number to watch, not our own ack latency.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

sys.path.insert(0, ".")
from rtrec.runtime.clock import parse_ts, to_sql_dt  # noqa: E402
from rtrec.stores.rawtree import RawTreeStore  # noqa: E402

SQL = """
SELECT served_by::String AS served_by, fallback_reason::String AS reason, count() AS n,
       quantile(0.5)(latency_ms::Float64) AS p50, quantile(0.95)(latency_ms::Float64) AS p95,
       quantile(0.99)(latency_ms::Float64) AS p99, max(latency_ms::Float64) AS max_ms,
       countIf(purchased::String != '') AS purchases, sum(revenue_eur::Float64) AS revenue
FROM {team}.feedback
WHERE request_id::String != '__init__'
  AND ts::DateTime64(3) >= toDateTime64('{since}', 3, 'UTC') AND ts::DateTime64(3) < toDateTime64('{until}', 3, 'UTC')
GROUP BY served_by, reason
ORDER BY n DESC"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    a = ap.parse_args()
    until = parse_ts(a.until) if a.until else time.time()
    since = parse_ts(a.since) if a.since else until - a.minutes * 60
    st = RawTreeStore.from_env()
    rows = st.query(SQL.format(team=st.team, since=to_sql_dt(since), until=to_sql_dt(until)), timeout=30)
    total = sum(int(r["n"]) for r in rows) or 1
    print(f"feedback {to_sql_dt(since)} .. {to_sql_dt(until)} UTC: {total} requests")
    for r in rows:
        n = int(r["n"])
        print(f"  {r['served_by']:8s} {r['reason'] or 'ok':8s} {n:6d} ({100 * n / total:5.1f}%)  latency_ms "
              f"p50={float(r['p50']):.0f} p95={float(r['p95']):.0f} p99={float(r['p99']):.0f} "
              f"max={float(r['max_ms']):.0f}  purchases/100={100 * int(r['purchases']) / n:.2f}")
    print(json.dumps(rows, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
