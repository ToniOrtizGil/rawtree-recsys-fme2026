"""Open a Store by name, so one flag switches the dashboard between the fixture, the simulator and RawTree.

    demo     dashboard.demo.DemoStore (synthetic, self-answering; works with no other agent's code)
    fake     A1's FakeStore over A2's simulator   (rtrec.stores.fake)
    rawtree  A1's RawTreeStore, live              (rtrec.stores.rawtree; needs TEAM_ID + key)

Default comes from $RTREC_STORE (the master's deploy switch), else "demo". We look up A1's code by
convention instead of importing it at module level, so this package works before A1's branch is merged.
"""
from __future__ import annotations

import importlib
import os


def default_kind() -> str:
    return os.environ.get("RTREC_STORE", "demo")


def open_store(kind: str | None = None, chaos: bool = False, seed: int = 7):
    kind = kind or default_kind()
    if kind == "demo":
        from .demo import DemoStore
        return DemoStore(seed=seed, chaos=chaos)
    try:
        pkg = importlib.import_module("rtrec.stores")
    except ImportError as e:
        raise SystemExit(f"--store {kind}: rtrec.stores (agent A1) is not available on this checkout ({e}). "
                         "Use --store demo, or merge A1's branch.") from e
    for fname in ("make_store", "open_store", "get_store"):
        f = getattr(pkg, fname, None)
        if callable(f):
            return f(kind)
    mod = importlib.import_module(f"rtrec.stores.{kind}")
    classes = [getattr(mod, n) for n in dir(mod) if n.endswith("Store") and isinstance(getattr(mod, n), type)
               and getattr(mod, n).__module__ == mod.__name__]
    if not classes:
        raise SystemExit(f"rtrec.stores.{kind} defines no *Store class")
    return classes[0]()
