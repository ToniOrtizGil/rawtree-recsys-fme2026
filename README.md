# rtrec · Real-time recommender for the RawTree challenge

Datathon FME 2026 · UPC Barcelona. Every ~0.33 s a shopper arrives; within 3 s we choose the 10 products
they see. Scored on expected purchases per 100 requests vs. a best-seller baseline and an oracle.

```
shop.requests ─▶ poll ─▶ candidates ─▶ LightGBM P(purchase) ─▶ finalize (10 valid) ─▶ team.submissions
shop.events / catalog_updates / team.feedback ─▶ live state (trends, stock, prices, profiles, calibration)
```

- Plan and everything we know about the challenge: [`docs/memory/`](docs/memory/MEMORY.md)
- Who builds what: [`docs/TASKS.md`](docs/TASKS.md) · rules for agents: [`CLAUDE.md`](CLAUDE.md)
- Organisers' starter kit (reference copy): [`docs/upstream/`](docs/upstream/)

## Quick start

```bash
pip install -e '.[dev]'
python -m pytest -q
```

Without an API key everything runs against the local simulator (`RTREC_STORE=fake`).

## How it works

In plain words, for each shopper:

1. **Read**: the serving loop polls `shop.requests` every ~0.2 s with an incremental cursor (`rtrec/runtime`).
2. **Candidates**: about 300 products that make sense for this session: what they viewed, similar products,
   what is trending, new arrivals related to the session (`rtrec/model`).
3. **Rank**: by the probability that this shopper buys each product. The model uses relationships (same
   subcategory as a viewed item, price relative to what they look at, trend, context), never product or
   shopper IDs, so it works for the new shoppers and products of the final.
4. **Validate**: `rtrec/validate.finalize` guarantees exactly 10 distinct, active, in-stock products; anything
   wrong is repaired from a safe best-seller list.
5. **Write**: answers are batched into `team_NN.submissions`. A heartbeat in `team_NN.scratch` lets a standby
   machine take over if the primary stops.

In the background, the live state (`rtrec/live`, see [`docs/LIVE.md`](docs/LIVE.md)) follows the stream:
stock and prices within 0.5 s, decayed trend counters, live shopper profiles, calibration from our own
feedback, exploration on the last two slots, and A/B tests between model versions.

## Demo

A live dashboard reads the same tables as the organisers, so it keeps working if the serving machine dies.
It shows our estimate of the organisers' green light, latency, fallback reasons, purchases per 100 per
`model_version` (with 95 % intervals) for cold vs warm shoppers, trends, and the last shoppers with the 10
products they saw and why. Standard library only.

```bash
python -m dashboard serve --store demo                   # synthetic fixture, no key → http://127.0.0.1:8050
python -m dashboard serve --store fake --with-runtime    # our real serving loop on the local simulator
RTREC_STORE=rawtree python -m dashboard serve            # live (needs TEAM_ID + key)
python -m dashboard check                                # one line; exit 0 ok / 1 warning / 2 critical
python -m dashboard sqlcheck --store rawtree             # run every panel's SQL twin in RawTree once
```

Every panel also has a SQL version that runs inside RawTree (`dashboard/queries.py`). They are tested
against embedded ClickHouse with RawTree-style `Dynamic` columns (`pip install -e '.[sqltest]'`), which checks
they return the same numbers as the Python metrics.

- Sunday procedure, alerts and what to do: [`ops/RUNBOOK.md`](ops/RUNBOOK.md)
- Jury deck and demo script: [`docs/presentation/`](docs/presentation/README.md)
