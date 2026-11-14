"""Forensics (H1-H11) runs end to end on a small synthetic dump, history + live tables."""
from rtrec.data.load import load_history
from sim.dump import write_dump
from sim.params import SimConfig
from sim.shop import make_simulator


def test_forensics_runs_end_to_end(tmp_path):
    """Every H1-H11 question produces an answer on a small synthetic dump with live tables."""
    from rtrec.data import forensics as FZ
    from rtrec.data.load import load_live
    from sim.dump import write_live
    sim = make_simulator(6, SimConfig.small(history_days=3))
    write_dump(sim.history(3), tmp_path, {"source": "synthetic"})
    write_live(sim, 5, tmp_path)
    res = FZ.run_all(load_history(tmp_path), load_live(tmp_path))
    assert [f.question[:3] for f in res] == ["H1 ", "H2 ", "H3 ", "H4 ", "H5 ", "H6 ", "H7 ", "H8 ", "H9 ", "H10", "H11"]
    assert all(f.answer for f in res)
    assert "Plackett-Luce" in res[8].answer and "NOT" not in res[8].answer
    assert FZ.to_markdown(res).count("###") == 11
