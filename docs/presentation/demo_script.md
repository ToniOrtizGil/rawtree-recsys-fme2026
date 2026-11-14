# Jury talk + demo script (≈ 6 min talk + 3 min demo)

Audience: a product manager. Say "shopper", "purchases per 100 shoppers", "new products" — not "IPS",
"cold start", "LightGBM" — unless a juror asks. Every claim must be something we can open in the repo.

Deck: `docs/presentation/slides.html` (open in a browser, ← →, P for PDF). Numbers in ⟨…⟩ are filled from
`README.md` in this folder before 13:00 Sunday.

## Before going on stage
- Laptop A (deck) full screen. Laptop B (demo) with the dashboard already open on the **3 h window**.
- If production is still running: dashboard on the live store (`RTREC_STORE=rawtree python -m dashboard serve`).
  Otherwise the replay: `python -m dashboard serve --store fake --with-runtime --policy <final policy>`,
  started 3 minutes earlier so the charts have data. Last resort, no network: `--store demo` (it says
  **DEMO DATA · synthetic** on the page; say so out loud).
- Screenshots of the dashboard taken at 12:05 (RUNBOOK §1) in `docs/presentation/` as a backup.

## Talk track

**Slide 1 · The problem (45 s)**
> "Every third of a second a shopper lands on the shop. We have three seconds to choose the ten products in
> their carousel. Today everybody sees the same best sellers, whoever they are and whatever they were looking at.
> We are scored on how many more purchases we get than that list, relative to a perfect list, on shoppers,
> products and trends nobody has seen before, and an answer that is late or includes one sold-out product
> is thrown away."

**Slide 2 · What we built (75 s)**
> "Left to right: a shopper arrives with what they just viewed. We gather about 300 sensible candidates (what
> they viewed, similar products, what is trending, new arrivals), rank them by the probability that this
> shopper buys them, check the list is valid, and write it back, in under ⟨p95⟩ seconds."
> Point to the three boxes: *generalises* (no IDs, so new shoppers and new products work on day one),
> *fast* (no database on the critical path), *cannot fail silently* (three fallbacks and a standby machine).

**Slide 3 · Method (75 s)**
> "We didn't go live on a hunch. The old system logged two million carousels, but it mostly showed popular
> things, so a naive replay flatters popular products. We corrected for that using how likely each item was
> to be shown. And because that correction can be wrong, we first checked it on a simulated shop where we
> know the true answer. A version only went live if it won offline without hurting new shoppers, and then
> it had to win an A/B test with 95 % confidence."
> Read the table: best sellers 0 → session rules ⟨⟩ → ranker ⟨⟩ → final ⟨⟩.

**Slide 4 · Learning from the stream + RawTree (75 s)**
> "The shop changes every minute: trends, price drops, sell-outs, products launched mid-event. Everything
> on the left updates within seconds, from the live tables. New shoppers are treated from their first click;
> new products borrow the record of similar products until they have their own."
> Right box: "RawTree is both our memory and our monitor. The heavy features are SQL inside RawTree; we read
> with incremental cursors and write in batches; heartbeats in a scratch table let a standby take over; and
> every number on our dashboard is also a SQL query in RawTree. I'll show you."

**Slide 5 · Results (45 s)** → switch to laptop B.

## Demo (3 min) — dashboard
1. **The light** (top left): "This is our estimate of the organisers' green light: share of answers that were
   valid and on time in the last 5 minutes. During the final it stayed at ⟨…⟩."
2. **Per-minute charts**: "% on time against the red line at 90 %, latency against the 3-second deadline,
   conversion over time."
3. **A/B table**: "Purchases per 100 shoppers per model version, with a 95 % interval: when intervals
   overlap we don't claim a winner. Cold vs warm columns: new shoppers and products." Mention the fallback
   row: "this is what the shopper got when we were too slow or invalid, the best sellers."
   If asked: A4's promotion rule counts fallbacks against the arm that caused them (intention-to-treat);
   the dashboard lists them apart, so the live table is not the promotion decision itself.
4. **Live feed** (scroll): pick a shopper with views. "They looked at ⟨X⟩; slot 1 is something they viewed,
   then the same subcategory, then a trending one. The tags are the families of signals the ranker uses."
   Point at a *purchased* outline if one is visible.
5. **SQL panel** (bottom): open "Purchases/100 by model_version". "The same number, computed inside RawTree:
   it joins our submissions to the organisers' feedback." If the store is live, the result table is right
   under the query.
6. (Optional, 20 s) Open `/runbook` from an alert link: "Every alert tells the person on duty what to do."

## Likely questions (one-line answers)
- *Why not just best sellers per category?* Because the session tells us much more: ⟨forensics H1 number⟩.
- *How do you handle a product that sells out mid-answer?* Stock refreshed every ≤0.5 s, validated before
  writing; we measured ⟨n⟩ invalid answers in the final.
- *How do you avoid overfitting to practice day?* No ID features; validation on a cold segment; the final had
  new shoppers and products and our cold score was ⟨…⟩.
- *What if your machine dies?* Standby on a second machine reads our heartbeat and takes over;
  we rehearsed it at 09:15 (RUNBOOK §2.8).
- *Did you use LLMs / agents?* Yes, to write code in parallel against a fixed contract
  (`rtrec/contract.py`); every module has tests and we can walk through any of it.
