"""One thread feeds bursts of events and catalogue updates while another serves through LiveWrapper.

Checks: no exception on either side, every answer is valid against the `available` snapshot the
policy saw, and once a stock-out has been applied (swap done) no later answer contains that id."""
import random
import threading

from rtrec import live
from rtrec.contract import State
from rtrec.live.policy import LiveSessionPolicy, LiveWrapper
from rtrec.validate import finalize

from test_live_trends import T0, _ev, _prod


def test_wrapper_serves_while_hooks_mutate():
    s = State()
    live.init(s, [_prod(f"p{i}", f"s{i % 9}") for i in range(400)])
    s.safe_list = [f"p{i}" for i in range(60)]
    pol = LiveWrapper(LiveSessionPolicy(), explore=True)
    rng = random.Random(4)
    errors, oos_applied, bad = [], set(), []
    done = threading.Event()
    mu = threading.Lock()                                   # guards the test's own bookkeeping set

    def writer():
        try:
            t = T0
            for k in range(40):
                evs = [_ev(t + i * 0.01, f"p{rng.randrange(400)}", et=rng.choice(["view"] * 6 + ["purchase"]),
                           uid=f"u{rng.randrange(80)}", sid=f"x{rng.randrange(80)}") for i in range(2000)]
                live.on_events(evs, s)
                t += 20
                victims = [f"p{rng.randrange(400)}" for _ in range(5)]
                live.on_catalog_updates([{**_prod(v, "s"), "ts": t, "change": "out_of_stock"} for v in victims], s)
                with mu:
                    oos_applied.update(victims)
                with mu:
                    back = rng.sample(sorted(oos_applied), 2)
                live.on_catalog_updates([{**_prod(v, "s"), "ts": t + 1, "change": "back_in_stock"} for v in back], s)
                with mu:
                    oos_applied.difference_update(v for v in list(oos_applied) if v in s.available)
        except Exception as e:  # pragma: no cover
            errors.append(e)
        finally:
            done.set()

    th = threading.Thread(target=writer)
    th.start()
    n = 0
    while not done.is_set() or n < 200:
        n += 1
        before = s.available
        with mu:
            must_hide = set(oos_applied) - before                # stock-outs applied before this request
        req = {"request_id": f"r{n}", "ts": T0, "user_id": f"u{n % 80}", "is_new_user": n % 3 == 0,
               "context": {"device": "ios", "region": "Catalunya", "page": "home"},
               "recent_views": [{"product_id": f"p{n % 400}", "ts": T0}]}
        try:
            out = finalize(pol.recommend(req, s), s)
        except Exception as e:  # pragma: no cover
            errors.append(e)
            break
        hidden_throughout = must_hide - before - s.available   # out of stock before and after
        if len(out) != 10 or len(set(out)) != 10 or set(out) & hidden_throughout:
            bad.append(out)
    th.join()
    assert not errors and not bad and n > 200
