"""Process-level crash test: kill -9 the serving process; the supervisor restarts it.

The "organisers" (FakeStore behind the HTTP emulator) live in this test process; the serving loop runs
as a real subprocess with RTREC_STORE=rawtree, i.e. exactly the production code path.
"""
import os
import signal
import subprocess
import sys
import time

import pytest

from rtrec.runtime.clock import RealClock
from rtrec.stores.fake import FakeStore
from rtrec.stores.fake_http import Emulator
from rtrec.stores.stub_sim import StubParams, StubSimulator


def _serving_pid(store, exclude=()):
    beats = [r for r in store.scratch if r.get("kind") == "heartbeat"]
    for r in reversed(beats):
        pid = int(str(r["host"]).rsplit("-", 1)[-1])
        if pid not in exclude:
            return pid
    return None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_kill9_and_supervisor_restart(tmp_path):
    clock = RealClock()
    sim = StubSimulator(seed=4, start=clock.now() - 0.01, params=StubParams(request_rate=10.0))
    store = FakeStore(sim, clock)
    http = Emulator(store, api_key="k").serve()
    env = dict(os.environ, RTREC_STORE="rawtree", TEAM_ID="team_00", RAWTREE_API_KEY="k",
               RAWTREE_API_URL=f"http://127.0.0.1:{http.server_address[1]}", RTREC_LOG="WARNING")
    sd = tmp_path / "rt"
    cmd = [sys.executable, "-m", "rtrec.runtime.supervisor", "--status-file", str(sd / "status.json"), "--",
           sys.executable, "-m", "rtrec.runtime.serve", "--state-dir", str(sd)]
    t0 = time.time()
    sup = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 20
        while _serving_pid(store) is None and time.time() < deadline:
            time.sleep(0.2)
        first = _serving_pid(store)
        assert first, "serving process never sent a heartbeat"
        time.sleep(5)
        t_kill = time.time()
        os.kill(first, signal.SIGKILL)
        deadline = time.time() + 20
        while _serving_pid(store, exclude={first}) is None and time.time() < deadline:
            time.sleep(0.2)
        second = _serving_pid(store, exclude={first})
        assert second and second != first, "supervisor did not restart the serving process"
        restart_s = time.time() - t_kill
        time.sleep(5)
        t_end = time.time()
    finally:
        sup.send_signal(signal.SIGTERM)
        sup.wait(timeout=10)
    time.sleep(6)
    rep = store.report(since=t0 + 3, until=t_end - 1)
    http.shutdown()
    print(f"restart took {restart_s:.2f} s; report {rep['reasons']} latency {rep['latency_received_s']}")
    assert rep["conflicting_answers"] == 0
    # what a kill -9 costs: the requests whose deadline fell inside the restart gap
    missing = rep["reasons"].get("missing", 0) + rep["reasons"].get("late", 0)
    assert missing <= 10 * (restart_s + 3), (rep, restart_s)
    assert rep["reliability"] > 0.6
