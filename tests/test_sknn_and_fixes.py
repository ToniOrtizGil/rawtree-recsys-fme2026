"""view_to_buy (session-KNN graph), the ndarray recent_views fix and the thread-safe metrics window."""
import threading

import numpy as np
import pandas as pd

from rtrec.model.artefacts import view_to_buy
from rtrec.runtime.metrics import Window


def test_view_to_buy_points_at_what_was_bought():
    ev = pd.DataFrame({
        "session_id": ["s1", "s1", "s1", "s2", "s2", "s3"],
        "product_id": ["a", "b", "c", "a", "c", "a"],
        "event_type": ["view", "view", "purchase", "view", "add_to_cart", "view"],
    })
    g = view_to_buy(ev)
    top_a = dict(g["a"])
    assert set(top_a) == {"c"}                      # a was viewed, c was bought/carted after it
    assert top_a["c"] > dict(g["b"])["c"] * 0.99    # a appears in two sessions that converted
    assert "c" not in g                              # never viewed -> no outgoing edges


def test_session_to_request_accepts_numpy_views():
    from rtrec.contract import State
    from rtrec.offline.trainset import session_to_request
    row = {"request_id": "r", "ts": "2026-11-14T10:00:00Z", "user_id": "u", "context": {},
           "recent_views": np.array([{"product_id": "p1", "ts": "x"}, {"product_id": "p2", "ts": "y"}])}
    assert [v["product_id"] for v in session_to_request(row, State())["recent_views"]] == ["p1", "p2"]
    row["recent_views"] = None
    assert session_to_request(row, State())["recent_views"] == []


def test_window_survives_concurrent_appends():
    w, stop = Window(maxlen=5000), threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            w.add(float(i), 1.0)
            i += 1

    t = threading.Thread(target=writer)
    t.start()
    try:
        for _ in range(300):
            w.pcts()
    finally:
        stop.set()
        t.join()


def test_decide_keeps_simplest_unless_clear_winner():
    from rtrec.live.decide import decide
    rng = np.random.default_rng(0)
    mk = lambda n, p: [(f"u{i % (n // 3)}", int(rng.random() < p)) for i in range(n)]  # noqa: E731
    same = {"baseline": mk(6000, 0.01), "v1-session-heuristic": mk(6000, 0.01), "v2-lgbm": mk(6000, 0.01)}
    assert decide(same, log=lambda *_: None) == "baseline"
    clear = {"baseline": mk(6000, 0.008), "v1-session-heuristic": mk(6000, 0.008), "v2-lgbm": mk(6000, 0.02)}
    assert decide(clear, log=lambda *_: None) == "v2-lgbm"


def test_ipw_weights_are_clipped_and_normalised():
    from rtrec.offline.pipeline import ipw_weights
    w = ipw_weights(np.array([0.5, 0.01, 1e-6, np.nan]), clip=20)
    assert abs(w.mean() - 1) < 1e-9 and w[1] == w[2]          # 1/0.01 = 100 and 1e6 both clipped to 20


def test_level2_alert_fires_after_sustained_fallback(monkeypatch):
    from rtrec.runtime import serve
    sent = []
    monkeypatch.setattr(serve, "notify", sent.append)

    class Dummy:
        L2_ALERT_SHARE, L2_ALERT_AFTER_S = serve.Server.L2_ALERT_SHARE, serve.Server.L2_ALERT_AFTER_S
        _check_level2 = serve.Server._check_level2

        class runner:
            name = "x"

        class metrics:
            @staticmethod
            def inc(*_):
                pass

    d = Dummy()
    for i in range(30):  # 10 s ticks, all answers from level 2
        d._check_level2(i * 10.0, {"served_level1": 0, "served_level2": 30 * (i + 1)})
    assert len(sent) == 1 and "DEGRADED" in sent[0]
