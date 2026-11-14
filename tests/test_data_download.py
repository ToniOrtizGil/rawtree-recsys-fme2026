"""scripts/download_history.py against a fake RawTree that serves the simulator's history (offline).
Checks the day chunking, the split-on-too-big retry, resumability and that the output matches the
layout of sim/dump.py. This does NOT prove it works against the real API."""
import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from sim.params import SimConfig
from sim.shop import make_simulator

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("download_history", ROOT / "scripts" / "download_history.py")
dl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dl)


class FakeRawTree:
    def __init__(self, dump, max_rows=4000):
        self.tables = {"catalog": dump.catalog, "history_sessions": dump.history_sessions,
                       "history_events": dump.history_events}
        self.max_rows = max_rows
        self.calls = 0

    def __call__(self, sql: str):
        self.calls += 1
        table = re.search(r"FROM shop\.(\w+)", sql).group(1)
        df = self.tables[table]
        if "min(ts" in sql:
            return [{"lo": df.ts.min().replace("T", " ").rstrip("Z"), "hi": df.ts.max().replace("T", " ").rstrip("Z")}]
        m = re.findall(r"toDateTime64\('([^']+)'", sql)
        if m:
            lo, hi = (datetime.fromisoformat(x).replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") for x in m)
            df = df[(df.ts >= lo) & (df.ts < hi)]
        if len(df) > self.max_rows:
            raise dl.QueryError(400, "max_result_bytes exceeded")
        recs = json.loads(df.to_json(orient="records"))
        return [{"__raw_data": json.dumps(r)} for r in recs]


@pytest.fixture(scope="module")
def dump():
    return make_simulator(4, SimConfig.small(history_days=2)).history(2)


def test_download_matches_dump_layout(tmp_path, dump):
    fake = FakeRawTree(dump)
    stats = dl.download(fake, tmp_path, log=lambda *_: None)
    assert stats["history_sessions"] == len(dump.history_sessions)
    assert stats["history_events"] == len(dump.history_events)       # events needed hour splits
    ev = pd.concat(pd.read_parquet(p) for p in sorted((tmp_path / "history_events").glob("*.parquet")))
    assert sorted(ev.columns) == sorted(dump.history_events.columns)
    hs = pd.read_parquet(sorted((tmp_path / "history_sessions").glob("*.parquet"))[0])
    assert len(hs.iloc[0]["shown"]) == 10 and set(hs.iloc[0]["context"]) == {"device", "region", "page"}
    assert json.loads((tmp_path / "SOURCE.json").read_text())["source"] == "rawtree"
    # resumable: a second run downloads nothing but the ts ranges
    fake.calls = 0
    dl.download(fake, tmp_path, log=lambda *_: None)
    assert fake.calls == 2
