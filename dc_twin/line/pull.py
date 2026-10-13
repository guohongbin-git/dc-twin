"""takt 看板拉料 —— DemandEngine pull 源 (蓝图 §13)。

工位每拍消耗 1 件 → 看板生成一张补料单 (ReplenishOrder); 经 core
DemandEngine 外部到达源契约 (arrivals_in) 进需求主轴归并 —— 复用
dc_twin.timetable 模式 (窗口切片, 左闭右开, 不重发)。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReplenishOrder:
    """补料单 (拉料源产物; build_task 之外的场景单)。"""

    order_id: str
    station_id: str
    rack_node: str
    t_s: float
    kind: str = "REPLENISH"


class TaktKanban:
    """takt 驱动拉料: 消耗节拍 t0_s + k·takt_s → 补料单序列。"""

    def __init__(self, *, station_id: str, takt_s: float, t0_s: float,
                 rack_node: str):
        if takt_s <= 0:
            raise ValueError(f"takt 非法: {takt_s}")
        self.station_id = station_id
        self.takt_s = float(takt_s)
        self.t0_s = float(t0_s)
        self.rack_node = rack_node

    def arrivals_in(self, t0: float, t1: float) -> list[ReplenishOrder]:
        """DemandEngine 到达源契约: [t0,t1) 窗内到站的补料单。"""
        lo = max(t0 - self.t0_s, 0.0)
        k0 = int(lo / self.takt_s) + (0 if lo % self.takt_s == 0 else 1)
        out = []
        k = k0
        while (t := self.t0_s + k * self.takt_s) < t1:
            if t >= t0:
                out.append(ReplenishOrder(
                    order_id=f"KB-{k:05d}", station_id=self.station_id,
                    rack_node=self.rack_node, t_s=round(t, 6)))
            k += 1
        return out
