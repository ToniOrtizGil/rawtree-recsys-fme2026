# Presentation kit

| File | What |
|---|---|
| `slides.html` | 5 slides, "from best sellers to a system that learns". Open in a browser; ← → to move, **P** prints to PDF (one slide per page). |
| `demo_script.md` | Talk track per slide, the 3-minute dashboard demo, likely questions. |

## Numbers to fill before presenting (never estimate: if a source is missing, drop the claim)

| Placeholder | Slide | Source |
|---|---|---|
| ⟨p95⟩ answer time | 2 | dashboard tile "Latency p50 / p95", 3 h window, or organisers' query 6 |
| v1 / v2 offline score, all + cold | 3 | `docs/MODEL.md` (A3), `scripts/arbiter.py` output |
| final version score, all + cold | 3 | `scripts/arbiter.py` output for the frozen version |
| ⟨21,600⟩ shoppers | 5 | `requests` in the dashboard's "all served by us" + fallback rows, 2 h window (exact count) |
| % valid & on time, p95 | 5 | dashboard "Fallback reasons" table, window covering 10:00–12:00 |
| purchases/100 (ours vs fallback), interval | 5 | dashboard A/B table, row "all served by us" and "(fallback)" |
| cold purchases/100, n, warm | 5 | dashboard A/B table, cold / warm columns |
| two "what we learned" sentences | 5 | `docs/FORENSICS.md` (A2), A/B log (A4) |
| invalid answers in the final | Q&A | dashboard "Fallback reasons" |

Claims on slide 4 that depend on other agents' work: verify before presenting.
- "Features in SQL … rtrec/sql": A2's `rtrec/sql/*.sql` must exist and be used. If not, say "computed in
  pandas from RawTree extracts" instead.
- "8-minute half-life" and "exploration on slots 9–10": `docs/LIVE.md` (A4). Check they are on in the final config.
- "batched every ~0.2 s, retried without duplicating" and "standby takes over": `rtrec/runtime` (A1).
