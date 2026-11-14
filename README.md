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
