# Forensics: what the shop rewards (H1–H11)

**Status: numbers below are from our SYNTHETIC shop (`sim/`, seed 7, 60-day history + 2 h of live stream).**
They prove the analysis pipeline works and recover what we built into the simulator; they say nothing yet about
the real shop. On day one with the key, rerun and replace this file:

```bash
python scripts/download_history.py --out data --live          # real dump (+ live snapshot)
python -m rtrec.data.forensics --data data > /tmp/findings.md  # same code, real numbers
jupyter nbconvert --to notebook --execute notebooks/forensics.ipynb
```

Method: every question is a function in `rtrec/data/forensics.py`; the notebook only calls them. Click and purchase
rates are compared **per unit of examination** (clicks / Σ examine[slot], using the position curve fitted in H2), so a
product property is not credited for the slot the old carousel happened to give it. The position curve itself comes
from a rank-1 Poisson fit over (product, slot) counts (`rtrec/data/features.fit_position_bias`): the old system's
randomness puts the same product in different slots, which separates "good product" from "good slot".

## Answers and what they decide

| # | Question | Synthetic answer | Decision for the system |
|---|---|---|---|
| H1 | Does a just-viewed product convert? | Yes, ×18 clicks per examination | Viewed items are candidates and lead the list unless already bought |
| H2 | Position curve | Slot 10 = 0.14× slot 1; top 3 slots = 58 % of attention. Fitted curve matches the simulator's true one (test) | Ordering matters a lot; explore only in slots 9–10; use the fitted curve in offline estimators |
| H3 | What explains clicks | Same subcategory ×6.6, same brand ×4.7, same category ×3.4; price close to the viewed ones best | Session-similarity features are the core of the ranker and of candidate generation |
| H4 | Price × device / region | P(buy\|cart) slope on log price: android −0.56, web −0.38, ios −0.17; regions −0.6 … 0 | Price features interacted with device and region |
| H5 | Long-term history, repeat purchases | Already bought ×0.28; earlier affinity to the subcategory ×1.39 | Penalise already-bought items hard; user affinity is a useful, secondary feature |
| H6 | Do event trends predict carousel clicks? | Weak in the synthetic history (×1.15 at lag 0, gone after 30 min): history too sparse per subcategory (~2 views / 15 min) | Trend counters must be computed on the dense live stream (~50 events/s), half-life ≲ 30 min |
| H7 | Price changes | No price variation inside the history; live: purchases/view ×1.5 after a drop, ×0.95 after a raise | Feature "price vs launch price"; boost recently discounted items |
| H8 | Target shape | ≤ 1 click per impression, purchase ⇒ click on the same item; 2.1 % CTR, 58 % of clicks buy | Model P(click)·P(buy\|click) per item, or P(purchase) directly |
| H9 | Propensity structure | Exactly Plackett–Luce over a per-day weight vector; 52 % of propensities < 1e-3 | Off-policy: per-slot IPS / SNIPS with clipping; list-level IPS is hopeless |
| H10 | Live request mix | 2.95 req/s, 50 % new shoppers, 25 % without views, 20 % of users come back | Cold start is half the traffic: context popularity × trends for no-view requests |
| H11 | Stock-outs right after answering | ~400 stock-outs/h, 30/h among the top 100; P(list of top items invalid within 3 s) ≈ 0.25 % | Refresh stock ≤ 0.5 s before each insert; never pad with stale best sellers |

Caveats to recheck on real data: the simulator has no price changes in history (H7), assumes the click model is
"examination × attraction" (H2/H8 test it), and its trend strength is our guess (H6).

## Full output (synthetic)

<!-- source: {'source': 'synthetic', 'seed': 7, 'days': 60, 'small': False} -->

### H1 Does showing a product already viewed in the session convert?

**Yes if > 1: a just-viewed product gets x18.09 clicks and x18.60 purchases per unit of examination vs other shown products (2151 pairs).**

| viewed | pairs | clicks | purchases | exposure | ctr_per_exam | cvr_per_exam | p_buy_given_click |
|---|---|---|---|---|---|---|---|
| False | 1502419 | 3041 | 1760 | 5.033e+05 | 0.006042 | 0.003497 | 0.5788 |
| True | 2151 | 79 | 47 | 722.7 | 0.1093 | 0.06503 | 0.5949 |

### H2 Position bias curve (clicks per slot, corrected)

**Slot 10 is examined 0.14x as much as slot 1 (fitted, product effects removed); the raw click curve says 0.12x. Top 3 slots carry 58% of the attention.**

| slot | impressions | clicks | purchases | mean_propensity | raw_ctr | raw_rel | fitted_examine |
|---|---|---|---|---|---|---|---|
| 1 | 150457 | 984 | 593 | 0.02298 | 0.00654 | 1 | 1 |
| 2 | 150457 | 511 | 286 | 0.02139 | 0.003396 | 0.5193 | 0.5328 |
| 3 | 150457 | 397 | 238 | 0.01986 | 0.002639 | 0.4035 | 0.423 |
| 4 | 150457 | 260 | 150 | 0.01864 | 0.001728 | 0.2642 | 0.2827 |
| 5 | 150457 | 235 | 139 | 0.01751 | 0.001562 | 0.2388 | 0.2596 |
| 6 | 150457 | 200 | 120 | 0.01617 | 0.001329 | 0.2033 | 0.2266 |
| 7 | 150457 | 159 | 86 | 0.01507 | 0.001057 | 0.1616 | 0.1826 |
| 8 | 150457 | 142 | 67 | 0.01421 | 0.0009438 | 0.1443 | 0.1655 |
| 9 | 150457 | 117 | 68 | 0.01345 | 0.0007776 | 0.1189 | 0.1386 |
| 10 | 150457 | 115 | 60 | 0.01251 | 0.0007643 | 0.1169 | 0.1384 |

### H3 What explains the click: same subcategory, category, brand, price close to what was viewed?

**Strongest single signal: same_subcategory (x6.63 clicks per examination). By price ratio to the viewed products: <0.5: 0.0056, 0.5-0.8: 0.0073, 0.8-1.25: 0.0083, 1.25-2: 0.0076, >2: 0.0047 clicks/exam.**

| feature | share_of_pairs | click_lift | purchase_lift | pairs | ctr_per_exam | cvr_per_exam |
|---|---|---|---|---|---|---|
| same_subcategory | 0.02512 | 6.631 | 6.545 | nan | nan | nan |
| same_category | 0.128 | 3.35 | 3.205 | nan | nan | nan |
| same_brand | 0.02652 | 4.656 | 5.072 | nan | nan | nan |
| price_ratio <0.5 | nan | nan | nan | 4.207e+05 | 0.005614 | 0.003352 |
| price_ratio 0.5-0.8 | nan | nan | nan | 1.821e+05 | 0.007274 | 0.004556 |
| price_ratio 0.8-1.25 | nan | nan | nan | 1.666e+05 | 0.008323 | 0.004891 |
| price_ratio 1.25-2 | nan | nan | nan | 1.422e+05 | 0.007619 | 0.004219 |
| price_ratio >2 | nan | nan | nan | 2.175e+05 | 0.004716 | 0.002211 |

### H4 Does purchase-given-interest depend on price x device / region?

**Logit slope of P(buy | cart) on log(price), by device: android -0.56, ios -0.17, web -0.38; across regions it ranges -0.62 .. +0.01. More negative = more price sensitive.**

| dim | value | carts | p_buy_given_cart | log_price_slope |
|---|---|---|---|---|
| device | android | 71737 | 0.3715 | -0.5644 |
| device | ios | 85323 | 0.3018 | -0.1723 |
| device | web | 127919 | 0.3384 | -0.3849 |
| region | Andalucía | 52288 | 0.3462 | -0.4711 |
| region | Aragón | 8272 | 0.3118 | -0.2773 |
| region | Asturias | 6055 | 0.3364 | -0.4593 |
| region | Baleares | 6398 | 0.2821 | 0.01151 |
| region | Canarias | 14710 | 0.3279 | -0.4305 |
| region | Cantabria | 2869 | 0.2956 | -0.1211 |
| region | Castilla y León | 13935 | 0.304 | -0.2902 |
| region | Castilla-La Mancha | 14127 | 0.3263 | -0.1965 |
| region | Catalunya | 41321 | 0.3134 | -0.2037 |
| region | Comunidad Valenciana | 31610 | 0.3532 | -0.458 |
| region | Extremadura | 6939 | 0.3464 | -0.502 |
| region | Galicia | 16134 | 0.3795 | -0.6039 |

### H5 Does long-term history predict? How much does having bought it already hurt?

**Already bought: x0.28 clicks per examination (9395 pairs). Subcategory seen in an earlier session: x1.39. 75% of impressions are by shoppers already active in the first week of history.**

| feature | bought_before | pairs | clicks | purchases | exposure | ctr_per_exam | cvr_per_exam | p_buy_given_click |
|---|---|---|---|---|---|---|---|---|
| bought_before | False | 1495175 | 3114 | 1804 | 5.006e+05 | 0.00622 | 0.003604 | 0.5793 |
| bought_before | True | 9395 | 6 | 3 | 3399 | 0.001765 | 0.0008827 | 0.5 |
| known_affinity | False | 1191149 | 2281 | 1328 | 3.987e+05 | 0.005721 | 0.003331 | 0.5822 |
| known_affinity | True | 313421 | 839 | 479 | 1.053e+05 | 0.007966 | 0.004548 | 0.5709 |

### H6 Do trends in the organic events predict carousel conversion? With what delay?

**Products whose subcategory is in the top 10% of 15-min view lift get x1.15 clicks per examination right away; the signal is strongest at lag 0 min and x0.90 after 120 min (decay = half-life of the counters).**

| lag_min | hot_threshold_lift | click_lift_hot_vs_rest | purchase_lift_hot_vs_rest |
|---|---|---|---|
| 0 | 3 | 1.146 | 1.177 |
| 15 | 3 | 1.055 | 1.126 |
| 30 | 3 | 0.8829 | 0.9215 |
| 60 | 3.158 | 0.9801 | 0.9373 |
| 120 | 3.429 | 0.9032 | 0.9488 |

### H7 Effect of a price change

**0.0% of products change price within the history. Live: purchases per view x1.52 after a price drop, x0.95 after a raise.**

| kind | after | add_to_cart | purchase | view | buy_per_view |
|---|---|---|---|---|---|
| drop | False | 4876 | 1566 | 19902 | 0.07869 |
| drop | True | 7089 | 2637 | 22046 | 0.1196 |
| raise | False | 3012 | 1026 | 10871 | 0.09438 |
| raise | True | 3419 | 1170 | 13075 | 0.08948 |

### H8 At most one click / purchase per impression? Does purchase imply click?

**One clicked and one purchased field per impression; purchased == clicked in 100% of purchases; 2.07% of impressions get a click, 1.20% a purchase (58% of clicks convert).**

| impressions | click_rate | purchase_rate | p_buy_given_click | purchase_equals_click | purchase_without_click | revenue_per_impression |
|---|---|---|---|---|---|---|
| 150457 | 0.02074 | 0.01201 | 0.5792 | 1 | 0 | 0.1571 |

### H9 Structure of `propensity`

**Consistent with a Plackett-Luce draw over a per-day weight vector (slot-k propensity predicted from slot-1 propensities with median error 0.0%), median propensity 0.0008, 52% below 1e-3 (IPS weights > 1000: clip or use SNIPS / per-slot estimators).**

| min | p01 | median | p99 | share_below_1e-3 | pl_check_pairs | pl_median_rel_error | slot1_top_product_propensity |
|---|---|---|---|---|---|---|---|
| 5.937e-05 | 6.305e-05 | 0.0008114 | 0.1339 | 0.5186 | 430902 | 5.399e-06 | 0.1268 |

### H10 Live requests: rate, % new shoppers, % without views, repetition

**2.95 requests/s, 50% new shoppers, 25% without recent views (mean 2.3 views); pages product 45%, home 29%, category 26%. Visibility delay of requests is measured by the runtime (latency_ms in feedback), not here.**

| requests | per_second | new_user_share | no_view_share | mean_views | requests_per_user | users_seen_twice_share |
|---|---|---|---|---|---|---|
| 21220 | 2.947 | 0.4972 | 0.2502 | 2.256 | 1.258 | 0.1974 |

### H11 Products going out of stock right after we answer

**396 stock-outs/hour (30 among the top 100); a 10-item list of top products goes invalid within 3s with p = 0.25% -> refresh stock <= 0.5 s before sending. Measured in feedback: 0.00% invalid.**

| oos_per_hour | oos_per_hour_in_top | p_list_invalid_within_horizon | feedback_invalid_share |
|---|---|---|---|
| 396.2 | 30.05 | 0.002501 | 0 |

