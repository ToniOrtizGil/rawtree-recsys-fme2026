"""Append the live practice stream to the history, in the history's own shape, for the final refit.

    python scripts/download_history.py --out data --live      # snapshot shop.* live tables + our feedback
    python scripts/live_to_history.py --data data --out data_final
    python scripts/train_ranker.py --parquet data_final --final --out artifacts/ranker_final.txt

Why: the practice stream is the closest thing we have to the final round (real trends, new products,
price drops, the same hidden shopper model) and our own answers with their clicks/purchases are
labelled impressions. Turning them into history_sessions / history_events rows lets the exact same
pipeline (artefacts -> rows -> ranker) learn from them, with no special code path.

- requests + team.feedback (joined on request_id) -> history_sessions rows (shown, clicked, purchased)
- shop.events -> history_events rows (same columns)
- catalog = history catalog + products added during the event (catalog_updates 'new_product')
The live snapshot stores some columns as their text repr; they are parsed back here.
"""
from __future__ import annotations

import argparse
import ast
import os
import re
from pathlib import Path

import pandas as pd

PID = re.compile(r"'(p_[0-9a-zA-Z_]+)'")
VIEW = re.compile(r"'product_id':\s*'([^']+)',\s*'ts':\s*'([^']+)'")


def _ids(x) -> list[str]:
    if x is None:
        return []
    if isinstance(x, str):
        return PID.findall(x)
    return [str(v) for v in list(x)]


def _views(x) -> list[dict]:
    if x is None:
        return []
    if isinstance(x, str):
        return [{"product_id": p, "ts": t} for p, t in VIEW.findall(x)]
    return [{"product_id": str(v["product_id"]), "ts": str(v["ts"])} for v in list(x)]


def _ctx(x) -> dict:
    if isinstance(x, dict):
        return x
    try:
        return ast.literal_eval(x) if isinstance(x, str) and x.startswith("{") else {}
    except (ValueError, SyntaxError):
        return {}


def _opt(x) -> str | None:
    return None if x is None or (isinstance(x, float) and x != x) or str(x) in ("", "None", "nan") else str(x)


def convert(data: Path, out: Path, holdout_hours: float = 0.0) -> dict:
    live = data / "live"
    read = lambda t: pd.concat([pd.read_parquet(f) for f in sorted((live / t).glob("*.parquet"))],  # noqa: E731
                               ignore_index=True)
    req, fb, ev, upd = read("requests"), read("feedback"), read("events"), read("catalog_updates")
    fb = fb.drop_duplicates("request_id", keep="last")
    j = req.merge(fb[["request_id", "shown", "clicked", "purchased", "revenue_eur"]], on="request_id")
    sessions = pd.DataFrame({
        "context": j["context"].map(_ctx),
        "propensity": [[1.0] * len(_ids(s)) for s in j["shown"]],  # not a logged policy's propensity: train only
        "recent_views": j["recent_views"].map(_views),
        "request_id": j["request_id"].astype(str),
        "revenue_eur": pd.to_numeric(j["revenue_eur"], errors="coerce").fillna(0.0),
        "shown": j["shown"].map(_ids),
        "ts": j["ts"].astype(str),
        "user_id": j["user_id"].astype(str),
        "clicked": j["clicked"].map(_opt),
        "purchased": j["purchased"].map(_opt),
    }).sort_values("ts")
    sessions = sessions[sessions["shown"].map(len) > 0]
    events = ev.assign(price_eur=pd.to_numeric(ev["price_eur"], errors="coerce")).sort_values("ts")

    cat = pd.read_parquet(data / "catalog.parquet")
    new = upd[upd["change"].astype(str) == "new_product"].drop_duplicates("product_id", keep="last")
    new = new[~new["product_id"].astype(str).isin(set(cat["product_id"].astype(str)))]
    if len(new):
        add = new[[c for c in cat.columns if c in new.columns]].copy()
        for c in cat.columns:
            if c in add.columns and pd.api.types.is_numeric_dtype(cat[c]):
                add[c] = pd.to_numeric(add[c], errors="coerce")
            elif c in add.columns and pd.api.types.is_bool_dtype(cat[c]):
                add[c] = add[c].astype(str) == "True"
        cat = pd.concat([cat, add], ignore_index=True)

    out.mkdir(parents=True, exist_ok=True)
    held = {}
    if holdout_hours > 0:
        # the last hours of the stream are set aside, never used to train or to tune: the refit has to
        # beat the current model there (scripts/compare_models.py) to be allowed into the final
        cut = (pd.Timestamp(sessions["ts"].max()) - pd.Timedelta(hours=holdout_hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        hd = out / "holdout"
        hd.mkdir(exist_ok=True)
        sessions[sessions["ts"] >= cut].to_parquet(hd / "sessions.parquet", index=False)
        events[events["ts"] >= cut].to_parquet(hd / "events.parquet", index=False)
        held = {"holdout_from": cut, "holdout_sessions": int((sessions["ts"] >= cut).sum())}
        sessions, events = sessions[sessions["ts"] < cut], events[events["ts"] < cut]
    cat.to_parquet(out / "catalog.parquet")
    for t, df in (("history_sessions", sessions), ("history_events", events)):
        d = out / t
        d.mkdir(exist_ok=True)
        for f in sorted((data / t).glob("*.parquet")):   # the real history, linked not copied
            link = d / f.name
            if not link.exists():
                os.symlink(f.resolve(), link)
        df.to_parquet(d / "zz-live.parquet", index=False)
    return {**held, "live_sessions": len(sessions), "purchases": int(sessions["purchased"].notna().sum()),
            "clicks": int(sessions["clicked"].notna().sum()), "live_events": len(events), "new_products": len(new)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="data_final")
    ap.add_argument("--holdout-hours", type=float, default=0.0)
    a = ap.parse_args()
    print(convert(Path(a.data), Path(a.out), a.holdout_hours))


if __name__ == "__main__":
    main()
