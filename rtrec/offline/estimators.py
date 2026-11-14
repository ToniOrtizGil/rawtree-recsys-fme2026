"""Off-policy estimates of "purchases per 100 requests" for a new policy, from carousel logs.

The old carousel logged, for each impression, the 10 products it showed and `propensity[k]`, the
probability that it put that product in slot k *given the products in the earlier slots*. A new
policy rarely shows the exact same list, so every estimator here decomposes the list into slots
(item-position model: a purchase of the slot-k product depends on that product and that slot):

  V(new) = sum_k  E[ purchase of new[k] at slot k ]

and corrects slot k with the impressions where the old system happened to show new[k] in slot k.

  ips_slot1   starter-kit estimator, slot 1 only (purchases, not clicks); a reference point.
  ips         sum over slots of  1[new_k = shown_k] * r_k / m_k(shown_k)
  snips       same, self-normalised per slot (lower variance, small bias)
  dm          direct method: sum_k q(x, new_k, k) from a reward model (the ranker)
  dr          doubly robust: dm + IPS correction of the reward model's error on matched slots

`m_k(p)` is the *marginal* probability that the old system shows p in slot k. The log only gives
the conditional one, so two options are provided: "conditional" (use propensity[k] as if it were
marginal: exact for slot 1, biased later) and "empirical" (estimate m_k(p) by counting how often
the old system put p in slot k; unbiased if the old policy does not depend on the shopper).
On the simulator we know the truth, so `rtrec.offline.validate_estimators` measures which of these
is unbiased before we trust any of them on RawTree's real logs.

Every estimate is a mean of per-impression terms, so it comes with a normal 95% CI and the number
of matched (impression, slot) pairs, which says how much evidence the number rests on.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence

import numpy as np

N_SLOTS = 10


def empirical_marginals(shown_lists: Sequence[Sequence[str]], n_slots: int = N_SLOTS) -> list[dict[str, float]]:
    """m_k(p) = share of impressions where the old system showed p in slot k."""
    n = max(len(shown_lists), 1)
    cnt = [Counter() for _ in range(n_slots)]
    for s in shown_lists:
        for k, p in enumerate(list(s)[:n_slots]):
            cnt[k][p] += 1
    return [{p: c / n for p, c in ck.items()} for ck in cnt]


def _ci(terms: np.ndarray) -> tuple[float, float]:
    m = float(terms.mean()) if len(terms) else float("nan")
    se = float(terms.std(ddof=1) / np.sqrt(len(terms))) if len(terms) > 1 else float("nan")
    return 100 * m, 100 * 1.96 * se


def estimate(new_lists: Sequence[Sequence[str]], shown: Sequence[Sequence[str]], propensity: Sequence[Sequence[float]],
             purchased: Sequence[str | None], q_new: np.ndarray | None = None, q_logged: np.ndarray | None = None,
             marginals: list[dict[str, float]] | None = None, n_slots: int = N_SLOTS,
             clip: float = 1e4) -> dict:
    """All estimators at once. Per-100 values with ±95% half-width.

    q_new[i, k]    reward model's P(purchase of new_lists[i][k] at slot k)   (optional, for dm/dr)
    q_logged[i, k] reward model's P(purchase of shown[i][k] at slot k)       (optional, for dr)
    marginals      from empirical_marginals(); if None, the conditional propensities are used."""
    n = len(new_lists)
    ips_t = np.zeros(n)
    s1_t = np.zeros(n)
    num = np.zeros((n, n_slots))
    den = np.zeros((n, n_slots))
    corr = np.zeros(n)
    matches = 0
    for i in range(n):
        new, sh, pr, y = list(new_lists[i]), list(shown[i]), list(propensity[i]), purchased[i]
        for k in range(min(n_slots, len(sh), len(new))):
            if new[k] != sh[k]:
                continue
            m = marginals[k].get(sh[k], 0.0) if marginals is not None else pr[k]
            if m <= 0:
                continue
            w = min(1.0 / m, clip)
            r = 1.0 if (y and sh[k] == y) else 0.0
            matches += 1
            ips_t[i] += w * r
            num[i, k], den[i, k] = w * r, w
            if k == 0:
                s1_t[i] = r / pr[0]
            if q_logged is not None:
                corr[i] += w * (r - q_logged[i, k])
    out = {"n": n, "matches": matches}
    out["ips_slot1"], out["ips_slot1_ci"] = _ci(s1_t)
    out["ips"], out["ips_ci"] = _ci(ips_t)
    # SNIPS per slot: sum_i w r / sum_i w  times the share of impressions (each slot normalised separately)
    sn = num.sum(0) / np.maximum(den.sum(0), 1e-12)
    out["snips"] = float(100 * sn.sum())
    # CI for SNIPS by the delta method, per slot then summed (slots treated as independent)
    var = 0.0
    for k in range(n_slots):
        if den[:, k].sum() > 0:
            term = (num[:, k] - sn[k] * den[:, k]) / den[:, k].mean()  # influence function of a ratio
            var += float(np.var(term) / n)
    out["snips_ci"] = float(100 * 1.96 * np.sqrt(var))
    if q_new is not None:
        dm_t = q_new[:, :n_slots].sum(1)
        out["dm"], out["dm_ci"] = _ci(dm_t)
        if q_logged is not None:
            out["dr"], out["dr_ci"] = _ci(dm_t + corr)
    return out


def reward_model_scores(score_fn: Callable[[int, list[str]], np.ndarray], lists: Sequence[Sequence[str]],
                        n_slots: int = N_SLOTS) -> np.ndarray:
    """q[i, k] = score_fn(i, lists[i])[k]; score_fn returns P(purchase) of each item at its slot."""
    q = np.zeros((len(lists), n_slots))
    for i, lst in enumerate(lists):
        v = score_fn(i, list(lst)[:n_slots])
        q[i, : len(v)] = v
    return q


def hit_rate(new_lists: Sequence[Sequence[str]], target: Sequence[str | None]) -> tuple[float, int]:
    """Share of impressions with a click/purchase whose product is in our top 10 (biased to popular items)."""
    hits = [t in set(lst[:10]) for lst, t in zip(new_lists, target) if t]
    return (float(np.mean(hits)) if hits else float("nan")), len(hits)
