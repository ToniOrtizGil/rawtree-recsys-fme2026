"""The static-ish world of the synthetic shop: products, shoppers, regions and trends.

Everything is stored as numpy arrays indexed by an integer (product index / user index) so that the
hidden model in sim/model.py can score thousands of (request, product) pairs in one vector op.
Products and users can be appended during the live stream (new products, new shoppers).

Hidden structure that a good recommender has to *discover* from the data (never exposed directly):
- every product has a latent appeal `q` and a style vector `z` (subcategory centroid + noise);
- every shopper has a taste vector `theta` close to the styles of 1-3 favourite categories;
- descriptions are written from the style vector, so text carries signal for brand-new products;
- regions and devices have their own category tastes and price sensitivities.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .params import SimConfig

CATEGORIES: dict[str, list[str]] = {
    "Electronics": ["Headphones", "Smartphones", "Laptops", "Tablets", "Smartwatches", "Cameras",
                    "Speakers", "Chargers", "Monitors"],
    "Home & Kitchen": ["Cookware", "Small Appliances", "Coffee Makers", "Bedding", "Lighting", "Storage",
                       "Tableware", "Cleaning", "Decor"],
    "Fashion": ["T-Shirts", "Jeans", "Dresses", "Jackets", "Sneakers", "Boots", "Bags", "Watches",
                "Sunglasses"],
    "Beauty": ["Skincare", "Makeup", "Fragrances", "Hair Care", "Nail Care", "Shaving", "Sun Care",
               "Bath & Body", "Beauty Tools"],
    "Sports": ["Running", "Cycling", "Fitness Equipment", "Yoga", "Camping", "Swimming", "Football",
               "Tennis", "Sports Nutrition"],
    "Toys": ["Building Sets", "Dolls", "Board Games", "Puzzles", "Remote Control", "Plush", "Educational",
             "Outdoor Toys", "Baby Toys"],
    "Books": ["Fiction", "Thrillers", "Science Fiction", "Cookbooks", "Children's Books", "Comics",
              "Self-Help", "History", "Travel Guides"],
    "Grocery": ["Coffee", "Tea", "Snacks", "Olive Oil", "Wine", "Chocolate", "Breakfast", "Spices",
                "Organic"],
    "Garden": ["Plants", "Garden Tools", "Outdoor Furniture", "Barbecue", "Irrigation", "Seeds",
               "Pots & Planters", "Lawn Care", "Outdoor Lighting"],
    "Pets": ["Dog Food", "Cat Food", "Pet Toys", "Pet Beds", "Aquarium", "Bird Supplies", "Grooming",
             "Leashes & Collars", "Pet Health"],
}

# Population-weighted Spanish regions (the real `region` values look like "Catalunya").
REGIONS: list[tuple[str, float]] = [
    ("Andalucía", 8.5), ("Catalunya", 7.8), ("Madrid", 6.8), ("Comunidad Valenciana", 5.1),
    ("Galicia", 2.7), ("Castilla y León", 2.4), ("País Vasco", 2.2), ("Canarias", 2.2),
    ("Castilla-La Mancha", 2.1), ("Murcia", 1.5), ("Aragón", 1.3), ("Baleares", 1.2),
    ("Extremadura", 1.1), ("Asturias", 1.0), ("Navarra", 0.66), ("Cantabria", 0.58), ("La Rioja", 0.32),
]
DEVICES = ("web", "ios", "android")
DEVICE_SHARE = (0.45, 0.30, 0.25)
PAGES = ("home", "product", "category")

STYLE_WORDS = ["classic", "modern", "minimalist", "rustic", "sporty", "elegant", "compact", "rugged",
               "colourful", "eco-friendly", "vintage", "smart", "cosy", "bold", "lightweight", "luxury",
               "handmade", "urban", "playful", "professional", "natural", "retro", "sleek", "family-size",
               "travel", "artisan", "nordic", "tropical", "wireless", "organic", "gourmet", "durable"]
TIER_PHRASES = {
    0: ["Great value for everyday use.", "Budget-friendly and reliable.", "Simple, affordable choice."],
    1: ["A solid all-rounder.", "Popular pick with balanced features.", "Well made and easy to use."],
    2: ["Premium materials and finish.", "Top of the range quality.", "Designed for demanding users."],
}
_SYL = ["ka", "lo", "mi", "ra", "ven", "tor", "zu", "nel", "bri", "co", "dan", "ex", "fi", "gor", "hal",
        "is", "jun", "kor", "lum", "mar", "nov", "or", "pex", "qui", "ros", "sol", "tan", "ul", "vi", "wex"]


def _unit(x: np.ndarray) -> np.ndarray:
    return x / np.maximum(np.linalg.norm(x, axis=-1, keepdims=True), 1e-9)


def iso(t: float | np.ndarray) -> str | np.ndarray:
    """Epoch seconds -> RawTree-style ISO-8601 UTC string with milliseconds ('2026-11-14T16:02:11.482Z')."""
    arr = np.asarray(np.round(np.asarray(t, dtype=np.float64) * 1000.0), dtype="int64").astype("datetime64[ms]")
    out = np.char.add(np.datetime_as_string(arr, unit="ms"), "Z")
    return str(out) if np.ndim(out) == 0 else out


class _Growable:
    """Column store whose rows can be appended (amortised doubling)."""

    def __init__(self, cols: dict[str, np.ndarray]):
        self.n = len(next(iter(cols.values())))
        self._cols = cols
        for k, v in cols.items():
            setattr(self, k, v)

    def append(self, **row) -> int:
        i = self.n
        if i >= len(self._cols[next(iter(self._cols))]):
            for k, v in self._cols.items():
                new = np.zeros((max(16, 2 * len(v)),) + v.shape[1:], dtype=v.dtype)
                new[: len(v)] = v
                self._cols[k] = new
                setattr(self, k, new)
        for k, v in row.items():
            self._cols[k][i] = v
        self.n += 1
        return i


@dataclass
class Trends:
    """Temporary log-lifts on a subcategory (kind 0) or a brand (kind 1)."""
    start: np.ndarray
    end: np.ndarray
    kind: np.ndarray
    target: np.ndarray
    log_lift: np.ndarray

    def amplitude(self, j: int, t: np.ndarray) -> np.ndarray:
        """Trapezoid: ramps up over the first quarter, plateau, ramps down over the last quarter."""
        s, e = self.start[j], self.end[j]
        ramp = 0.25 * (e - s)
        a = np.clip(np.minimum((t - s) / ramp, (e - t) / ramp), 0.0, 1.0)
        return a * self.log_lift[j]

    def active_between(self, t0: float, t1: float) -> np.ndarray:
        return np.nonzero((self.start < t1) & (self.end > t0))[0]


class World:
    def __init__(self, cfg: SimConfig, rng: np.random.Generator):
        self.cfg = cfg
        self.rng = rng
        D = cfg.style_dim
        self.cat_names = list(CATEGORIES)[: cfg.n_categories]
        self.sub_names: list[str] = []
        self.sub_cat: list[int] = []
        for c, name in enumerate(self.cat_names):
            for s in CATEGORIES[name][: cfg.subcats_per_category]:
                self.sub_names.append(s)
                self.sub_cat.append(c)
        self.sub_cat = np.array(self.sub_cat)
        n_sub = len(self.sub_names)
        self.cat_z = _unit(rng.normal(size=(cfg.n_categories, D)))
        self.sub_z = _unit(self.cat_z[self.sub_cat] + 0.7 * rng.normal(size=(n_sub, D)) / np.sqrt(D))
        self.sub_logprice = (np.log(rng.choice([8, 15, 25, 40, 70, 120], size=cfg.n_categories))[self.sub_cat]
                             + rng.normal(0, 0.45, size=n_sub))
        # brands belong to a category; each has an appeal offset and a price tier
        nb = cfg.brands_per_category
        self.brand_cat = np.repeat(np.arange(cfg.n_categories), nb)
        self.brand_q = rng.normal(0, 0.45, size=len(self.brand_cat))
        self.brand_tier = rng.choice([0, 1, 2], p=[0.35, 0.45, 0.2], size=len(self.brand_cat))
        self.brand_names = self._brand_names(len(self.brand_cat))
        self.word_vec = _unit(rng.normal(size=(len(STYLE_WORDS), D)))
        # regions / devices
        self.region_names = [r for r, _ in REGIONS[: cfg.n_regions]]
        w = np.array([p for _, p in REGIONS[: cfg.n_regions]])
        self.region_p = w / w.sum()
        self.region_cat = rng.normal(0, 0.35, size=(cfg.n_regions, cfg.n_categories))
        self.device_cat = rng.normal(0, 0.25, size=(3, cfg.n_categories))
        self.alpha_region = rng.normal(0, cfg.hidden.alpha_region_sd, size=cfg.n_regions)
        self._ids: set[str] = set()
        self._build_products()
        self._build_users()
        self.sub_median_price = np.array([np.median(self.products.price0[: self.products.n][
            self.products.sub[: self.products.n] == s]) if np.any(self.products.sub[: self.products.n] == s)
            else np.exp(self.sub_logprice[s]) for s in range(n_sub)])
        self.trends = self._build_trends()

    # ------------------------------------------------------------------ helpers
    def _brand_names(self, n: int) -> list[str]:
        out, seen = [], set()
        while len(out) < n:
            k = self.rng.integers(2, 4)
            name = "".join(self.rng.choice(_SYL, size=k)).capitalize()
            if name not in seen:
                seen.add(name)
                out.append(name)
        return out

    def new_id(self, prefix: str, rng: np.random.Generator, nhex: int = 6) -> str:
        while True:
            s = f"{prefix}_{rng.integers(0, 16 ** nhex):0{nhex}x}"
            if s not in self._ids:
                self._ids.add(s)
                return s

    def _description(self, z: np.ndarray, sub: int, brand: int, tier: int, rng: np.random.Generator) -> str:
        score = self.word_vec @ z + rng.normal(0, 0.15, size=len(STYLE_WORDS))
        w1, w2 = (STYLE_WORDS[i] for i in np.argsort(-score)[:2])
        noun = self.sub_names[sub].lower()
        return (f"{w1.capitalize()} {w2} {noun} by {self.brand_names[brand]}. "
                f"{TIER_PHRASES[tier][rng.integers(0, 3)]}")

    def sample_product(self, rng: np.random.Generator, created: float, cat: int | None = None) -> dict:
        """Attributes of one product (used for the initial catalogue and for live `new_product`)."""
        cfg = self.cfg
        if cat is None:
            cat = int(rng.integers(0, cfg.n_categories))
        sub = int(cat * cfg.subcats_per_category + rng.integers(0, cfg.subcats_per_category))
        brand = int(cat * cfg.brands_per_category + rng.integers(0, cfg.brands_per_category))
        z = _unit(self.sub_z[sub] + 0.45 * rng.normal(size=cfg.style_dim) / np.sqrt(cfg.style_dim))
        q = self.brand_q[brand] + rng.normal(0, 0.6)
        tier = int(self.brand_tier[brand])
        lp = self.sub_logprice[sub] + 0.35 * (tier - 1) + 0.15 * q + rng.normal(0, 0.3)
        price = max(1.99, round(float(np.exp(lp))) - 0.01)
        return dict(pid=self.new_id("p", rng), cat=cat, sub=sub, brand=brand, q=q, z=z, price0=price,
                    price=price, created=created, active=False,
                    desc=self._description(z, sub, brand, tier, rng))

    # ------------------------------------------------------------------ builders
    def _build_products(self) -> None:
        cfg, rng = self.cfg, self.rng
        n = cfg.n_products
        h0 = cfg.stream_start - cfg.history_days * 86400
        created = np.where(rng.random(n) < cfg.created_in_history_share,
                           rng.uniform(h0, cfg.stream_start - 3600, n),
                           rng.uniform(h0 - 400 * 86400, h0, n))
        rows = [self.sample_product(rng, float(created[i])) for i in range(n)]
        self.products = _Growable({
            "cat": np.array([r["cat"] for r in rows], dtype=np.int32),
            "sub": np.array([r["sub"] for r in rows], dtype=np.int32),
            "brand": np.array([r["brand"] for r in rows], dtype=np.int32),
            "q": np.array([r["q"] for r in rows]),
            "z": np.stack([r["z"] for r in rows]),
            "price0": np.array([r["price0"] for r in rows]),
            "price": np.array([r["price"] for r in rows]),
            "created": created.astype(np.float64),
            "active": rng.random(n) >= cfg.inactive_share,
        })
        self.hist_active = self.products.active[:n].copy()  # catalogue flag during the history window
        self.pid = [r["pid"] for r in rows]
        self.desc = [r["desc"] for r in rows]
        self.pid_index = {p: i for i, p in enumerate(self.pid)}

    def add_product(self, row: dict) -> int:
        i = self.products.append(**{k: row[k] for k in ("cat", "sub", "brand", "q", "z", "price0", "price",
                                                         "created", "active")})
        self.pid.append(row["pid"])
        self.desc.append(row["desc"])
        self.pid_index[row["pid"]] = i
        return i

    def sample_user(self, rng: np.random.Generator) -> dict:
        cfg = self.cfg
        k = rng.choice([1, 2, 3], p=[0.5, 0.35, 0.15])
        cats = rng.choice(cfg.n_categories, size=k, replace=False)
        theta = _unit(self.cat_z[cats].sum(0) + 0.5 * rng.normal(size=cfg.style_dim) / np.sqrt(cfg.style_dim))
        return dict(uid=self.new_id("u", rng), theta=theta,
                    region=int(rng.choice(cfg.n_regions, p=self.region_p)),
                    device=int(rng.choice(3, p=DEVICE_SHARE)),
                    alpha_noise=rng.normal(0, cfg.hidden.alpha_user_sd),
                    activity=float(rng.lognormal(0, 1.0)))

    def _build_users(self) -> None:
        rows = [self.sample_user(self.rng) for _ in range(self.cfg.n_history_users)]
        self.users = _Growable({
            "theta": np.stack([r["theta"] for r in rows]),
            "region": np.array([r["region"] for r in rows], dtype=np.int32),
            "device": np.array([r["device"] for r in rows], dtype=np.int32),
            "alpha_noise": np.array([r["alpha_noise"] for r in rows]),
            "activity": np.array([r["activity"] for r in rows]),
        })
        self.uid = [r["uid"] for r in rows]
        self.n_history_users = len(rows)
        self.uid_index = {u: i for i, u in enumerate(self.uid)}

    def add_user(self, row: dict) -> int:
        i = self.users.append(**{k: row[k] for k in ("theta", "region", "device", "alpha_noise", "activity")})
        self.uid.append(row["uid"])
        self.uid_index[row["uid"]] = i
        return i

    def _build_trends(self) -> Trends:
        cfg, h = self.cfg, self.cfg.hidden
        rng = np.random.default_rng([cfg.seed, 99])
        t0 = cfg.stream_start - cfg.history_days * 86400
        t1 = cfg.stream_start + 14 * 86400
        n = rng.poisson(h.trend_rate_per_hour * (t1 - t0) / 3600)
        start = np.sort(rng.uniform(t0, t1, n))
        dur = rng.uniform(*h.trend_duration_s, n)
        kind = (rng.random(n) < h.trend_brand_share).astype(np.int32)
        target = np.where(kind == 1, rng.integers(0, len(self.brand_cat), n), rng.integers(0, len(self.sub_names), n))
        return Trends(start, start + dur, kind, target.astype(np.int32), rng.uniform(*h.trend_log_lift, n))

    # ------------------------------------------------------------------ queries
    def available(self, t: float) -> np.ndarray:
        """Boolean mask over products that could be shown / bought at time t."""
        n = self.products.n
        if t < self.cfg.stream_start:
            m = np.zeros(n, bool)
            m[: len(self.hist_active)] = self.hist_active
            return m & (self.products.created[:n] <= t)
        return self.products.active[:n] & (self.products.created[:n] <= t)

    def product_row(self, i: int) -> dict:
        """rtrec.schemas.Product for product index i, current state."""
        P = self.products
        return {"product_id": self.pid[i], "category": self.cat_names[P.cat[i]],
                "subcategory": self.sub_names[P.sub[i]], "brand": self.brand_names[P.brand[i]],
                "price_eur": float(P.price[i]), "description": self.desc[i], "created_at": iso(P.created[i]),
                "is_active": bool(P.active[i])}
