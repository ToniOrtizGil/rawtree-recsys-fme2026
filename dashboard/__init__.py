"""Live dashboard and alerts for the serving system (agent A5).

Why it exists: during the final (Sun 10:00-12:00 CET) the organisers hide the leaderboard and only show a
green/red light (>90 % of answers valid and on time over the last 5 minutes). We need to see, before that
light turns red, whether we are answering, how fast, and whether the model is converting, per
model_version and for cold vs warm shoppers.

Design rules
- Read-only: the dashboard never writes to team_NN.submissions (one writer only). It reads through any
  rtrec.store.Store, so the same code runs on the demo fixture, the local simulator and live RawTree.
- Metrics are computed in Python from incremental reads (works on every store) and, when the store can run
  SQL (RawTree), also by the SQL in dashboard/queries.py, so the page shows both and that they agree.

    python -m dashboard serve --store demo      # synthetic fixture, no key needed
    python -m dashboard check --store rawtree   # one-line health check, exit code 0 ok / 1 warn / 2 crit
"""
