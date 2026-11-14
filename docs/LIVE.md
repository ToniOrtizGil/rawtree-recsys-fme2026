# Live learning (A4) — what `rtrec.live` writes into `State`

Code: `rtrec/live/`. Hooks the runtime (A1) calls from **one** updater thread with the rows it just read:

```python
from rtrec import live
live.init(state, catalog_rows)            # once: shop.catalog (list of dicts or DataFrame)
live.on_catalog_updates(rows, state)      # shop.catalog_updates, every <= 0.5 s
live.on_events(rows, state)               # shop.events (~50 rows/s, bursts of thousands)
live.seed_history(state, history_events)  # optional, once: long-run subcat/brand shares (sharper trend lifts)
live.on_requests(rows, state)             # shop.requests (optional; gives feedback a user_id / cold flag)
live.on_feedback(rows, state)             # team_NN.feedback
```

With A1's runtime (`rtrec/runtime/hooks.py` HookSet) just pass `hooks=[rtrec.live.hooks.LiveHook()]`:
`on_start` loads the catalogue from `state.catalog`, `on_served` records each request and the
`model_version` that answered it (feedback has neither user_id nor version), `on_tick` refreshes
snapshots. Measured: 2,000-event chunk ≈ 15 ms, 50 events ≈ 0.5 ms, trend snapshot ≈ 2–10 ms.
Hooks are also serialised by an internal lock, so calling them from several threads is safe.
A4 owns `state.catalog` / `available` / `new_products` (master decision): the runtime's own
tracker should not write them (revised decision: A1 is the single writer; LiveHook runs the live tracker with owner=False, derived keys only).

Guarantees: incremental (O(1) per row), idempotent (cursor-overlap duplicates ignored), exact with
out-of-order rows, memory bounded. Event time (max `ts` seen), not wall clock, drives decay, so the
same code runs on a replay, the simulator and the live stream.

Thread safety for readers (policies): every key below is either **replaced as a whole object**
(safe to iterate) or **lookup-only** (item-assigned in place; use `.get()`, never iterate).

## Contract fields (exact, set by `catalog.py`)

| Field | Meaning |
|---|---|
| `state.catalog[pid]` | product row with the latest update (by its own `ts`) applied; current `price_eur`, `is_active` |
| `state.available` | frozenset: `is_active` **and** in stock. Out-of-order rows never resurrect old stock; on a ts tie `out_of_stock` wins |
| `state.new_products` | frozenset of ids seen with `change == "new_product"` (organisers' cold definition) |
| `state.users[uid]` | `UserProfile`: live `views` / `carts` / `purchases` appended from `shop.events` (sorted by ts, capped 200); carousel purchases and clicks from our feedback (needs `on_requests`) |

## `state.live` keys

| Key | Type | Refresh | Meaning |
|---|---|---|---|
| `price` | dict pid → `PriceInfo(price, base_price, prev_price, changed_at)` | every update (lookup-only) | `.ratio_to_base`, `.last_change_ratio` (< 1 = price dropped) |
| `new_product_ts` | dict pid → epoch | replaced | when each new product was created |
| `oos_since` / `back_in_stock_ts` | dict pid → epoch | replaced | stock history (e.g. avoid items that flicker) |
| `now` | float | snapshot | event-time clock (max event ts) |
| `trend_rate` | dict pid → weighted events/h, 8-min half-life | snapshot ≤ 2 s | what is popular now (view 1, cart 3, purchase 6) |
| `trend_lift` | dict pid → float | snapshot | (short rate + a) / (3 h rate + a) / shop-wide lift; 1 = normal, > 1 trending |
| `trend_global_lift` | float | snapshot | shop-wide short/long traffic ratio |
| `trend_top` | list[pid] (300, available only) | snapshot | by `trend_rate` |
| `trend_rising` | list[pid] (300, available only) | snapshot | by `trend_lift`, min support 2 weighted events |
| `trend_top_by_subcat` | dict subcat → list[pid] (30) | snapshot | available, by `trend_rate` |
| `subcat_lift` | dict subcat → float | snapshot | recent share of activity (5-min half-life) / long-run share from history (`live.seed_history`), shrunk to 1; falls back to a 3 h live counter |
| `brand_lift` | dict brand → float | snapshot | lift at brand level (simulator trends hit a subcategory or a whole brand) |
| `buy_rate` | dict pid → purchases/h (16-min half-life) | snapshot | organic purchases only |
| `ctx_top` | dict (device, region) → list[pid] (30) | snapshot | carts+purchases, 1 h half-life — cold start for new shoppers |
| `device_top` | dict (device,) → list[pid] (30) | snapshot | same, by device only |
| `coview` | dict pid → tuple[(pid, weight)] (top 30) | lookup-only, ≤ 2 s | live co-views (same session, ≤ 30 min apart, 6 h half-life); covers new products |
| `new_neighbours` | dict new pid → tuple[pid] (10) | replaced on new_product | content neighbours (subcat, brand, price, description words) |
| `similar_new` | dict pid → tuple[new pid] (≤ 5) | replaced on new_product | reverse map: new look-alikes of an established product |
| `slot_cvr` / `slot_ctr` | list[10] | every 50 feedback rows | purchase / click rate by slot in our carousel (position curve) |
| `calib_subcat` | dict subcat → multiplier | same | observed / expected purchases, Gamma prior towards 1 |
| `calib_product` | dict pid → multiplier | same | shrunk towards its subcategory |
| `calib_posterior` | dict pid → (shape, rate) | same | Gamma posterior of the multiplier (Thompson sampling) |
| `impressions` | dict pid → int | same | times shown in our carousel |
| `reliability` | dict | same | last 2,000 answers: `team_rate`, `p50_ms`/`p95_ms`/`p99_ms` (organisers' latency), `fallback_reasons` |
| `_learner`, `_subcat_index` | internal | — | do not read |

| `ab` | `ABTest` | set by `rtrec.live.ab.install` | the running A/B test (arms, salt) |
| `ab_report` | dict | every 50 feedback rows | per arm: `n`, `purchases_per_100`, `ci95`, cold, clicks, revenue, `team_rate`, `diff_vs_champion`, `diff_lower_bound`; `promote` = arm name or None |

## Exploration (`rtrec/live/bandit.py`)

`ThompsonExplorer.apply(req, ranked, state)` re-fills slots 9–10 (0-based 8, 9) by Thompson sampling
over a pool (base ranks 9–20 + related new products) using `calib_posterior` (unseen products: their
subcategory prior with high variance) times a rank-decay relevance prior. RNG seeded by `request_id`
(reproducible, pure). `explorer.log` holds `(request_id, slot, pid, propensity)` for off-policy
evaluation; the runtime may drain it into `team_NN.scratch`.

## A/B (`rtrec/live/ab.py`)

`ABTest([("v1", .5), ("v2", .5)], salt="ab1")`: arm = crc32(salt + request_id) bucket. Feedback is
attributed by recomputing the arm (intention-to-treat). `ABRouter(test, {"v1": p1, "v2": p2})` is a
Policy; **the runtime must write `router.version_for(req)` as `model_version`** when the policy has
that method. Promotion: both arms >= `min_n` and one-sided 95 % lower bound of the difference > 0.

## Policies (`rtrec/live/policy.py`)

- `LiveSessionPolicy`: model-free, live-state-only session heuristic (cold-start specialist, reference).
- `LiveWrapper(base, cold_merge, rerank, explore, alpha, beta, ...)`: adds live learning to any base
  policy (A3's ranker): merges cold candidates on cold requests, re-ranks the head by
  rank prior × calibration^alpha × trend-lift^beta × repeat-purchase penalty, explores slots 9–10.

## Measuring (`rtrec/live/bench.py`)

`python -m rtrec.live.bench --minutes 30` replays A2's simulator through the hooks and prints, per
policy, the organisers' normalised score (global and cold) computed with `sim.expected_purchase`.

Helpers for policies (pure): `rtrec.live.coldstart.cold_candidates(req, state)`,
`new_product_prior(pid, state)`, `similarity(a, b)`.

## Live features for the ranker (`rtrec/live/features.py`)

`live_features(req, pid, state) -> dict` with keys `LIVE_FEATURES`; neutral defaults for unseen
products (lift 1 → log 0, ratio 1, rank 99): `trend_rate_log`, `trend_lift_log`, `subcat_lift_log`,
`buy_rate_log`, `price_ratio_to_base`, `price_last_change`, `price_changed_recently`, `is_new_product`,
`minutes_since_new`, `back_in_stock_recent`, `calib_mult_log`, `calib_subcat_log`, `impressions_log`,
`ctx_rank`, `coview_session`. No ids. For training, the same values can be rebuilt offline by
replaying history through the hooks (they depend only on event time).

## What the real stream looks like (practice round, 14-nov 09:00–12:25 UTC)

Read-only analysis of `shop.catalog_updates`, `shop.events`, `shop.requests`, `shop.history_events`
(aggregations in RawTree SQL; replay through `rtrec.live`). Numbers will be refreshed during the day.

- **Catalogue updates (930)**: in 5-minute batches at round timestamps. 3 products go out of stock every
  5 min and come back exactly 60 min later; `out_of_stock` rows carry `is_active=False`. 100 new
  products all at 10:00; 620 price changes all at 10:30, all discounts (median −19 %, range 0–20 %).
  No duplicates, no same-product ts ties, no out-of-order rows, no price change on an out-of-stock
  product. Both catalogue trackers give the same available set, even after shuffling/duplicating the
  rows: the two-clock rule is a safety net, not something today's stream needs.
- **Trends are real, big and at subcategory level**: Aquariums 09:30–10:15 and Kettles 11:30–12:15
  (45 min each, switched on/off as a step). During a trend the subcategory's share of views is ×4.5–4.8
  its history share, of carts and purchases ×11–14, and purchases per view ×2–3. No brand-level
  trend so far (brand lifts at 11:30 are just the kettle brands).
- **Detection**: replaying 11:00–12:25 through `TrendTracker` with history shares, `subcat_lift`
  puts Kettles at rank 0 one minute after onset (1.7 at +1 min, 3.0 at +5 min, ≈5 at plateau); the
  runner-up never exceeds ≈1.4, so a threshold around 1.7 separates trend from noise. It fades
  ≈8 min after the trend ends (5-min half-life).
- **New products**: all 100 got organic events within 16 min (median first event ≈1 min after
  creation); median 9 events in 15 min, 38 in 1 h; heavily skewed (best: 910 events, 128 purchases).
  They over-index on purchases (2.7 % of purchases for 2.0 % of products): a novelty effect.
- **Requests**: 30 % new shoppers, 20 % without recent views. Events ≈66/s.
- **Cost**: real events cost ≈120 ms of CPU per 1,000 rows in the hooks (≈1 % of one core at 66/s);
  a 2,000-row backfill chunk ≈240 ms, above A1's 50 ms per-call guideline, so backfill should use
  chunks of ≤ 400 rows.
