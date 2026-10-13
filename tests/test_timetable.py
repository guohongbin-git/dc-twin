"""到达/停靠/离场时刻表 + DemandEngine 外部到达源契约 (蓝图 §3-P0)。"""
from __future__ import annotations

import pytest

from dc_twin.timetable import ShipmentOrder, TruckTimetable


def _tt() -> TruckTimetable:
    return TruckTimetable((
        ("TRK01", "tk:T42", "D_G42", 10.0, 60.0),
        ("TRK02", "tk:T43", "D_G43", 30.0, 90.0),
    ))


def test_arrivals_in_window_is_sorted_and_bounded():
    tt = _tt()
    a = tt.arrivals_in(0.0, 100.0)
    assert [o.truck_id for o in a] == ["TRK01", "TRK02"]
    b = tt.arrivals_in(15.0, 100.0)
    assert [o.truck_id for o in b] == ["TRK02"]         # 已到站不再重发
    assert tt.arrivals_in(0.0, 5.0) == []
    ts = [o.t_s for o in a]
    assert ts == sorted(ts)


def test_shipment_order_fields():
    o = _tt().arrivals_in(0.0, 100.0)[0]
    assert isinstance(o, ShipmentOrder)
    assert o.order_id and o.tk_node == "tk:T42" and o.dock_node == "D_G42"
    assert o.t_s == 10.0


def test_timetable_feeds_demand_engine_as_arrival_source():
    from sim.demand import DemandEngine

    eng = DemandEngine(initial_pallets=64, arrival_sources=(_tt(),))
    fresh = eng.generate_until(100.0)
    ships = [o for o in fresh if isinstance(o, ShipmentOrder)]
    assert [s.truck_id for s in ships] == ["TRK01", "TRK02"]
    # 归并有序: 与引擎内部订单按 t_s 交错
    ts = [o.t_s for o in fresh]
    assert ts == sorted(ts)
    # 游标推进后不重复
    assert not [o for o in eng.generate_until(120.0)
                if isinstance(o, ShipmentOrder)]


def test_visit_lifecycle_accessors():
    tt = _tt()
    assert tt.due(9.9) == []
    assert [v.truck_id for v in tt.due(10.0)] == ["TRK01"]
    assert [v.truck_id for v in tt.due(61.0)] == ["TRK01", "TRK02"]
    with pytest.raises(ValueError):
        TruckTimetable((("TRK01", "tk:T42", "D_G42", 60.0, 10.0),))
