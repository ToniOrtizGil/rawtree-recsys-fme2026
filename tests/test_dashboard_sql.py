"""The SQL twins (dashboard/queries.py) must give the same numbers as the Python metrics.

We cannot reach RawTree yet, so we run the queries on embedded ClickHouse (chdb), with every column declared
`Dynamic` like RawTree does, loaded with DemoStore rows. Skipped when chdb is not installed
(`pip install -e '.[sqltest]'`). This proves syntax + semantics on ClickHouse, not on live RawTree.
"""
from __future__ import annotations

import json

import pytest

chdb_session = pytest.importorskip("chdb.session")

from dashboard import queries  # noqa: E402
from dashboard.collector import Collector  # noqa: E402
from dashboard.demo import DemoStore  # noqa: E402
from dashboard.metrics import to_iso  # noqa: E402

NOW = 1_794_733_200.0      # 2026-11-15 09:00:00 UTC (start of the final), fixed


def _lit(t: float) -> str:
    return f"toDateTime64('{to_iso(t)[:-1].replace('T', ' ')}', 3, 'UTC')"


@pytest.fixture(scope="module")
def world():
    store = DemoStore(seed=3, start=NOW - 1500, clock=lambda: NOW, chaos=True)
    col = Collector(store, sql_every_s=0)
    for _ in range(5):          # version lookups are capped per poll; drain the 25-minute backfill
        col.poll()
    sess = chdb_session.Session()
    for table, rows in store.dump().items():
        db, name = table.split(".")
        sess.query(f"CREATE DATABASE IF NOT EXISTS {db}")
        cols = sorted({k for r in rows for k in r})
        extra = ", __raw_data String" if table == "shop.requests" else ""
        sess.query(f"CREATE TABLE {table} ({', '.join(f'`{c}` Dynamic' for c in cols)}{extra}) ENGINE = Memory")
        body = []
        for r in rows:
            row = dict(r)
            if table == "shop.requests":
                row["__raw_data"] = json.dumps(r)
            body.append(json.dumps(row))
        for i in range(0, len(body), 2000):
            sess.query(f"INSERT INTO {table} FORMAT JSONEachRow " + "\n".join(body[i:i + 2000]))
    yield store, col, sess
    sess.close()


def q(sess, sql: str) -> list[dict]:
    out = sess.query(sql, "JSONEachRow").bytes().decode()
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_every_panel_runs_and_is_select_only(world):
    _, _, sess = world
    for name, (_, sql) in queries.panels("team_demo", _lit(NOW)).items():
        assert sql.lstrip().upper().startswith(("SELECT", "WITH")), name
        q(sess, sql)   # raises on any syntax / type error


def test_light_and_latency_match_python(world):
    _, col, sess = world
    snap = col.snapshot()
    (light,) = q(sess, queries.light("team_demo", _lit(NOW)))
    py = snap["light"]
    assert int(light["answers"]) == py["n"] and int(light["ok"]) == py["ok"]
    assert light["light"] == py["status"]
    assert int(light["missing"]) == py["reasons"]["missing"] > 0      # the chaos outage is in the window
    (lat,) = q(sess, queries.latency("team_demo", _lit(NOW)))
    assert lat["p95_ms"] == pytest.approx(snap["latency_5m"]["p95"], abs=0.1)
    assert lat["p99_ms"] == pytest.approx(snap["latency_5m"]["p99"], abs=0.1)


def test_versions_and_cold_warm_match_python(world):
    _, col, sess = world
    snap = col.snapshot(window_s=3600)
    sql_v = {r["model_version"]: r for r in q(sess, queries.by_version("team_demo", 60, _lit(NOW)))}
    for v in snap["versions"]:
        assert int(sql_v[v["model_version"]]["requests"]) == v["n"]
        assert int(sql_v[v["model_version"]]["purchases"]) == v["purchases"]
    sql_s = {r["segment"]: r for r in q(sess, queries.cold_warm("team_demo", 60, _lit(NOW)))}
    for seg in ("cold", "warm"):
        assert int(sql_s[seg]["requests"]) == snap["segments"][seg]["n"]
        assert int(sql_s[seg]["purchases"]) == snap["segments"][seg]["purchases"]


def test_versions_lookup_and_heartbeat(world):
    store, col, sess = world
    ids = list(store.submissions)[:50]
    rows = q(sess, queries.versions_for("team_demo", ids + ["x'; DROP TABLE y; --"]))
    assert {r["request_id"]: r["model_version"] for r in rows} == {i: store.submissions[i]["model_version"] for i in ids}
    (hb,) = q(sess, queries.last_heartbeat("team_demo"))
    assert hb["kind"] == "heartbeat" and hb["role"] == "primary"


def test_trends_top_subcategory_matches_python(world):
    store, col, sess = world
    rows = q(sess, queries.trends(_lit(NOW)))
    assert rows and rows[0]["subcategory"] == store._trending(NOW - 1)
