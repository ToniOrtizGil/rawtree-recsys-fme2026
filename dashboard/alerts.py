"""Alert rules: turn a dashboard snapshot into a short list of things a human must look at, each pointing to
the section of ops/RUNBOOK.md that says what to do.

Why stricter than the organisers: their light turns red at <=90 % valid & on time over 5 minutes. By then
we have already lost points. We warn at <98 % and on the first late / invalid / missing answer, so the team
reacts while the light is still green.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

LEVELS = {"ok": 0, "info": 0, "warning": 1, "critical": 2}

T = {  # thresholds (one place, so the runbook and the tests quote the same numbers)
    "light_pct": 90.0,          # organisers' light
    "warn_pct": 98.0,           # our early warning
    "min_answers": 20,          # below this, 5-minute percentages are noise
    "heartbeat_s": 15.0,        # heartbeat expected every <=5 s
    "feedback_warn_s": 60.0,    # feedback lands ~5 s after the deadline
    "feedback_crit_s": 180.0,
    "requests_s": 30.0,         # ~3 requests/s: 30 s of silence is not normal
    "p95_ms": 2000.0,           # budget: p99 < 1.8 s target, 3 s deadline
    "p99_ms": 2500.0,
}

CAUSE = {
    "missing": ("We are not writing answers (process down, stuck writer, or wrong team/key).", "r1-missing"),
    "late": ("Our answers arrive after the 3 s deadline.", "r2-late"),
    "invalid": ("Lists rejected: an inactive / out-of-stock or duplicate product.", "r3-invalid"),
}


@dataclass
class Alert:
    level: str
    code: str
    title: str
    detail: str
    runbook: str        # anchor in ops/RUNBOOK.md


def _fmt(x, unit="") -> str:
    return "n/a" if x is None else f"{x:.0f}{unit}"


def evaluate(s: dict) -> list[Alert]:
    out: list[Alert] = []
    li, ages = s["light"], s["ages"]
    n, pct = li["n"], li["ok_pct"]
    reasons = li.get("reasons", {})
    top = max(reasons, key=lambda k: reasons[k]) if any(reasons.values()) else None

    if n >= T["min_answers"] and pct is not None and pct <= T["light_pct"]:
        cause, anchor = CAUSE.get(top, ("Unknown fallback reason.", "r0-triage"))
        out.append(Alert("critical", "LIGHT_RED", f"Organisers' light is RED: {pct:.1f}% valid & on time (5 min)",
                         f"{n - li['ok']} of {n} answers served by fallback; mostly '{top}'. {cause}", anchor))
    elif n >= T["min_answers"] and pct is not None and pct < T["warn_pct"]:
        cause, anchor = CAUSE.get(top, ("", "r0-triage"))
        out.append(Alert("warning", "RELIABILITY_LOW", f"Only {pct:.1f}% valid & on time (5 min), light still green",
                         f"Fallback reasons: {reasons}. {cause}", anchor))
    else:
        for k in ("invalid", "late", "missing"):
            if reasons.get(k):
                cause, anchor = CAUSE[k]
                out.append(Alert("warning", f"SEEN_{k.upper()}", f"{reasons[k]} '{k}' answer(s) in the last 5 min",
                                 cause, anchor))

    hb_age = ages.get("heartbeat")
    if hb_age is not None and hb_age > T["heartbeat_s"]:
        out.append(Alert("critical", "HEARTBEAT_LOST", f"No heartbeat from the serving process for {hb_age:.0f} s",
                         "The primary may be dead or frozen. Standby should take over; check it does.", "r4-machine"))
    elif hb_age is None and s.get("uptime_s", 0) > 60:
        out.append(Alert("info", "NO_HEARTBEAT_SOURCE", "No heartbeat rows seen in team.scratch",
                         "Either the runtime does not write them yet or this store cannot read scratch.", "r0-triage"))

    req_age, fb_age = ages.get("request"), ages.get("feedback")
    if req_age is None or req_age > T["requests_s"]:
        out.append(Alert("warning", "REQUESTS_STALLED", f"No new shop.requests for {_fmt(req_age, ' s')}",
                         "Stream paused by the organisers, or our reader is broken (check errors below).", "r5-stalled"))
    elif fb_age is None or fb_age > T["feedback_warn_s"]:
        lvl = "critical" if fb_age is None or fb_age > T["feedback_crit_s"] else "warning"
        out.append(Alert(lvl, "FEEDBACK_STALLED", f"Requests flow but no new feedback for {_fmt(fb_age, ' s')}",
                         "We are blind on reliability and conversion. Check team.feedback by SQL.", "r5-stalled"))

    lat = s.get("latency_5m") or {}
    if lat.get("p99") is not None and lat["p99"] > T["p99_ms"]:
        out.append(Alert("warning", "LATENCY_P99", f"p99 latency {lat['p99']:.0f} ms (deadline 3000 ms)",
                         f"p50 {_fmt(lat.get('p50'))} / p95 {_fmt(lat.get('p95'))} ms.", "r2-late"))
    elif lat.get("p95") is not None and lat["p95"] > T["p95_ms"]:
        out.append(Alert("warning", "LATENCY_P95", f"p95 latency {lat['p95']:.0f} ms", "", "r2-late"))

    for stream, err in (s.get("errors") or {}).items():
        out.append(Alert("warning", "READ_ERROR", f"Dashboard cannot read {stream}", err, "r0-triage"))

    out.sort(key=lambda a: -LEVELS[a.level])
    return out


def overall(alerts: list[Alert]) -> str:
    worst = max((LEVELS[a.level] for a in alerts), default=0)
    return {0: "ok", 1: "warning", 2: "critical"}[worst]


def as_dicts(alerts: list[Alert]) -> list[dict]:
    return [asdict(a) for a in alerts]


def one_line(s: dict, alerts: list[Alert]) -> str:
    """For cron checks and chat reports: `CRITICAL light=red ok=84.2% n=900 p95=1200ms | LIGHT_RED, ...`."""
    li, lat = s["light"], s.get("latency_5m") or {}
    pct = "n/a" if li["ok_pct"] is None else f"{li['ok_pct']:.1f}%"
    codes = ", ".join(a.code for a in alerts if a.level != "info") or "-"
    conv = s.get("conversion") or {}
    p100 = "n/a" if conv.get("p100") is None else f"{conv['p100']:.2f}"
    return (f"{overall(alerts).upper()} light={li['status']} ok={pct} n5m={li['n']} "
            f"p95={_fmt(lat.get('p95'), 'ms')} purchases/100={p100} | {codes}")
