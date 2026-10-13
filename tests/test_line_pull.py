"""takt 看板拉料 (DemandEngine pull 源) 契约 (蓝图 §13)。"""
from __future__ import annotations

import pytest

from dc_twin.line.pull import ReplenishOrder, TaktKanban


def _kb(takt=10.0, t0=5.0):
    return TaktKanban(station_id="ST-PRESS", takt_s=takt, t0_s=t0,
                      rack_node="L-B1")


def test_arrivals_in_window_bounded_and_sorted():
    kb = _kb()
    a = kb.arrivals_in(0.0, 100.0)
    assert [o.t_s for o in a] == [5.0, 15.0, 25.0, 35.0, 45.0, 55.0,
                                  65.0, 75.0, 85.0, 95.0]  # t0+takt*k, k>=0
    assert [o.t_s for o in kb.arrivals_in(0.0, 15.0)] == [5.0]
    assert [o.t_s for o in kb.arrivals_in(20.0, 40.0)] == [25.0, 35.0]


def test_replenish_order_fields():
    o = _kb().arrivals_in(0.0, 100.0)[0]
    assert isinstance(o, ReplenishOrder)
    assert o.order_id and o.station_id == "ST-PRESS"
    assert o.rack_node == "L-B1" and o.kind == "REPLENISH"


def test_feeds_demand_engine_as_arrival_source():
    from sim.demand import DemandEngine

    eng = DemandEngine(initial_pallets=64, arrival_sources=(_kb(),))
    fresh = eng.generate_until(50.0)
    reps = [o for o in fresh if isinstance(o, ReplenishOrder)]
    assert [o.t_s for o in reps] == [5.0, 15.0, 25.0, 35.0, 45.0]
    ts = [o.t_s for o in fresh]
    assert ts == sorted(ts)                              # 归并有序
    assert not [o for o in eng.generate_until(50.0)
                if isinstance(o, ReplenishOrder)]        # 游标推进不重发


def test_invalid_takt_rejected():
    with pytest.raises(ValueError):
        TaktKanban(station_id="ST-PRESS", takt_s=0.0, t0_s=0.0,
                   rack_node="L-B1")
