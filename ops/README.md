# Ops: running the serving loop

Owner: A1 (runtime). Sunday procedure and incident playbook: `ops/RUNBOOK.md` (A5).

## Commands

| What | Command |
|---|---|
| Check the live API answers our queries (first thing with a key) | `ops/run.sh --smoke` |
| Production, primary machine | `ops/run.sh` (reads `.env`: `TEAM_ID`, `RAWTREE_API_KEY`) |
| Second machine, hot standby | `ops/run.sh --standby` |
| Local rehearsal on the simulated shop | `ops/run.sh --fake` |
| systemd | `ops/rtrec.service` (instructions inside) |
| Docker | `ops/Dockerfile` (instructions inside) |
| Load test, 10x nominal rate | `python ops/loadtest.py` -> `ops/reports/loadtest.md` |
| Local fake RawTree over HTTP | `python -m rtrec.stores.fake_http --port 8765` (+ `RAWTREE_API_URL=http://127.0.0.1:8765`) |

Model and live learning are plugged in by import path, no code change in the runtime:
`RTREC_POLICY=rtrec.model.policies:make_policy RTREC_HOOKS=rtrec.live:make_hooks ops/run.sh`.
The policy loads in the background; until it is ready the loop answers with the fallback policy.

## What keeps answers flowing

| Failure | Defence | Tested in |
|---|---|---|
| Model raises / returns junk | level 2 (recent views + best sellers), then level 3 (`finalize` safe list); breaker benches a policy after 20 errors in a row | `tests/test_runtime_fallback.py` |
| Model slow or hung | policy runs on a worker thread with a 150 ms batch budget; on overrun the rest of the batch is answered by level 2 and level 1 is skipped until the stuck call returns | same |
| Product goes out of stock | catalogue refreshed every 0.25 s, applied before every batch; `finalize` drops anything unavailable | `tests/test_runtime_catalog.py` |
| Store call fails / 429 | pollers retry on the next tick (no 2 s sleep); writer retries identical rows (dedup token) honouring `retry-after`, drops rows that can no longer be on time | `tests/test_runtime_loop.py::test_flaky_store_still_on_time` |
| One HTTP call stalls | two staggered request pollers; inserts hedged after 350 ms (identical copy, first success wins) | `ops/loadtest.py` |
| Process crash | supervisor restarts in ~1.5 s; write-ahead journal: already-answered requests skipped, decided-but-unsent answers resent identically; snapshot restores learnt state | `tests/test_runtime_restart.py`, `tests/test_runtime_process.py` |
| Process hung | supervisor kills it when `status.json` is older than 45 s | `rtrec/runtime/supervisor.py` |
| Machine dies | standby on another machine computes every answer; writes only after 2.5 s without a new primary heartbeat (skew-free: compares sequence numbers on its own clock) | `tests/test_runtime_standby.py` |

## Latency budget (target p99 < 1.8 s from `ts`, limit 3 s)

visible after `ts` (~0.3-0.6 s) + our poll (<= 0.1 s with two pollers) + compute (< 50 ms/batch) + insert
(~0.2 s) + organisers' poll (<= 0.5 s). The truth is `latency_ms` in `team.feedback`; the runtime logs its
p50/p95/p99 every 10 s and writes `artifacts/runtime*/status.json` and `kind=runtime_status` rows in
`team.scratch` for the dashboard.

## Untested live

`rtrec/stores/rawtree.py` was written from the docs without a key. Everything except the SQL text has run
against the emulator. `ops/smoke_rawtree.py` exercises every query once and prints type hints; fix the SQL
constants at the top of `rawtree.py` if anything fails.
