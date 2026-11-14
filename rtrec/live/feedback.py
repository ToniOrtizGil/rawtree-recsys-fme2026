"""Learning from our own feedback (team_NN.feedback): position curve, calibration, reliability, A/B.

Calibration, in one sentence: for every product (and subcategory) we compare purchases observed in
our carousel with purchases *expected* for an average product at the same slots; the ratio is a
multiplicative correction with a Gamma prior (Gamma-Poisson, the count version of Beta-Bernoulli):

    m_sub = (B_sub + a_sub) / (E_sub + a_sub)                     shrunk towards 1
    m_p   = (B_p + a_p * m_sub) / (E_p + a_p)                     shrunk towards its subcategory

B = observed purchases, E = sum over impressions of the slot's average purchase rate. E is computed
at publish time from per-slot impression counts and the *current* position curve, so a burst of
feedback processed before the curve is learned is not biased. The posterior Gamma(shape=B+a*m,
rate=E+a) is what Thompson sampling draws from (rtrec.live.bandit). Counters decay (default 6 h)
so the correction follows the stream. Fallback-served answers still count: shoppers saw them.
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field

from rtrec.contract import State

from ._util import DecayedCounter, RecentKeys, to_epoch

N_SLOTS = 10


@dataclass
class FeedbackLearner:
    half_life_s: float = 6 * 3600
    a_sub: float = 5.0
    a_prod: float = 2.0
    publish_every: int = 50          # feedback rows between snapshots
    window: int = 2000               # reliability window (last N answers)
    seen: RecentKeys = field(default_factory=lambda: RecentKeys(300_000))
    shown_k: list = field(default_factory=lambda: [0.0] * N_SLOTS)
    click_k: list = field(default_factory=lambda: [0.0] * N_SLOTS)
    buy_k: list = field(default_factory=lambda: [0.0] * N_SLOTS)
    N_p: list = field(init=False)                     # per slot: decayed impressions per product
    N_s: list = field(init=False)                     # per slot: decayed impressions per subcategory
    B_p: DecayedCounter = field(init=False)
    B_s: DecayedCounter = field(init=False)
    imps: Counter = field(default_factory=Counter)
    recent: deque = field(init=False)
    now: float = 0.0
    _since_pub: int = 0
    _cvr: list = field(default_factory=list)

    def __post_init__(self):
        self.B_p = DecayedCounter(self.half_life_s)
        self.B_s = DecayedCounter(self.half_life_s)
        self.N_p = [DecayedCounter(self.half_life_s) for _ in range(N_SLOTS)]
        self.N_s = [DecayedCounter(self.half_life_s) for _ in range(N_SLOTS)]
        self.recent = deque(maxlen=self.window)

    def slot_cvr(self) -> list[float]:
        """Purchase rate by slot, smoothed towards a 1/(k+1) prior until data accumulates."""
        tot_s, tot_b = sum(self.shown_k), sum(self.buy_k)
        base = (tot_b + 0.5) / (tot_s + 50.0)
        prior = [1.0 / (k + 1) for k in range(N_SLOTS)]
        z = sum(prior) / N_SLOTS
        out = []
        for k in range(N_SLOTS):
            pk = base * prior[k] / z
            out.append((self.buy_k[k] + 20.0 * pk) / (self.shown_k[k] + 20.0))
        return out

    def apply(self, rows, state: State, learner=None) -> int:
        n = 0
        cat = state.catalog
        ab = state.live.get("ab")
        for fb in rows:
            rid = fb.get("request_id")
            if not rid or rid == "__init__" or not self.seen.add(rid):
                continue
            n += 1
            t = to_epoch(fb.get("ts") or self.now)
            self.now = max(self.now, t)
            shown = list(fb.get("shown") or [])[:N_SLOTS]
            clicked = fb.get("clicked") or ""
            bought = fb.get("purchased") or ""
            for k, pid in enumerate(shown):
                self.shown_k[k] += 1
                if pid == clicked:
                    self.click_k[k] += 1
                if pid == bought:
                    self.buy_k[k] += 1
                sub = (cat.get(pid) or {}).get("subcategory")
                self.N_p[k].add(pid, 1.0, t)
                self.imps[pid] += 1
                if sub is not None:
                    self.N_s[k].add(sub, 1.0, t)
                if pid == bought:
                    self.B_p.add(pid, 1.0, t)
                    if sub is not None:
                        self.B_s.add(sub, 1.0, t)
            self.recent.append((fb.get("served_by", "team"), fb.get("fallback_reason", ""),
                                float(fb.get("latency_ms") or 0.0)))
            info = learner.requests.get(rid) if learner is not None else None
            if info is not None and info.user_id and learner is not None:
                learner.profiles.add_carousel(info.user_id, t, clicked, bought, state)
            if ab is not None:
                ab.update(rid, fb, info)
        self._since_pub += n
        if n and self._since_pub >= self.publish_every:
            self.publish(state)
        return n

    # ------------------------------------------------------------------ reads
    def expected(self, key, sub: bool = False, now=None) -> float:
        """E: purchases an average product would have had with the same slot exposure."""
        now = self.now if now is None else now
        cvr = self._cvr or self.slot_cvr()
        counters = self.N_s if sub else self.N_p
        return sum(c * n.value(key, now) for c, n in zip(cvr, counters))

    def multiplier_subcat(self, sub, now=None) -> float:
        now = self.now if now is None else now
        return (self.B_s.value(sub, now) + self.a_sub) / (self.expected(sub, True, now) + self.a_sub)

    def posterior(self, pid: str, sub=None, now=None) -> tuple[float, float]:
        """Gamma(shape, rate) posterior of the product's multiplier."""
        now = self.now if now is None else now
        m_sub = self.multiplier_subcat(sub, now) if sub is not None else 1.0
        return (self.B_p.value(pid, now) + self.a_prod * m_sub, self.expected(pid, False, now) + self.a_prod)

    def publish(self, state: State) -> None:
        self._since_pub = 0
        now = self.now
        cat = state.catalog
        self._cvr = self.slot_cvr()
        subs = set().union(*(n.d for n in self.N_s))
        sub_m = {s: self.multiplier_subcat(s, now) for s in subs}
        prod_m, post = {}, {}
        for pid in self.imps:
            sub = (cat.get(pid) or {}).get("subcategory")
            shape, rate = (self.B_p.value(pid, now) + self.a_prod * sub_m.get(sub, 1.0),
                           self.expected(pid, False, now) + self.a_prod)
            prod_m[pid] = shape / rate
            post[pid] = (shape, rate)
        live = state.live
        live["calib_subcat"] = sub_m
        live["calib_product"] = prod_m
        live["calib_posterior"] = post
        live["impressions"] = dict(self.imps)
        live["slot_cvr"] = self.slot_cvr()
        live["slot_ctr"] = [(c + 1.0) / (s + 20.0) for c, s in zip(self.click_k, self.shown_k)]
        live["reliability"] = self._reliability()
        ab = live.get("ab")
        if ab is not None:
            live["ab_report"] = ab.report()

    def _reliability(self) -> dict:
        r = list(self.recent)
        if not r:
            return {"n": 0}
        lat = sorted(x[2] for x in r)
        q = lambda f: lat[min(int(f * len(lat)), len(lat) - 1)]  # noqa: E731
        reasons = Counter(x[1] for x in r if x[0] != "team")
        return {"n": len(r), "team_rate": sum(x[0] == "team" for x in r) / len(r),
                "p50_ms": q(0.5), "p95_ms": q(0.95), "p99_ms": q(0.99),
                "fallback_reasons": dict(reasons)}
