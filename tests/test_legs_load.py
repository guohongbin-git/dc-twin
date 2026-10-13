"""load 腿类型 (dock → tk, AGV 载具) 注册契约 (蓝图 §3-P0: 非 trunk)。"""
from __future__ import annotations

import dc_twin.legs as legs


def test_load_leg_registered_and_idempotent():
    legs.register()
    legs.register()                                    # 幂等
    from sim import dispatch

    assert dispatch.LEG_VEHICLE_TYPE["load"] == "agv"
    leg = dispatch.Leg("L1", "T", "load", "D_G42", "tk:T42", 0.16)
    assert leg.handling_s() == legs.LOAD_HANDLING_S > 0


def test_load_leg_distinct_from_trunk():
    from sim import dispatch

    assert "load" != "trunk" and dispatch.LEG_VEHICLE_TYPE["load"] == \
        dispatch.LEG_VEHICLE_TYPE["trunk"] == "agv"
    # 语义分野: load 的作业时长走 dc-twin 注册值, 非 trunk 默认
    assert dispatch.Leg("a", "T", "load", "D", "K", 0.1).handling_s() != \
        dispatch.Leg("b", "T", "trunk", "D", "K", 0.1).handling_s()


def test_three_leg_task_assigns_load_to_agv():
    legs.register()
    tk_nodes = __import__("dc_twin.layout", fromlist=["register"]).register()
    tk = sorted(tk_nodes)[0]
    from sim import dispatch

    t = dispatch.TransportTask(
        task_id="T-LOAD-1", kind=dispatch.OrderKind.OUTBOUND, sku="SKU-001",
        qty=1, weight_kg=0.05, release_s=0.0, handoff="H01",
        legs=(
            dispatch.Leg("T-LOAD-1:f", "T-LOAD-1", "fork", "R11-S0-L0",
                         "H01", 1.0, 0.100),
            dispatch.Leg("T-LOAD-1:t", "T-LOAD-1", "trunk", "H01", "D_G42",
                         2.0),
            dispatch.Leg("T-LOAD-1:l", "T-LOAD-1", "load", "D_G42", tk,
                         0.16),
        ))
    elig = t.eligible_vehicles(dispatch.FLEET_STUB)
    assert all(v.vtype == "forklift" for v in elig["T-LOAD-1:f"])
    assert all(v.vtype == "agv" for v in elig["T-LOAD-1:l"])
