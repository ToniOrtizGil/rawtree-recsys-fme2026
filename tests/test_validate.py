from rtrec.contract import State
from rtrec.validate import finalize, is_valid


def _state(n=30):
    pids = [f"p_{i:03d}" for i in range(n)]
    return State(available=frozenset(pids[2:]), safe_list=pids)


def test_repairs_duplicates_unavailable_and_short_lists():
    s = _state()
    out = finalize(["p_005", "p_005", "p_000", "p_007"], s)
    assert out[:2] == ["p_005", "p_007"]
    assert is_valid(out, s)


def test_none_and_garbage():
    s = _state()
    assert is_valid(finalize(None, s), s)
    assert is_valid(finalize(["nope"] * 50, s), s)


def test_keeps_policy_order_when_valid():
    s = _state()
    mine = [f"p_{i:03d}" for i in range(29, 19, -1)]
    assert finalize(mine, s) == mine
