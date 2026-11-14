"""Day-one check of RawTreeStore against the live API. Read-only except one row in team.scratch.

    ops/run.sh --smoke        (or: TEAM_ID=team_NN RAWTREE_API_KEY=rt_... python ops/smoke_rawtree.py)

RawTreeStore was written without access to the API. This runs every query it sends once and prints
row counts, a sample row, latency and RawTree's type `hints`, so a wrong cast or column path shows up
in seconds instead of as missing answers. It never writes to team.submissions.
Exit code 0 only if everything the serving loop needs works.
"""
from __future__ import annotations

import json
import sys
import time

sys.path.insert(0, ".")
from rtrec.runtime.clock import parse_ts  # noqa: E402
from rtrec.stores.rawtree import RawTreeStore  # noqa: E402


def main() -> int:
    st = RawTreeStore.from_env()
    now = time.time()
    ok = True
    checks = [
        ("catalog", lambda: st.catalog(), True),
        ("requests_since(now-60)", lambda: st.requests_since(now - 60), True),
        ("catalog_updates_since(0)", lambda: st.catalog_updates_since(0, limit=1000), False),
        ("events_since(now-60)", lambda: st.events_since(now - 60, limit=1000), True),
        ("feedback_since(now-600)", lambda: st.feedback_since(now - 600, limit=100), False),
        ("best_sellers", lambda: st.best_sellers(), True),
        ("scratch heartbeat read", lambda: st.scratch_since(now - 600, kind="heartbeat"), False),
    ]
    for name, fn, must_have_rows in checks:
        t0 = time.perf_counter()
        try:
            rows = fn()
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {name}: {e}")
            ok = False
            continue
        dt = (time.perf_counter() - t0) * 1000
        flag = "OK  " if rows or not must_have_rows else "EMPTY"
        if must_have_rows and not rows:
            ok = False
        print(f"{flag} {name}: {len(rows)} rows in {dt:.0f} ms; hints={st.last_hints[:3]}")
        if rows:
            print("     sample:", json.dumps(rows[0], default=str)[:400])
        if name.startswith("requests") and rows:
            lag = time.time() - max(parse_ts(r["ts"]) for r in rows)
            print(f"     newest request is {lag:.2f} s old (visibility lag + poll; should be < 1 s)")
            bad = [r for r in rows if not r["context"].get("device") or not isinstance(r["recent_views"], list)]
            if bad:
                print(f"WARN {len(bad)} requests with empty context/recent_views: check the column paths")
    t0 = time.perf_counter()
    try:
        st.insert_scratch([{"kind": "smoke", "ts_epoch": time.time(), "note": "ops/smoke_rawtree.py"}])
        print(f"OK   insert into {st.team}.scratch in {(time.perf_counter() - t0) * 1000:.0f} ms")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL insert scratch: {e}")
        ok = False
    print("ALL GOOD" if ok else "SOMETHING FAILED: fix rtrec/stores/rawtree.py before going live")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
