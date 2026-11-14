"""Every knob of the synthetic shop in one place.

Two groups:
- `HiddenModel`: the "secret" purchase model (what the organisers' simulator would hide from us).
  Tests and experiments vary these to check that our system learns relations, not constants.
- `SimConfig`: sizes, rates and the clock (catalogue size, requests/s, history volume...).

The defaults are tuned so that, on this simulator, the starter baseline converts ~1-2 purchases
per 100 requests and the oracle ~8-12, i.e. there is a lot of room for a personalised policy.
Nothing here is known about the real simulator: it is our best guess at its *shape*.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class HiddenModel:
    # ---- position bias: probability that the shopper examines slot k (k = 0..9). Sum <= 1 so the
    # click probabilities of a list are a proper "at most one click" distribution.
    examine: tuple[float, ...] = (0.30, 0.17, 0.12, 0.095, 0.078, 0.066, 0.057, 0.050, 0.045, 0.041)

    # ---- click logit u = click_bias + sum of terms below
    click_bias: float = -5.8
    w_quality: float = 0.8           # latent product appeal (drives best sellers)
    w_taste: float = 1.6             # user taste vector . product style vector (hidden, long-term)
    w_intent_sub: float = 1.6        # product is in the session's (hidden) intent subcategory
    w_intent_cat: float = 0.6        # ... or in its category
    w_view_sub: float = 1.0          # recency-weighted share of recent views in the same subcategory
    w_view_brand: float = 0.7        # ... with the same brand
    w_viewed: float = 0.9            # the product itself was just viewed (H1: showing it again helps)
    w_context: float = 1.0           # region x category and device x category tastes
    w_new: float = 0.4               # novelty boost of a freshly created product, decays with tau_new_s
    tau_new_s: float = 3 * 3600.0
    w_price_click: float = 0.6       # relative price inside its subcategory (log ratio to median)
    w_price_drop: float = 1.2        # log(original / current price), only when cheaper than at launch
    w_repeat: float = -3.0           # shopper already bought this product (README: "tends not to want it again")
    page_intent_mult: tuple[tuple[str, float], ...] = (("product", 1.0), ("category", 0.8), ("home", 0.45))

    # ---- purchase-given-click logit v = buy_bias + ...
    buy_bias: float = -0.9
    w_buy_quality: float = 0.4
    price_ref_eur: float = 30.0      # v -= alpha_user * log(price / price_ref)
    alpha_base: float = 0.45         # price sensitivity, shifted by device and region below
    alpha_device: tuple[tuple[str, float], ...] = (("web", 0.0), ("ios", -0.25), ("android", 0.25))
    alpha_region_sd: float = 0.20    # each region gets a fixed random shift ~ N(0, sd)
    alpha_user_sd: float = 0.15
    w_buy_price_drop: float = 0.8

    # ---- trends: temporary log-lift on a subcategory or a brand (seen first in organic events)
    trend_rate_per_hour: float = 3.0
    trend_duration_s: tuple[float, float] = (20 * 60.0, 70 * 60.0)
    trend_log_lift: tuple[float, float] = (0.7, 1.6)   # x2 .. x5 on the click odds
    trend_brand_share: float = 0.3

    # ---- organic browsing (shop.events): which products a session views, and how often it buys
    view_intent_sub: float = 2.5     # extra log-weight of the intent subcategory when picking what to view
    view_intent_cat: float = 1.0
    organic_cart_mult: float = 1.3   # P(add_to_cart | view) = min(0.9, mult * c)
    organic_buy_mult: float = 0.8    # P(purchase | cart)   = min(0.9, mult * b)


@dataclass(frozen=True)
class SimConfig:
    seed: int = 7
    # ---- clock: live stream starts Saturday 10:00 CET = 09:00 UTC; history is the 60 days before
    stream_start: float = 1794646800.0           # 2026-11-14T09:00:00Z
    history_days: int = 60
    # ---- world sizes
    n_products: int = 5000
    n_categories: int = 10
    subcats_per_category: int = 9
    brands_per_category: int = 14
    style_dim: int = 8
    n_regions: int = 17
    n_history_users: int = 40_000
    inactive_share: float = 0.03
    created_in_history_share: float = 0.15
    # ---- live rates
    requests_per_s: float = 3.0
    organic_sessions_per_s: float = 8.0           # with request-session views gives ~50 events/s
    new_user_share: float = 0.35                  # share of live requests from shoppers not in history
    returning_live_share: float = 0.15            # share from shoppers first seen earlier in the stream
    no_view_share: float = 0.25                   # requests with empty recent_views
    new_products_per_hour: float = 150.0
    price_changes_per_hour: float = 600.0
    out_of_stock_per_hour: float = 400.0
    back_in_stock_mean_s: float = 20 * 60.0
    # ---- history volume (the real dump is ~33k impressions and ~200k events per day; we default lower
    # so a full 60-day synthetic history builds in well under a minute)
    history_requests_per_day: int = 2500
    history_organic_sessions_per_day: int = 3500
    # ---- old carousel ("best sellers + randomness"): Plackett-Luce over w = rank^-power + floor
    logging_rank_power: float = 1.1
    logging_floor: float = 0.0004
    # ---- organisers' timing
    deadline_s: float = 3.0
    feedback_delay_s: tuple[float, float] = (1.0, 4.0)
    block_s: float = 5.0                          # live stream is generated in fixed blocks (chunking-invariant)
    hidden: HiddenModel = field(default_factory=HiddenModel)

    def with_(self, **kw) -> "SimConfig":
        return replace(self, **kw)

    @classmethod
    def alt(cls, **kw) -> "SimConfig":
        """A deliberately DIFFERENT shop, to check policies generalise instead of fitting our own guess:
        steeper position curve, much stronger price sensitivity, weaker session affinity (taste and
        context matter more), faster and stronger trends, milder repeat penalty, stronger novelty."""
        hidden = HiddenModel(
            examine=(0.42, 0.20, 0.11, 0.07, 0.05, 0.035, 0.025, 0.018, 0.013, 0.01),
            click_bias=-6.1, w_quality=0.6, w_taste=2.3, w_intent_sub=1.0, w_intent_cat=0.9,
            w_view_sub=0.5, w_view_brand=0.3, w_viewed=0.25, w_context=1.5, w_new=0.9, tau_new_s=1800.0,
            w_price_click=1.1, w_price_drop=2.0, w_repeat=-1.5,
            page_intent_mult=(("product", 1.0), ("category", 0.9), ("home", 0.7)),
            buy_bias=-0.3, alpha_base=0.85, alpha_device=(("web", 0.0), ("ios", -0.45), ("android", 0.45)),
            alpha_region_sd=0.35, alpha_user_sd=0.3, w_buy_price_drop=1.2,
            trend_rate_per_hour=6.0, trend_duration_s=(8 * 60.0, 30 * 60.0), trend_log_lift=(1.0, 2.2),
            trend_brand_share=0.5)
        base = dict(seed=1007, hidden=hidden, new_user_share=0.5, no_view_share=0.35)
        base.update(kw)
        return cls(**base)

    @classmethod
    def small(cls, **kw) -> "SimConfig":
        """Tiny world for unit tests: builds in about a second."""
        base = dict(n_products=900, brands_per_category=6, n_history_users=3000, history_days=4,
                    history_requests_per_day=800, history_organic_sessions_per_day=1000)
        base.update(kw)
        return cls(**base)
