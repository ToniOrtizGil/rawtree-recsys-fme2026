# Work split (phase "no key yet")

Plan: `docs/memory/06_plan_ganador.md`. Everything below is buildable without an API key; items marked
🔑 wait for the key and are prepared so they run with one command when it arrives.

| Agent | Branch | Owns (only edit these) |
|---|---|---|
| A1 Runtime | `agent/a1-runtime` | `rtrec/runtime/`, `rtrec/stores/`, `ops/` (except `ops/RUNBOOK.md`), `tests/test_runtime*`, `tests/test_stores*` |
| A2 Data & simulator | `agent/a2-data` | `sim/` (except `sim/base.py`), `scripts/download_*`, `rtrec/data/`, `rtrec/sql/`, `notebooks/forensics*`, `docs/FORENSICS.md`, `tests/test_sim*`, `tests/test_data*` |
| A3 Model | `agent/a3-model` | `rtrec/model/`, `rtrec/offline/`, `scripts/train_*`, `scripts/eval_*`, `notebooks/model*`, `docs/MODEL.md`, `tests/test_model*`, `tests/test_offline*` |
| A4 Live learning | `agent/a4-live` | `rtrec/live/`, `docs/LIVE.md`, `tests/test_live*` |
| A5 Observability & demo | `agent/a5-demo` | `dashboard/`, `ops/RUNBOOK.md`, `docs/presentation/`, `README.md` sections "How it works"/"Demo", `tests/test_dashboard*` |
| Master | `main` | contract files, `CLAUDE.md`, `docs/TASKS.md`, `docs/memory/`, `scripts/arbiter.py`, `tests/test_arbiter.py`, merges |

## A1 Runtime
1. `rtrec/stores/fake.py` FakeStore over a `sim.base.Simulator` (use a trivial stub sim until A2 ships), with a
   simulated clock *and* a real-time mode; it must reproduce the organisers' timing: request visible after a
   small delay, deadline 3 s from `ts`, judged by polling every 0.5 s, feedback a few seconds later.
2. `rtrec/stores/rawtree.py` RawTreeStore: persistent HTTP session, incremental cursor queries with overlap,
   batched inserts, retries that never duplicate a submission, 429 `retry-after` handling, gzip. 🔑 untested live.
3. `rtrec/runtime/serve.py` the loop: poll 150–250 ms → policy (with per-batch time budget, exception guard,
   3 fallback levels) → `finalize` → batched insert. Background threads refresh catalogue/stock every ≤0.5 s and
   run the live updaters (A4 hooks: `on_events`, `on_catalog_updates`, `on_feedback`). One writer only.
4. Crash safety: supervisor/auto-restart, state snapshot to disk, heartbeat row in `team_NN.scratch`,
   standby mode that only writes if the primary heartbeat is lost, dedupe of answered request_ids across restarts.
5. `ops/`: one-command deploy (`ops/run.sh`), systemd unit, Dockerfile. Load test: 10× nominal rate, p99 latency report.

## A2 Data & simulator
1. **First, within ~1 h**: `sim/shop.py` implementing `sim.base.Simulator` — synthetic shop with the real
   shapes and rates (≈5,000 products, 10 categories, 90 subcategories, brands, prices, descriptions; 3 req/s;
   ~50 events/s; new products, price changes, stock-outs, trends; new users), and a **hidden purchase model**
   with position bias, session/category affinity, price sensitivity by device/region, trends, repeat-purchase
   penalty. Plus an old "best sellers + randomness" logging policy that records true propensities. Push early
   so A1/A3/A4 can use it; refine later. Keep the hidden model's parameters in one place so tests can vary them.
2. `scripts/download_history.py` 🔑: dumps `shop.catalog`, `history_sessions`, `history_events` to Parquet
   in day chunks (CSV/TSV + gzip), resumable, respecting rate limits. Same layout the simulator writes:
   `data/catalog.parquet`, `data/history_sessions/*.parquet`, `data/history_events/*.parquet`.
3. `rtrec/sql/*.sql` + `rtrec/data/features.py`: feature queries in RawTree SQL (co-views, popularity by context,
   conversion priors corrected by propensity, position bias, trend lift) with pandas equivalents so they run on
   the simulator dump; this is the "Use of RawTree" story.
4. `notebooks/forensics.ipynb` + `docs/FORENSICS.md`: the H1–H11 questions of the plan, as code that runs on
   either the synthetic dump or the real one; report numbers for synthetic now, rerun on real data on day one.

## A3 Model
1. `rtrec/offline/`: evaluation harness on `history_sessions`-shaped data: temporal split, IPS / SNIPS /
   doubly-robust estimates of purchases per 100 requests (with CI and match counts), hit@10, cold segment.
   Validate the estimators against the simulator's `expected_purchase` ground truth (unique advantage: we can
   check our offline estimator is unbiased before trusting it on real data).
2. `rtrec/model/candidates.py`: ~300 candidates/request (session items, co-views, same subcategory/brand,
   trends, context popularity, content-similar new products).
3. `rtrec/model/features.py` + `ranker.py`: LightGBM P(purchase) with no ID features; `scripts/train_ranker.py`
   trains from Parquet (synthetic now, real later) and exports an artefact loaded into `state.offline`.
4. Policies implementing `rtrec.contract.Policy`: `BaselinePolicy`, `SessionHeuristicPolicy` (v1), `RankerPolicy` (v2).
   Report their score on the simulator as `100*(team-baseline)/(oracle-baseline)`.

## A4 Live learning
1. `rtrec/live/`: updaters the runtime calls with new rows: catalogue/price/stock tracker (sets
   `state.available`, `state.new_products`), decayed trend counters + lift, live user profiles, co-view updates.
2. Feedback learning: per product/subcategory Beta calibration (observed vs predicted), periodic retrain hook.
3. Exploration: Thompson sampling on slots 9–10 for uncertain/new products; logging of our own propensities.
4. A/B framework: hash(request_id) split, `model_version` tags, purchases/100 with CI from feedback, promotion rule.
5. Cold start: new users without views (context popularity × trend), new products (content neighbours, priors).
   Measure every piece on the simulator (cold and global score).

## A5 Observability & demo
1. `dashboard/`: live page (requests flowing, the 10 items and why, purchases/100 by model_version, cold
   vs warm, latency p50/p95/p99, fallback reasons, trends) reading from the Store; works on the simulator now.
2. Alerts: on-time rate < 98 % over 5 min, heartbeat lost, feedback stalled.
3. `ops/RUNBOOK.md`: Sunday procedure (freeze, checks, what to do if the light goes red).
4. `docs/presentation/`: 5-slide story for a product manager + demo script.

## Promotion rule (master)
A policy goes to production only if `scripts/arbiter.py` (same seed, fresh simulator per policy, live hooks fed,
baseline = fixed best sellers in stock, oracle = `sim.oracle`) gives it the best `score` without lowering
`cold_score`, with 0 `policy_errors`, and A1's load test keeps p99 < 1.8 s with it. On real data the same
decision is confirmed by an A/B on our own feedback.
