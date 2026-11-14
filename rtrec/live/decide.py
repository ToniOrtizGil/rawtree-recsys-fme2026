"""Pre-registered decision rule for the live A/B (agreed 14-nov, applied once at a fixed time).

Primary:   Beta-Binomial. Purchases per request in each arm ~ Beta(1 + buys, 1 + n - buys);
           P(B > A) by Monte Carlo.
Check:     bootstrap by shopper (user_id): the same shopper makes several requests, so requests are not
           independent; resampling shoppers gives honest uncertainty. P(B - A > 0) over resamples.
Secondary: clicks (≈3x more frequent than purchases, correlated with buying) - reported, used only as a
           tie-break signal in the report, never to overrule purchases.
Rule:      start from the simplest arm (baseline < v1 < v2). A more complex arm replaces the current
           choice only if P_beta >= 0.95 AND P_boot >= 0.90 that it converts better. Otherwise keep the simpler one.
Rows answered by OUR fallback (model not live) are excluded before calling this.
"""
from __future__ import annotations

import numpy as np

ORDER = ["baseline", "v1-session-heuristic", "v2-lgbm"]   # simplest first
P_BETA, P_BOOT = 0.95, 0.90


def p_beta(k_a: int, n_a: int, k_b: int, n_b: int, draws: int = 200_000, seed: int = 0) -> float:
    """P(rate_b > rate_a) under independent Beta(1+k, 1+n-k) posteriors."""
    rng = np.random.default_rng(seed)
    a = rng.beta(1 + k_a, 1 + n_a - k_a, draws)
    b = rng.beta(1 + k_b, 1 + n_b - k_b, draws)
    return float((b > a).mean())


def p_boot_users(rows_a: list[tuple[str, int]], rows_b: list[tuple[str, int]], reps: int = 2000,
                 seed: int = 0) -> float:
    """rows = [(user_id, bought 0/1)] per request. Resample shoppers with replacement within each arm."""
    rng = np.random.default_rng(seed)

    def per_user(rows):
        d: dict[str, list[int]] = {}
        for u, y in rows:
            d.setdefault(u, [0, 0])
            d[u][0] += y
            d[u][1] += 1
        arr = np.array(list(d.values()), float)
        return arr[:, 0], arr[:, 1]

    ka, na = per_user(rows_a)
    kb, nb = per_user(rows_b)
    if len(na) == 0 or len(nb) == 0:
        return 0.5
    ia = rng.integers(0, len(na), (reps, len(na)))
    ib = rng.integers(0, len(nb), (reps, len(nb)))
    ra = ka[ia].sum(1) / na[ia].sum(1)
    rb = kb[ib].sum(1) / nb[ib].sum(1)
    return float((rb > ra).mean())


def decide(arms: dict[str, list[tuple[str, int]]], log=print) -> str:
    """arms = {arm_name: [(user_id, bought), ...]}. Returns the arm name chosen by the rule."""
    present = [a for a in ORDER if arms.get(a)]
    if not present:
        return "v2-lgbm"
    choice = present[0]
    for cand in present[1:]:
        A, B = arms[choice], arms[cand]
        pb = p_beta(sum(y for _, y in A), len(A), sum(y for _, y in B), len(B))
        pu = p_boot_users(A, B)
        ok = pb >= P_BETA and pu >= P_BOOT
        log(f"{cand} vs {choice}: P_beta={pb:.3f} P_boot_users={pu:.3f} -> {'switch' if ok else 'keep simpler'}")
        if ok:
            choice = cand
    return choice
