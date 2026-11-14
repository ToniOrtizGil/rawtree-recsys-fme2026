"""A3: off-policy estimators recover a known value on synthetic logs with known propensities."""
import numpy as np

from rtrec.offline import estimators as est


def _logs(n=40000, n_items=30, seed=0):
    """Logging policy: uniform random slates (Plackett-Luce with equal weights). Purchase of the item in
    slot k happens independently with prob q[item] * pos[k] (item-position model, no competition)."""
    rng = np.random.default_rng(seed)
    items = [f"p{i}" for i in range(n_items)]
    q = rng.uniform(0.0, 0.05, n_items)
    pos = 0.9 ** np.arange(10)
    shown, props, purchased = [], [], []
    for _ in range(n):
        perm = rng.permutation(n_items)[:10]
        sl = [items[j] for j in perm]
        pr = [1.0 / (n_items - k) for k in range(10)]
        p = q[perm] * pos
        y = None
        for k in range(10):  # at most one purchase: first success
            if rng.random() < p[k]:
                y = sl[k]
                break
        shown.append(sl)
        props.append(pr)
        purchased.append(y)
    return items, q, pos, shown, props, purchased


def test_ips_snips_dr_unbiased_on_item_position_model():
    items, q, pos, shown, props, purchased = _logs()
    order = list(np.argsort(-q))
    new = [[items[j] for j in order[:10]]] * len(shown)
    # truth under the generating process (first-success: P(buy slot k) = p_k * prod_{j<k}(1-p_j))
    p = q[order[:10]] * pos
    truth = 100 * float(sum(p[k] * np.prod(1 - p[:k]) for k in range(10)))
    marg = est.empirical_marginals(shown)
    r = est.estimate(new, shown, props, purchased, marginals=marg)
    # with uniform logging, marginals are 1/n_items, and conditional propensities differ from them
    assert r["matches"] > 1000
    assert abs(r["snips"] - truth) < 3 * r["snips_ci"] + 0.3
    # DR with a perfect-ish reward model: tighter
    qn = np.tile(p, (len(shown), 1))
    idx = {it: i for i, it in enumerate(items)}
    ql = np.array([[q[idx[s]] * pos[k] for k, s in enumerate(sl)] for sl in shown])
    r2 = est.estimate(new, shown, props, purchased, q_new=qn, q_logged=ql, marginals=marg)
    assert abs(r2["dr"] - truth) < 3 * r2["dr_ci"] + 0.3
    assert r2["dr_ci"] < r["ips_ci"]


def test_hit_rate():
    hr, n = est.hit_rate([["a", "b"], ["c"]], ["a", None])
    assert hr == 1.0 and n == 1
