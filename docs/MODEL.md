# The model (A3): how we choose the 10 products

> Status: built and measured **on our synthetic simulator only** (A2's `sim/shop.py`). No number
> here comes from RawTree; we will re-run the same scripts on the real history on day one.

## In one paragraph (for a product manager)

Every ~0.3 s a shopper opens the carousel. We look at what they just viewed, what they did before,
the device, region and page, and what is selling in the shop right now. From that we shortlist about
300 plausible products, estimate for each one **the probability that this shopper buys it if we show
it**, and show the 10 most likely, best first (the first slots get most of the attention). The model
never memorises product or shopper IDs; it learns *relations* (“same subcategory as what you just looked
at”, “cheaper than what you usually buy”, “selling 3x faster than usual right now”), so it keeps working
on Sunday's brand-new shoppers and products.

## Three versions, all behind the same interface

| Version | `model_version` | What it does | Needs training |
|---|---|---|---|
| Baseline | `baseline` | The organisers' fallback: all-time best sellers in stock. Score 0 by definition. | no |
| v1 | `v1-session-heuristic` | Same shortlist and signals as v2, combined with hand-set weights. Also the safety net if the model file is missing. | no |
| v2 | `v2-lgbm` | Two LightGBM models: P(click) × P(buy given click), learned from the old carousel's logs. | yes |

All three implement `rtrec.contract.Policy`: `recommend(request, state)` is pure (reads only `State`),
takes 1–3 ms, and its answer always goes through `validate.finalize` (10 distinct, active, in-stock ids).
They can be built as `Policy(state, dump)` at start-up (that is how `scripts/arbiter.py` builds them).

## Results on the simulator (ground truth, no luck)

Score = `100 × (ours − baseline) / (oracle − baseline)`, on expected purchases computed by the
simulator's hidden model, exactly like the leaderboard. Paired evaluation: every request answered
by every policy on the same live state (`scripts/eval_sim.py`).

| Policy | Score (all) | ±95% | Cold requests | Warm | p99 latency |
|---|---|---|---|---|---|
| Baseline | 0 | — | 0 | 0 | 0.1 ms |
| v1 heuristic | **66.2** | 3.7 | 67.2 | 65.1 | 3.5 ms |
| v2 LightGBM | *(training)* | | | | |

Seed 7, 5 min warm-up + 10 min scored (1,803 requests, 920 cold). Purchases per 100 requests:
baseline 2.49, v1 8.73, oracle 11.92.

## How a recommendation is made

1. **Shortlist (`rtrec/model/candidates.py`, ~300 products).** Sources, each tagged so the model knows
   why a product is there: the products just viewed; products viewed together with them (history
   co-views and live co-views); best sellers of the subcategories and brands in the session and the
   shopper's profile; what is trending now (overall and per subcategory); best sellers for this
   device/region; **new products that look like what they viewed** (cold start); all-time best sellers.
2. **Features (`rtrec/model/features.py`, 42 numbers per product, no IDs).**
   - *Session*: was it viewed, how recently, recency-weighted share of views in the same subcategory /
     category / brand, co-view strength, price vs. what they viewed.
   - *Product*: price (absolute and vs. its subcategory), conversion rate from organic events, carousel
     lift (purchases vs. what its slots predict, i.e. position-debiased), popularity, age, new or not.
     New products borrow these from their content neighbours, then from their subcategory.
   - *Shopper*: affinity to the subcategory/category from their history, already bought it (a shopper
     rarely buys the same thing twice), carted it, new shopper.
   - *Context*: device, page, hour, how much this region / device likes this category, its price appetite.
   - *Content*: overlap of the description's words with the session's and the profile's (style taste).
   - *Live*: trend lift of the product and its subcategory, live sales rate, price change vs. launch.
3. **Score.** v2: `P(click | slot 0) × P(buy | click)`; v1: a hand-weighted log-score of the same
   features. Then a live correction from our own feedback (A4's calibration multipliers).
4. **Order** by score, best first; `finalize` guarantees validity.

## Why two models (click, then buy)

Purchases are rare in the log (~1 impression in 80 ends in a purchase on the simulator); clicks are
~10x more frequent. Learning “would they click” from many examples, and “would they buy once
interested” from the clicked rows, is far more data-efficient than learning purchases directly. The
slot is a feature of the click model (the log shows products in all 10 slots, so it learns the
position bias); at serving time we set slot = 0 so products compete on their own merit.

## How we validate offline (the “Method” criterion)

- **Temporal split** of the 60-day history (`rtrec/offline/trainset.py`): days 0–21 build the priors and
  co-views, days 21–51 give the training rows (features computed *as of each impression*, by replaying
  the history through the same live code A4 runs in production), days 51–60 are the test window.
- **Same code offline and live**: training features come from `featurize()` on a `State` rebuilt by
  `rtrec.live` (A4's updaters); there is no separate “offline feature” implementation to drift.
- **Off-policy estimators** (`rtrec/offline/estimators.py`): IPS, self-normalised IPS (SNIPS), direct
  method (DM, the model's own prediction) and doubly robust (DR) estimates of purchases per 100, slot
  by slot, with 95% intervals and the number of matched (impression, slot) pairs.
- **Unique check — are the estimators unbiased?** (`rtrec/offline/estimator_check.py`). On the simulator
  we log a stream with a known-randomness “old carousel”, and compare each estimator to the true value
  for each policy. *(Numbers to be filled in.)*

## Contract with the live system (A4) — `State` keys we read

From `state.offline` (built by `rtrec.model.artefacts.build_offline` at start-up):
`best_sellers`, `prod` (ProdStats per product), `sub` (SubStats per subcategory), `sub_top`, `ctx_top`
(per device), `coview`, `slot_rate`, `ctx_cat`, `ctx_price`, `tokens`, `idf`, `ranker` (the model).

From `state.live` (A4, see docs/LIVE.md), all optional — missing keys mean neutral values:
`trend_lift`, `subcat_lift`, `trend_rate`, `buy_rate`, `trend_top`, `trend_top_by_subcat`, `ctx_top`
(per device, region), `coview`, `new_neighbours`, `similar_new`, `price` (PriceInfo),
`calib_product`, `calib_subcat`. Plus `state.users` profiles (views, carts, purchases incl. carousel
purchases from our feedback), `state.available`, `state.new_products`, `state.catalog`, `state.safe_list`.

## How to reproduce

```bash
python scripts/train_ranker.py --sim --seed 7            # -> artifacts/ranker.txt (+ .buy.txt, .json, .metrics.json)
python scripts/eval_sim.py --seed 7 --minutes 10 --check-estimators 30
python scripts/arbiter.py rtrec.model.policies:RankerPolicy --seed 7     # master's official procedure
# real data (after scripts/download_history.py):  python scripts/train_ranker.py --parquet data/
```
