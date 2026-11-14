"""Write-ahead journal of our answers, so a restart never answers a request twice (or not at all).

Why: the platform is append-only (no UPDATE/DELETE) and only one answer per request counts. After a
crash we must know (a) which requests we already answered, to skip them, and (b) which answers we
had decided but maybe not inserted, to resend them *identically* while they can still be on time.

Format: JSON lines in `<dir>/answers.jsonl`
    {"a": {request_id, items, model_version}, "ts": request_ts}     decided (written before insert)
    {"ack": [request_id, ...]}                                       insert confirmed
On open, the file is compacted to the last `keep_s` seconds so it never grows without bound.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path


class Journal:
    def __init__(self, directory: str | Path | None, keep_s: float = 900.0):
        self.path = Path(directory) / "answers.jsonl" if directory else None
        self.keep_s = keep_s
        self.answered: dict[str, float] = {}       # request_id -> request ts
        self.unacked: dict[str, tuple[dict, float]] = {}
        self._lock = threading.Lock()
        self._fh = None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._load()
            self._fh = open(self.path, "a", buffering=1)

    def _load(self) -> None:
        if not self.path.exists():
            return
        rows = {}
        for line in self.path.read_text().splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # torn last line after a crash
            if "a" in rec:
                rid = rec["a"]["request_id"]
                rows[rid] = (rec["a"], float(rec.get("ts", 0)), False)
            elif "ack" in rec:
                for rid in rec["ack"]:
                    if rid in rows:
                        a, t, _ = rows[rid]
                        rows[rid] = (a, t, True)
        newest = max((t for _, t, _ in rows.values()), default=0.0)
        keep = {rid: v for rid, v in rows.items() if v[1] >= newest - self.keep_s}
        for rid, (a, t, acked) in keep.items():
            self.answered[rid] = t
            if not acked:
                self.unacked[rid] = (a, t)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as fh:
            for rid, (a, t, acked) in keep.items():
                fh.write(json.dumps({"a": a, "ts": t}) + "\n")
                if acked:
                    fh.write(json.dumps({"ack": [rid]}) + "\n")
        os.replace(tmp, self.path)

    def newest_ts(self) -> float:
        return max(self.answered.values(), default=0.0)

    def decided(self, rows: list[tuple[dict, float]]) -> None:
        with self._lock:
            for a, t in rows:
                self.answered[a["request_id"]] = t
                self.unacked[a["request_id"]] = (a, t)
                if self._fh:
                    self._fh.write(json.dumps({"a": a, "ts": t}) + "\n")
            if self._fh:
                self._fh.flush()

    def acked(self, request_ids: list[str]) -> None:
        with self._lock:
            for rid in request_ids:
                self.unacked.pop(rid, None)
            if self._fh and request_ids:
                self._fh.write(json.dumps({"ack": request_ids}) + "\n")
                self._fh.flush()
            if len(self.answered) > 200_000:   # memory bound: ~1 day at 3 req/s
                cut = self.newest_ts() - self.keep_s
                self.answered = {k: v for k, v in self.answered.items() if v >= cut}

    def __contains__(self, rid: str) -> bool:
        return rid in self.answered

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
