# Rules for every agent working in this repo

Read first: `docs/memory/MEMORY.md` (challenge, data, RawTree API, starter kit, plan) and `docs/TASKS.md` (who owns what).

## Situation
- RawTree challenge, Datathon FME 2026. Only Sunday 15-nov 10:00–12:00 CET counts (09:00–11:00 UTC).
- **We have NO API key yet** (the team is still choosing between two challenges). `api.rawtree.com` is blocked
  from these containers. Build and test everything against the local simulator (`sim/`) through
  `rtrec.store.Store`. Never mock results and call them real; never claim something was tested live.
- When the key arrives the master session switches `RTREC_STORE=rawtree` and redeploys. Your code must work
  with both stores unchanged.

## Contract (do not break)
- `rtrec/schemas.py`, `rtrec/contract.py`, `rtrec/store.py`, `rtrec/validate.py`, `sim/base.py` belong to the
  master. Need a change? Send the master a message explaining it; don't edit them yourself.
- `Policy.recommend(req, state)` is pure (no I/O), < 5 ms per request. Every answer goes through `validate.finalize`.
- No product IDs or user IDs as model features: the final round has new shoppers and new products.
- RawTree SQL quirks: every column is Dynamic; cast join keys / argMax args (`product_id::String`,
  `ts::DateTime64(3)`). Only SELECT / WITH / INSERT…SELECT; no views, no UPDATE/DELETE.

## Workflow
- Work on branch `agent/<your-name>` only, touch only the paths you own (docs/TASKS.md). Commit small,
  push often: `git push -u origin agent/<your-name>`. Do not open pull requests; the master merges.
- Before pushing: `python -m pytest -q` must pass. Add tests for what you build.
- Python 3.11+, deps in `pyproject.toml` (`pip install -e '.[dev]'`). Add a dep there if you need one.
- Keep code readable: the jury reads the repo and "anything a team cannot explain may be penalised".
  Short module docstrings that say *why*.
- Never commit `.env`, keys, or data files (`data/`, `artifacts/` are git-ignored).

## Reporting to the master
- Master session id: `session_01XduZa73MPSwYUepqrdFyRP`. Report with the `send_message` tool:
  when you push a milestone, when you are blocked, and when you finish. Format:
  `[A<n>] DONE|PROGRESS|BLOCKED — <one line> · branch <x> @ <sha> · needs from user: <none|...>`
- Do not ask the user anything directly; the master relays. If something needs the human (a key, a
  machine, a decision), say so in your report under "needs from user".
