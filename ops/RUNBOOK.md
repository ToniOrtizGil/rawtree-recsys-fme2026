# RUNBOOK · Sunday 15 Nov, final round

**Only 10:00–12:00 CET (09:00–11:00 UTC) is scored.** About 21,600 requests. The leaderboard is hidden; the only
signal from the organisers is a light per team: **green if more than 90 % of our answers in the last 5 minutes
were valid and on time**. Every answer we miss goes to the best-seller fallback, which scores 0 conversion
points *and* costs reliability.

Our rule for the morning: **change nothing unless an alert says so. If you do change something, change one
thing and say it in the team chat first.**

> Owner of this file: A5. Commands marked (A1) belong to the runtime. They use the flags of
> `python -m rtrec.runtime.serve` as of Saturday 13:00; A1 confirms the final wrappers (`ops/run.sh`).

## Commands (fill `FINAL` at 07:00, then never retype them)

```bash
export RTREC_STORE=rawtree TEAM_ID=team_NN          # key: environment/.env only, never in git or chat
export FINAL_POLICY=rtrec.model.policies:…          # chosen at 07:00 (§4)
export HOOKS=rtrec.live.hook:LiveHook

# primary (production machine)                                    (A1)
python -m rtrec.runtime.serve --role primary --policy $FINAL_POLICY --hooks $HOOKS
# standby (second machine): writes only if the primary heartbeat is lost   (A1)
python -m rtrec.runtime.serve --role standby --policy $FINAL_POLICY --hooks $HOOKS
# baseline-only (emergency): no --policy = built-in session/best-seller fallback chain   (A1)
python -m rtrec.runtime.serve --role primary --hooks $HOOKS
# watcher
python -m dashboard serve            # http://127.0.0.1:8050
python -m dashboard check            # one line, exit 0 ok / 1 warning / 2 critical
```

## Decision table for the final (10:00–12:00)

Find the row, run the action, and say it in the team chat. Only the pilot runs commands on production.

| Dashboard shows | Most likely | Action (pilot) | Then |
|---|---|---|---|
| Light green, no alert | all good | **nothing** | keep watching |
| `SEEN_INVALID`, a few per hour | stock changed between read and judge | nothing, note it | §5.3 if it keeps growing |
| `RELIABILITY_LOW` (<98 %), mostly `late` | slow machine / network | check `top` and the network (§5.2) | if p99 > 2500 ms for 5 min: restart primary with the lighter policy (master decides) |
| `LIGHT_RED`, mostly `missing`, heartbeat **old** | primary dead / frozen | confirm standby took over (heartbeat role `standby`); if not, start the **primary** command on the standby machine | restart the dead one as `--role standby` only |
| `LIGHT_RED`, mostly `missing`, heartbeat **fresh** | writer failing (429 / 401 / network) | read the runtime log for insert errors (§5.1.3); check only one writer is running | if not fixed within 3 min: stop it, start **baseline-only** |
| `LIGHT_RED`, mostly `late` | overload, or RawTree slow for everyone | restart primary; if still late, **baseline-only** | tell the master |
| `LIGHT_RED`, mostly `invalid` | catalogue/stock refresh stuck | restart primary (state reloads in seconds) | if it persists, **baseline-only** |
| `REQUESTS_STALLED`, everyone | organisers paused / 12:00 | nothing | event channel |
| `READ_ERROR` only on the dashboard | watcher's laptop/network | restart the dashboard; **do not touch production** | — |
| Conversion lower than Saturday | new shoppers, products and trends | **nothing** (not an alarm) | — |

---

## 0. Roles (fill in on Saturday night)

| Role | Who | Does |
|---|---|---|
| **Pilot** | … | Only person who touches the production machine. Runs the commands below. |
| **Watcher** | … | Keeps the dashboard open on a second screen and reads alerts aloud. Does not touch production. |
| **Backup pilot** | … | Has SSH and the repo on the standby machine. Takes over if the pilot's laptop or connection dies. |
| **Jury prep** | … | Rehearses `docs/presentation/` from 10:30. Does not touch production. |

Team chat channel: … · Phone numbers on paper (the laptop may be what dies).

## 1. Timeline (CET; UTC in brackets)

| When | What | Done |
|---|---|---|
| Sat 22:00 (21:00) | Dashboard and runbook ready; overnight watch through `python -m dashboard check` | ☐ |
| Sun 07:00 (06:00) | Choose the final `model_version` from the A/B table (§4). Write it down here: `……` | ☐ |
| **Sun 09:00 (08:00)** | **CODE FREEZE.** Tag it: `git tag final-$(date +%H%M) && git push --tags`. Production runs that tag. | ☐ |
| 09:00–09:30 | Pre-flight checklist (§2), including the restart drill | ☐ |
| 09:30–09:55 | Hands off. Light green, `check` prints `OK` | ☐ |
| **10:00 (09:00)** | **Final starts.** New shoppers, products and trends. Watch only (§3) | ☐ |
| 12:00 (11:00) | Stream stops, final ranking appears. Keep the system running until 12:05, then stop it | ☐ |
| 12:05 | Take screenshots of the dashboard (2 h window) for the jury | ☐ |

## 2. Pre-flight checklist (09:00–09:30)

Run these on the **production** machine unless the step says otherwise.

1. ☐ Right code: `git describe --tags` prints the freeze tag. `git status` is clean.
2. ☐ Right team and store: `echo $TEAM_ID $RTREC_STORE` prints `team_NN rawtree`. The key is in the environment
   or in `.env`, never in git.
3. ☐ Machine will not sleep. Laptop: plugged in, sleep and lid-close action disabled, no OS updates pending,
   wired network or a stable hotspot. VPS: `uptime` and free disk space (`df -h .`) are fine.
4. ☐ Exactly **one writer**. Primary running, standby in standby mode (it only writes when the primary
   heartbeat is lost). Two primaries mean duplicate answers. (A1)
5. ☐ Dashboard open in a browser on the watcher's machine (its own process; it reads RawTree directly, so it
   keeps working if production dies):
   ```bash
   RTREC_STORE=rawtree python -m dashboard serve      # http://127.0.0.1:8050
   ```
6. ☐ SQL twins work against live RawTree: `python -m dashboard sqlcheck --store rawtree` prints `OK` on every line.
7. ☐ Health line is OK: `python -m dashboard check --store rawtree` exits 0, light green, p95 well under 2000 ms,
   heartbeat a few seconds old, model_version in the A/B table equals the chosen one.
8. ☐ **Restart drill** (before 09:30 only): stop the primary process, wait 10 s, start it again with the deploy
   primary command (Commands above). In the dashboard, `HEARTBEAT_LOST` should appear and then clear. The standby
   should take over during the gap and stand down after it, and the light should stay green.
9. ☐ Fallback drill on the standby only: confirm it answers with valid lists when promoted (A1).
10. ☐ Everyone knows §3 and the decision rule.

## 2b. Key day: the first time we have the API key (any day, before Sunday)

Run once, in this order, and paste the outputs in the team chat:
1. ☐ `python -m dashboard sqlcheck --store rawtree`: every line `OK`. An error on `cold_warm` means
   `shop.requests` has no `__raw_data`. Tell A5, because the cold/warm SQL must then read `recent_views` another way.
   The Python cold/warm numbers on the page do not depend on it.
2. ☐ `python -m dashboard check --store rawtree`: our reads work (`READ_ERROR` would show here).
3. ☐ Start the primary (Commands above), then open the dashboard and watch the **first 10 minutes**:
   | Minute | Expect | If not |
   |---|---|---|
   | 0–1 | requests/s ≈ 3, heartbeat < 5 s, `FEEDBACK_STALLED` may show briefly | `READ_ERROR` → key, team id, network |
   | 1–2 | first feedback rows, served by `team` | all `missing` → we write to the wrong table/team (§5.1) |
   | 2–5 | light estimate turns GREEN (needs 20 answers); p95 ≈ the organisers' `latency_ms` | p95 > 2000 ms → §5.2 before anything else |
   | 5–10 | A/B table shows our `model_version`, not "(no submission row)"; cold column filling | "(no submission row)" → our submissions are not readable back; tell A1 |
4. ☐ Compare our light with the organisers' light on the leaderboard: they must agree.
5. ☐ Leave `python -m dashboard check --store rawtree` running every 10 minutes overnight (§3).

## 3. During the final (10:00–12:00): what to watch

The watcher keeps the dashboard open and reads the **Alerts** box. Each alert names a section of this file.

| Signal | Normal | Act when |
|---|---|---|
| Light (our estimate) | GREEN, 99 %+ | below 98 % → warning; ≤ 90 % → RED, see §5 |
| Requests/s | about 3 | 0 for more than 30 s → §5.5 |
| Latency p95 / p99 | under 1500 / 2000 ms | p95 above 2000 or p99 above 2500 → §5.2 |
| Heartbeat | a few seconds old | older than 15 s → §5.4 |
| Last feedback | under 10 s ago | over 60 s → §5.5 |
| Fallback reasons | all 0 | any `invalid` → §5.3; any `missing` → §5.1 |
| Purchases/100 by model_version | stable | **do not react to it** (see below) |

**Conversion is not an alarm.** It is noisy minute to minute, and the final has new shoppers, products and
trends, so it will differ from Saturday. Only reliability problems justify touching production during the final.
The live learning (trends, prices, stock, feedback calibration) is built to adapt by itself.

Scripted watch (cron or a terminal loop). It prints one line a minute and exits non-zero when something is wrong:
```bash
while true; do python -m dashboard check --store rawtree --warmup 2 || echo "ALERT — open the dashboard"; sleep 60; done
```

## 4. Choosing the final version (07:00)

Open the dashboard with the **3 h** window, A/B table:
- Only versions with thousands of requests count. If two 95 % intervals overlap, there is **no proven
  difference**: keep the simpler and more reliable one.
- Look at the **cold** column too: the final is full of cold shoppers (new users, new products). A version that
  wins only on warm traffic is a risk.
- Reliability first: a version whose fallback share or latency is higher loses, whatever its conversion.
- Write the choice in §1 and in the team chat. The master session makes the deploy.

## 5. When an alert fires

<a id="r0-triage"></a>
### 5.0 Triage (any alert, or `READ_ERROR`)
1. Read the **fallback reasons** row: `missing`, `late` and `invalid` have very different fixes (§5.1–5.3).
2. If the dashboard itself shows `READ_ERROR` but production is fine, the problem is the dashboard's machine
   (network, key). Production does not need the dashboard. Restart the dashboard and do not touch production.
3. Check the organisers' own light on the event site. If theirs is green and ours is red, trust theirs and
   investigate calmly.
4. RawTree can be queried by hand (organisers' queries 5–6, `docs/upstream/queries.sql`), and every panel's
   SQL is under "Computed inside RawTree" on the page.

<a id="r1-missing"></a>
### 5.1 `missing`: we are not answering
Most likely, in order: the process is dead, the writer is stuck (inserts failing, e.g. 429 or network), or we
are writing to the wrong team or with a wrong key.
1. Heartbeat old? Then the process is dead or frozen. The **standby should already be answering**. If it is not,
   promote it (A1) on the standby machine.
2. Restart the primary with the deploy primary command (Commands above). It reloads its state snapshot in seconds.
3. Heartbeat fresh but answers still `missing`? Then the writer is failing. Look at the runtime log for HTTP
   errors on `/v1/tables/submissions`. 429 means rate limiting: the runtime batches inserts, so check that
   only one process is writing. 401/403 means a wrong key or team.
4. If nothing works within 3 minutes, run the **baseline-only mode** (A1) from the freeze tag. Best sellers on
   time beat a smart answer that never arrives.

<a id="r2-late"></a>
### 5.2 `late`: answers after 3 s, or latency rising
1. Is the machine overloaded (`top`) or the network slow? Tethering, wifi, a VPN: move to cable or the VPS.
2. Is RawTree slow for everyone? `statistics.elapsed` of our reads in the runtime log; organisers' channel.
3. The runtime has a per-batch time budget and falls back to the session heuristic, then to the safe list (A1).
   If p99 still exceeds 2500 ms, switch to the lighter model version (the master decides).
4. Never "fix" lateness by writing more often than every ~0.2–0.5 s. Batching is what keeps us under the
   rate limits.

<a id="r3-invalid"></a>
### 5.3 `invalid`: a list was rejected
One out-of-stock or inactive product invalidates the whole list.
1. A few per hour: a product went out of stock between our read and the organisers' check. Acceptable. Note it.
2. A steady stream: the catalogue/stock refresh is stuck. Check the runtime log for `catalog_updates` reads
   (they should run at least every 0.5 s). Restart the primary if they stopped.
3. `validate.finalize` should make duplicates and wrong lengths impossible. If invalids persist after a restart,
   switch to baseline-only and tell the master.

<a id="r4-machine"></a>
### 5.4 `HEARTBEAT_LOST`: the machine or the process died
1. Standby takes over automatically after the heartbeat timeout (A1). Confirm in the dashboard: light still
   green, heartbeat role `standby`.
2. Laptop died or lost network: the backup pilot runs the deploy command on the standby/VPS from the freeze tag
   **only if** the standby is not already answering. Two writers = duplicate answers.
3. Once the primary is back, it must come up as standby, or the standby must stand down. Never both primary.

<a id="r5-stalled"></a>
### 5.5 `REQUESTS_STALLED` / `FEEDBACK_STALLED`
1. Requests stopped for everyone (organisers paused the stream, or it is 12:00)? Check the event channel.
   Nothing to do.
2. Requests stopped only in our reader: our cursor or the network is stuck. Restart the dashboard. If production
   also stopped answering (`missing` grows once feedback resumes), restart production.
3. Feedback stalled while requests flow: we cannot see our reliability. Use the organisers' light and the SQL
   queries by hand. Production itself does not depend on feedback.

## 6. What we never do on Sunday morning
- Retrain, change features or merge branches after 09:00 (unless the master decides a rollback).
- Run a second writer "to test".
- Write anything into `team_NN.submissions` by hand: recommendations must come from code.
- Paste the API key into a chat, a commit or a screenshot.
