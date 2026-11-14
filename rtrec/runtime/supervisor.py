"""Keeps the serving process alive: restart on exit, kill-and-restart when it hangs.

    python -m rtrec.runtime.supervisor [--status-file artifacts/runtime/status.json] -- \
        python -m rtrec.runtime.serve --role primary

Why both this and systemd's Restart=always: systemd only sees exits. A process stuck in a deadlock
or a hung C call never exits, but it stops refreshing its status file (written every 10 s by the
serving thread); after `--hang-after` seconds the supervisor kills it and starts a fresh one.
Restarts back off (0.5 s doubling to 10 s) only while the child keeps dying young; a restart costs
~1-2 s of answers thanks to the journal and snapshot (see serve.py).
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("rtrec.supervisor")


def supervise(cmd: list[str], status_file: str | None, hang_after: float = 45.0, max_restarts: int | None = None,
              min_uptime: float = 30.0) -> int:
    stop = False
    child: subprocess.Popen | None = None

    def on_signal(signum, _frame):
        nonlocal stop
        stop = True
        if child and child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    backoff, restarts = 0.5, 0
    while not stop:
        started = time.time()
        log.info("starting: %s", " ".join(cmd))
        child = subprocess.Popen(cmd)
        while child.poll() is None and not stop:
            time.sleep(0.5)
            if status_file and time.time() - started > hang_after:
                try:
                    age = time.time() - Path(status_file).stat().st_mtime
                except FileNotFoundError:
                    age = time.time() - started
                if age > hang_after:
                    log.error("child %d silent for %.0f s: killing it", child.pid, age)
                    child.kill()
        code = child.wait()
        if stop:
            break
        uptime = time.time() - started
        restarts += 1
        if max_restarts is not None and restarts > max_restarts:
            log.error("giving up after %d restarts", restarts - 1)
            return code or 1
        backoff = 0.5 if uptime > min_uptime else min(backoff * 2, 10.0)
        log.warning("child exited with %s after %.1f s; restart %d in %.1f s", code, uptime, restarts, backoff)
        time.sleep(backoff)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" in argv:
        i = argv.index("--")
        own, cmd = argv[:i], argv[i + 1:]
    else:
        own, cmd = argv, [sys.executable, "-m", "rtrec.runtime.serve"]
    ap = argparse.ArgumentParser()
    ap.add_argument("--status-file", default=os.environ.get("RTREC_STATE_DIR", "artifacts/runtime") + "/status.json")
    ap.add_argument("--hang-after", type=float, default=45.0)
    ap.add_argument("--max-restarts", type=int, default=None)
    a = ap.parse_args(own)
    logging.basicConfig(level="INFO", stream=sys.stdout, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return supervise(cmd, a.status_file, a.hang_after, a.max_restarts)


if __name__ == "__main__":
    sys.exit(main())
