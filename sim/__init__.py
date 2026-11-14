"""Synthetic RawTree shop (A2). Entry point: `sim.make_simulator(seed, config)`; knobs in `sim.params`."""
from .params import HiddenModel, SimConfig
from .shop import ShopSimulator, make_simulator

__all__ = ["HiddenModel", "SimConfig", "ShopSimulator", "make_simulator"]
