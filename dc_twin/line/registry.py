"""产线场景注册表 (蓝图 §13/§14: 机构参数进可扩展注册表)。

叙事定位 (§14 可口可乐装瓶体系对标): line-feed = 装瓶厂产线供料
(Evidence-Grounded Bottling Line); ST-PRESS 为「压盖/冲压类工位」
的力学抽象 —— 咬合 + 压线力判定与力剖面证据同构。

工位 (ST-*) 与线边库架位 (L-B*) 的绑定关系、冲压机构参数全部经此
注册进场景表 —— 运行时/评分器只读表, 不硬编码工位语义。首个实现
工位 = ST-PRESS (冲压); 规划中的工位行 (implemented=False) 允许
预登记绑定, 供错工位追溯判例使用。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PressSpec:
    """冲压工位机构参数 (MuJoCo 机构 + 证据门限 + 节拍预算)。"""

    station_id: str
    takt_s: float                    # 节拍窗 (工序完成门: 窗内)
    press_depth_m: float             # 冲压成形深度 (头下压量)
    force_min_n: float               # 力剖面门限 (峰值 ≥)
    feed_pos_tol_m: float            # 上料到位容差 (L2a 几何级, xy)
    force_window_s: float = 0.30     # 力剖面采样窗 (触底后)
    press_speed_mps: float = 0.008   # 冲压头升降速度
    push_speed_mps: float = 0.05     # 推料滑台速度
    implemented: bool = True         # 规划工位行可为 False (无机构)
    description: str = ""


_STATIONS: dict[str, PressSpec] = {}
_RACKS: dict[str, str] = {}


def register_station(spec: PressSpec) -> PressSpec:
    """注册工位机构参数; 重注册/非法参数拒绝。"""
    if not spec.station_id or not spec.station_id.startswith("ST-"):
        raise ValueError(f"工位 id 非法 (ST- 前缀): {spec.station_id!r}")
    if spec.station_id in _STATIONS:
        raise ValueError(f"工位已注册: {spec.station_id!r}")
    if spec.takt_s <= 0 or spec.press_depth_m <= 0 or spec.force_min_n <= 0:
        raise ValueError(f"机构参数非法: {spec}")
    _STATIONS[spec.station_id] = spec
    return spec


def unregister_station(station_id: str) -> None:
    """测试钩子: 撤销注册 (连动解除架位绑定)。"""
    _STATIONS.pop(station_id, None)
    for rack in [r for r, s in _RACKS.items() if s == station_id]:
        del _RACKS[rack]


def station(station_id: str) -> PressSpec:
    if station_id not in _STATIONS:
        raise KeyError(f"未注册工位: {station_id!r}; 可用 {sorted(_STATIONS)}")
    return _STATIONS[station_id]


def registered_stations() -> dict[str, PressSpec]:
    return dict(_STATIONS)


def bind_rack(rack_node: str, station_id: str) -> None:
    """绑定线边库架位 → 工位 (供料关系; 重绑/未知工位拒绝)。"""
    if station_id not in _STATIONS:
        raise KeyError(f"绑定到未注册工位: {station_id!r}")
    if rack_node in _RACKS:
        raise ValueError(f"架位已绑定: {rack_node!r} → {_RACKS[rack_node]!r}")
    _RACKS[rack_node] = station_id


def rack_bindings() -> dict[str, str]:
    return dict(_RACKS)


def station_sources(station_id: str) -> tuple[str, ...]:
    """工位的供料架位 (绑定序)。"""
    return tuple(r for r, s in _RACKS.items() if s == station_id)


# ---------------------------------------------------------------- 场景默认
register_station(PressSpec(
    station_id="ST-PRESS", takt_s=40.0, press_depth_m=0.005,
    force_min_n=6.0, feed_pos_tol_m=0.008,
    description="压盖/冲压类工位力学抽象 (蓝图 §13 首个工位, §14 对标)"))
for _r in ("L-B1", "L-B2"):
    bind_rack(_r, "ST-PRESS")
#: 规划中的下一工位 (仅注册行 + 架位绑定, 无 MuJoCo 机构):
#: 错工位投料判例的追溯锚点 —— L-B3 物料不属于 ST-PRESS。
register_station(PressSpec(
    station_id="ST-WELD", takt_s=40.0, press_depth_m=0.005,
    force_min_n=6.0, feed_pos_tol_m=0.008, implemented=False,
    description="规划工位 (P0' 不实现机构; 供追溯判例, §14.1 占位谱系)"))
bind_rack("L-B3", "ST-WELD")
