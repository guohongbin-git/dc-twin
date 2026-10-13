"""BerthTruck 到达/停靠/离场时刻表 (蓝图 §3-P0)。

经 DemandEngine 外部到达源契约 (arrivals_in) 进需求主轴: 一次到访
产生一张装运单 (ShipmentOrder), 引擎按 t_s 与内部订单归并 —— core
不解释卡车语义, 只做时间归并。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class TruckVisit:
    """一次到访: 停靠窗 [t_arrive_s, t_depart_s] (sim_t)。"""

    truck_id: str
    tk_node: str
    dock_node: str
    t_arrive_s: float
    t_depart_s: float


@dataclass(frozen=True)
class ShipmentOrder:
    """装运单 (DemandEngine 到达源产物; build_task 之外的场景单)。"""

    order_id: str
    truck_id: str
    tk_node: str
    dock_node: str
    t_s: float
    kind: str = "SHIPMENT"


class TruckTimetable:
    """时刻表: 到访序列 (确定性, 按 t_arrive 排序) + 到达源适配。"""

    def __init__(self, visits: Iterable[tuple]):
        parsed = [TruckVisit(*v) for v in visits]
        for v in parsed:
            if not v.t_arrive_s < v.t_depart_s:
                raise ValueError(f"到访窗非法 (arrive≥depart): {v}")
        self.visits: tuple[TruckVisit, ...] = tuple(
            sorted(parsed, key=lambda v: (v.t_arrive_s, v.truck_id)))

    def arrivals_in(self, t0_s: float, t1_s: float) -> list[ShipmentOrder]:
        """DemandEngine 到达源契约: [t0,t1) 窗内到站 → 装运单。"""
        return [
            ShipmentOrder(order_id=f"SHP-{v.truck_id}", truck_id=v.truck_id,
                          tk_node=v.tk_node, dock_node=v.dock_node,
                          t_s=v.t_arrive_s)
            for v in self.visits if t0_s <= v.t_arrive_s < t1_s]

    def due(self, t_s: float) -> list[TruckVisit]:
        """t_s 及以前已到站的到访 (驶入动画触发用)。"""
        return [v for v in self.visits if v.t_arrive_s <= t_s]

    def visit_of(self, truck_id: str) -> TruckVisit:
        for v in self.visits:
            if v.truck_id == truck_id:
                return v
        raise KeyError(f"时刻表无此卡车: {truck_id!r}")
