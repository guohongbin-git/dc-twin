"""LineCell —— 装瓶厂产线场景装配 (蓝图 §13/§14)。

在 WarehouseCell 生成的 XML 上注入 (场景组合, 不改内核语义):
- 线边库 L-B1..B3 (2 层小架, line.racks);
- ST-* 工位机构: 夹具 nest (背止挡定位) + 冲压头 slide actuator +
  推料滑台 + 夹紧 weld (默认失活, 激活即「夹紧确认」);
- 物流结构: 暂存门架 (新托盘+工件列) / 产出门架 (成品托盘列);
- 产线托盘 pl_* (自由体) + 工件 wp_* (自由体, 坐托盘顶)。

净空链 (全程无碰撞走廊): AGV 顶 0.062 < 条带底 0.090; 立柱对角
布置, 距 AGV 停位点 ≥0.16 (盘 r0.12+余量); 托盘交接面全链 0.095
(同 stand/bed 顶销交接链)。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import mujoco
import numpy as np

from sim.warehouse_cell import PalletView, WarehouseCell

from . import racks as _racks
from .registry import PressSpec, rack_bindings, station as _station_spec

#: 工位平面 (相对走廊线的托盘止点; 背止挡面 = 止点 +0.030)
NEST_STOP_DY = 0.225
EXIT_STOP_DY = 0.425
#: 夹具/出口承托面 (全链同 stand_top_z)
PLANE_Z = _racks.L0_TOP_Z
#: 冲压头: q=0 回缩位头底 0.170 (工件顶 ~0.141 上方净空 29mm);
#: 下压行程 q ∈ [-0.042, 0], 目标 q* = -0.035 → 头底 0.135,
#: 成形压入 ~6mm (位置伺服力 = kp×|Δq|, 力剖面采样源)
PRESS_Q_RANGE = 0.042
PRESS_Q_TARGET = -0.035
#: 推料滑台: q=0 刀前 2.060 (架面托盘南脸 2.070 前 10mm);
#: 进料行程 0.235 (托盘止于 nest), 出料行程 0.435 (止于出口)
PUSH_Q_RANGE = 0.46
PUSH_Q_FEED = 0.235
PUSH_Q_EXIT = 0.435
#: 工件 (瓶坯/小箱体) 半尺寸与质量
WP_HALF_XYZ = (0.024, 0.018, 0.014)
WP_MASS_KG = 0.05
#: 暂存/产出门架
STAGING_XS = (4.15, 4.70)
SLOTS_PER_BENCH = 30
OUT_X = 2.50
OUT_Y0 = 1.90


def run_segments(cell, segs: list[tuple]) -> None:
    """单通道段执行器 (FleetRunner 同构: 100Hz 位置闭环 + settle)。"""
    sub = max(1, round(0.01 / cell.model.opt.timestep))
    for seg in segs:
        aid, build, off = seg[0], seg[1], seg[2]
        settle = seg[3] if len(seg) > 3 else 0.15
        ramp = build()
        t0 = cell.data.time
        while cell.data.time - t0 < ramp.duration:
            cell.data.ctrl[aid] = ramp.at(cell.data.time - t0) - off
            cell.step(sub)
        cell.data.ctrl[aid] = ramp.target - off
        for _ in range(max(1, int(settle / (sub * cell.model.opt.timestep)))):
            cell.step(sub)


@dataclass(frozen=True)
class StationGeometry:
    """工位平面几何 (nest/exit 止点矩形 + 机构锚点 + MJCF 生成)。"""

    spec: PressSpec
    rack_node: str

    @property
    def x(self) -> float:
        return _racks.register()[self.rack_node][0]

    @property
    def cy(self) -> float:
        return _racks.LINE_CORRIDOR_Y

    @property
    def nest_y(self) -> float:
        return self.cy + NEST_STOP_DY

    @property
    def exit_y(self) -> float:
        return self.cy + EXIT_STOP_DY

    def nest_rect(self) -> tuple[float, float, float, float]:
        """nest 托盘止位矩形 (x0,x1,y0,y1) —— 评分器 custody 核对。"""
        return (self.x - 0.048, self.x + 0.048,
                self.nest_y - 0.034, self.nest_y + 0.034)

    def exit_rect(self) -> tuple[float, float, float, float]:
        return (self.x - 0.048, self.x + 0.048,
                self.exit_y - 0.034, self.exit_y + 0.034)

    # ------------------------------------------------------------ MJCF
    def xml(self) -> str:
        x, sid = self.x, self.spec.station_id
        ny, ey, cy = self.nest_y, self.exit_y, self.cy
        g = [f'  <body name="line_fixt_{sid}" pos="0 0 0">']
        A = g.append
        for sx in (-0.033, 0.033):                      # nest/出口条带
            for tag, y_b in (("nest", ny), ("exit", ey)):
                for sy in (y_b - 0.047, y_b + 0.047):
                    A(f'    <geom name="fixt_{sid}_{tag}_{sx:.3f}_{sy:.3f}" '
                      f'type="box" pos="{x + sx:.3f} {sy:.3f} {PLANE_Z - 0.002:.4f}" '
                      f'size="0.011 0.023 0.002" rgba="0.32 0.46 0.52 1"/>')
        for sx in (-0.033, 0.033):                      # 过渡导轨 架面→nest→出口
            A(f'    <geom name="fixt_{sid}_rail_{sx:.3f}" type="box" '
              f'pos="{x + sx:.3f} 2.290 {PLANE_Z - 0.0025:.4f}" '
              f'size="0.004 0.270 0.0025" rgba="0.28 0.30 0.34 1"/>')
        for tag, y_stop in (("nest", ny + 0.035), ("exit", ey + 0.035)):
            A(f'    <geom name="fixt_{sid}_stop_{tag}" type="box" '
              f'pos="{x:.3f} {y_stop:.3f} {PLANE_Z + 0.0225:.4f}" '
              f'size="0.055 0.005 0.0225" rgba="0.85 0.30 0.20 1"/>')
        # Portal: nestp=冲压门架 / exitp=出口挡 / sp=推料滑台支座
        for tag, py, hz, (b0, b1) in (("nestp", ny, 0.17, (0.34, 0.38)),
                                      ("exitp", ey, 0.07, (0.14, 0.16)),
                                      ("sp", cy - 0.10, 0.15, (0.30, 0.324))):
            for sx in (-0.16, 0.16):
                A(f'    <geom name="fixt_{sid}_leg_{tag}_{sx:.2f}" type="box" '
                  f'pos="{x + sx:.2f} {py:.3f} {hz:.3f}" '
                  f'size="0.008 0.008 {hz:.3f}" rgba="0.22 0.24 0.28 1"/>')
            A(f'    <geom name="fixt_{sid}_beam_{tag}" type="box" '
              f'pos="{x:.3f} {py:.3f} {(b0 + b1) / 2:.3f}" '
              f'size="0.170 0.010 {(b1 - b0) / 2:.3f}" rgba="0.30 0.32 0.36 1"/>')
        # ---- 冲压头 (slide actuator; ctrlrange 显式) ----
        A(f'    <body name="line_press_{sid}" pos="{x:.3f} {ny:.3f} 0.180">')
        A(f'      <joint name="line_{sid}_press_j" type="slide" axis="0 0 1" '
          f'range="{-PRESS_Q_RANGE} 0" damping="30"/>')
        A(f'      <geom name="line_{sid}_head" type="box" pos="0 0 0" '
          f'size="0.030 0.026 0.010" mass="0.30" '
          f'friction="0.9 0.005 0.0001" rgba="0.75 0.76 0.80 1"/>')
        A(f'      <geom name="line_{sid}_stem" type="cylinder" pos="0 0 0.085" '
          f'size="0.006 0.075" mass="0" rgba="0.45 0.46 0.50 1" '
          f'contype="0" conaffinity="0"/>')
        A("    </body>")
        # ---- 推料滑台 (slide actuator) ----
        A(f'    <body name="line_push_{sid}" pos="{x:.3f} {cy - 0.045:.3f} 0">')
        A(f'      <joint name="line_{sid}_push_j" type="slide" axis="0 1 0" '
          f'range="0 {PUSH_Q_RANGE}" damping="40"/>')
        A(f'      <geom name="line_{sid}_blade" type="box" pos="0 0 0.1125" '
          f'size="0.046 0.005 0.016" mass="0.20" '
          f'friction="0.6 0.005 0.0001" rgba="0.90 0.75 0.10 1"/>')
        for sx in (-0.06, 0.06):
            A(f'      <geom name="line_{sid}_strut_{sx:.2f}" type="box" '
              f'pos="{sx:.2f} 0 0.225" size="0.008 0.006 0.097" mass="0" '
              f'rgba="0.45 0.46 0.50 1" contype="0" conaffinity="0"/>')
        A(f'      <geom name="line_{sid}_arm" type="box" pos="0 -0.03 0.315" '
          f'size="0.070 0.035 0.006" mass="0" rgba="0.45 0.46 0.50 1" '
          f'contype="0" conaffinity="0"/>')
        A("    </body>")
        A("  </body>")
        return "\n".join(g)

    def actuators_xml(self) -> str:
        sid = self.spec.station_id
        return (
            f'    <position name="line_{sid}_press" joint="line_{sid}_press_j" '
            f'kp="4000" forcerange="-80 80" ctrlrange="{-PRESS_Q_RANGE} 0"/>\n'
            f'    <position name="line_{sid}_push" joint="line_{sid}_push_j" '
            f'kp="3000" forcerange="-60 60" ctrlrange="0 {PUSH_Q_RANGE}"/>')


def _bench_xml(tag: str, x: float, y_lo: float, y_hi: float) -> str:
    """暂存/产出门架: 导轨 (顶 0.095) + 对角立柱 (AGV 包络外)。"""
    ymid, yhalf = (y_lo + y_hi) / 2, (y_hi - y_lo) / 2
    ys = [y_lo]
    while ys[-1] + 0.5 < y_hi - 1e-9:
        ys.append(ys[-1] + 0.5)
    if ys[-1] < y_hi - 1e-9:
        ys.append(y_hi)
    rails = "\n".join(
        f'    <geom name="bench_{tag}_rail_{sx:.2f}" type="box" '
        f'pos="{x + sx:.2f} {ymid:.3f} {PLANE_Z - 0.0025:.4f}" '
        f'size="0.004 {yhalf:.4f} 0.0025" rgba="0.28 0.30 0.34 1"/>'
        for sx in (-0.033, 0.033))
    legs = "\n".join(
        f'    <geom name="bench_{tag}_leg_{sx:.2f}_{ly:.2f}" type="box" '
        f'pos="{x + sx:.2f} {ly:.3f} 0.045" size="0.006 0.006 0.045" '
        f'rgba="0.22 0.24 0.28 1"/>'
        for ly in ys for sx in (-0.19, 0.19))
    return f'  <body name="bench_{tag}" pos="0 0 0">\n{rails}\n{legs}\n  </body>'


def _workpiece_xml(name: str, x: float, y: float, z: float) -> str:
    hx, hy, hz = WP_HALF_XYZ
    return f"""
    <body name="{name}" pos="{x:.6f} {y:.6f} {z:.6f}">
      <freejoint name="free_{name}"/>
      <geom name="{name}_body" type="box" pos="0 0 0" size="{hx} {hy} {hz}"
            mass="{WP_MASS_KG}" friction="1.0 0.005 0.0001" rgba="0.25 0.45 0.75 1"/>
    </body>"""


def _quat_conj(q: np.ndarray) -> np.ndarray:
    return np.array([q[0], -q[1], -q[2], -q[3]])


def _quat_mul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    w1, x1, y1, z1 = a
    w2, x2, y2, z2 = b
    return np.array([w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
                     w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
                     w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
                     w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2])


@dataclass(frozen=True)
class PressHandle:
    """工位机构句柄: 执行器/关节/几何 id (运行时驱动 + 力采样)。"""

    spec: PressSpec
    geo: StationGeometry
    press_aid: int
    push_aid: int
    press_qadr: int
    push_qadr: int
    head_bid: int
    head_gids: frozenset
    nest_strip_gids: frozenset
    fixture_bid: int
    welds: dict[str, int]

    @property
    def station_id(self) -> str:
        return self.spec.station_id

    def q_press(self, cell) -> float:
        return float(cell.data.qpos[self.press_qadr])

    def q_push(self, cell) -> float:
        return float(cell.data.qpos[self.push_qadr])

    def head_bottom_z(self, cell) -> float:
        return float(cell.data.xpos[self.head_bid][2]) - 0.010

    def in_position(self, cell) -> bool:
        """设备字判据: 冲压头到位 (位置环; 不感知接触/负载, 同 B4)。"""
        return abs(self.q_press(cell) - PRESS_Q_TARGET) < 5e-4


class LineCell(WarehouseCell):
    """注入产线场景的 WarehouseCell (装瓶厂产线供料, 蓝图 §13/§14)。"""

    def __init__(self, *, line_racks: tuple[str, ...] = ("L-B1", "L-B2", "L-B3"),
                 stations: tuple[str, ...] = ("ST-PRESS",),
                 line_pallets: int = 8,
                 preload_racks: tuple[str, ...] = ("L-B1", "L-B2"),
                 workpieces: bool = True, **kw):
        if line_pallets < len(preload_racks):
            raise ValueError("托盘数少于预载数")
        self._line_racks = tuple(line_racks)
        self._station_ids = tuple(stations)
        self._line_pallets = int(line_pallets)
        self._preload_racks = tuple(preload_racks)
        self._with_wp = bool(workpieces)
        self.geos: dict[str, StationGeometry] = {}
        self.presses: dict[str, PressHandle] = {}
        self.racks: dict[str, _racks.RackGeometry] = {}
        self.workpieces: dict[str, dict] = {}
        nodes = _racks.register()
        for node in self._line_racks:
            self.racks[node] = _racks.RackGeometry(node, nodes[node][:2])
        for sid in self._station_ids:
            spec = _station_spec(sid)
            rack = next(r for r, s in rack_bindings().items() if s == sid)
            self.geos[sid] = StationGeometry(spec, rack)
        super().__init__(**kw)

    # ------------------------------------------------------------ 播种
    def _pallet_names(self) -> list[str]:
        return []                       # 产线托盘由 _xml 直接注入

    @property
    def n_staging(self) -> int:
        return self._line_pallets - len(self._preload_racks)

    def staging_slot(self, k: int) -> tuple[float, float]:
        if k >= self.n_staging:
            raise KeyError(f"暂存位不足: {k} ≥ {self.n_staging}")
        return (STAGING_XS[k // SLOTS_PER_BENCH],
                _racks.LINE_CORRIDOR_Y + 0.10 * (k % SLOTS_PER_BENCH))

    def out_slot(self, j: int) -> tuple[float, float]:
        return (OUT_X, OUT_Y0 - 0.10 * j)

    def _pallet_plan(self) -> list[tuple[str, float, float]]:
        """托盘播种表: 预载架位在前, 其余按暂存列序。"""
        out = [(f"pl_{k:03d}", *self.racks[node].xy)
               for k, node in enumerate(self._preload_racks)]
        out += [(f"pl_{len(self._preload_racks) + j:03d}", *self.staging_slot(j))
                for j in range(self.n_staging)]
        return out

    def pallet_of_wp(self, wp: str) -> str:
        return self.workpieces[wp]["pallet"]

    # ------------------------------------------------------------ MJCF
    def _xml(self, dt: float) -> str:
        xml = super()._xml(dt)
        xml = xml.replace('<size nconmax="600" njmax="400"/>',
                          '<size nconmax="2000" njmax="1200"/>', 1)
        bodies = [g.xml() for g in self.geos.values()]
        bodies += [self.racks[n].xml() for n in self._line_racks]
        n_out = max(1, self._line_pallets - 2)
        bodies.append(_bench_xml("stgA", STAGING_XS[0], 2.00,
                                 2.00 + 0.10 * min(self.n_staging,
                                                   SLOTS_PER_BENCH)))
        if self.n_staging > SLOTS_PER_BENCH:
            bodies.append(_bench_xml(
                "stgB", STAGING_XS[1], 2.00,
                2.00 + 0.10 * (self.n_staging - SLOTS_PER_BENCH)))
        bodies.append(_bench_xml("out", OUT_X,
                                 OUT_Y0 - 0.10 * (n_out - 1) - 0.05,
                                 OUT_Y0 + 0.05))
        plan = self._pallet_plan()
        for name, x, y in plan:
            bodies.append(self._pallet_xml(name, x, y, PLANE_Z, 0.0))
        if self._with_wp:
            for _name, x, y in plan:
                z = PLANE_Z + 0.020 + WP_HALF_XYZ[2] + 0.001
                bodies.append(_workpiece_xml("wp_" + _name[-3:], x, y, z))
        # 夹紧 weld (每托盘 × 每实现工位, 默认失活; 编译期记录相对位形)
        welds = "".join(
            f'\n    <weld name="clamp_{sid}_{name}" body1="line_fixt_{sid}" '
            f'body2="{name}" active="false"/>'
            for sid, geo in self.geos.items() if geo.spec.implemented
            for name, _x, _y in plan)
        eq = f"\n  <equality>{welds}\n  </equality>" if welds else ""
        acts = "\n".join(g.actuators_xml() for g in self.geos.values())
        xml = xml.replace("  </worldbody>",
                          "".join("\n" + b for b in bodies) + "\n  </worldbody>", 1)
        xml = xml.replace("  </actuator>", acts + "\n  </actuator>", 1)
        return xml.replace("</mujoco>", f"{eq}\n</mujoco>", 1) if eq else xml

    # ------------------------------------------------------------ id 缓存
    def _cache_ids(self):
        super()._cache_ids()
        m, T = self.model, mujoco.mjtObj
        self.pallets = {}
        for name, _x, _y in self._pallet_plan():
            bid = mujoco.mj_name2id(m, T.mjOBJ_BODY, name)
            if bid < 0:
                raise KeyError(f"产线托盘未注入: {name}")
            gids = [g for g in range(m.ngeom) if m.geom_bodyid[g] == bid]
            self.pallets[name] = PalletView(name, bid, gids,
                                            float(m.body_mass[bid]))
        self.workpieces = {}
        if self._with_wp:
            for name, _x, _y in self._pallet_plan():
                wname = "wp_" + name[-3:]
                bid = mujoco.mj_name2id(m, T.mjOBJ_BODY, wname)
                if bid < 0:
                    raise KeyError(f"工件未注入: {wname}")
                self.workpieces[wname] = {
                    "bid": bid,
                    "gids": frozenset(g for g in range(m.ngeom)
                                      if m.geom_bodyid[g] == bid),
                    "pallet": name}
        for sid, geo in self.geos.items():
            s = geo.spec.station_id
            press_aid = mujoco.mj_name2id(m, T.mjOBJ_ACTUATOR, f"line_{s}_press")
            push_aid = mujoco.mj_name2id(m, T.mjOBJ_ACTUATOR, f"line_{s}_push")
            pj = mujoco.mj_name2id(m, T.mjOBJ_JOINT, f"line_{s}_press_j")
            gj = mujoco.mj_name2id(m, T.mjOBJ_JOINT, f"line_{s}_push_j")
            hb = mujoco.mj_name2id(m, T.mjOBJ_BODY, f"line_press_{s}")
            fb = mujoco.mj_name2id(m, T.mjOBJ_BODY, f"line_fixt_{s}")
            if min(press_aid, push_aid, pj, gj, hb, fb) < 0:
                raise KeyError(f"工位机构未注入: {s}")
            hg = frozenset(g for g in range(m.ngeom)
                           if m.geom_bodyid[g] == hb
                           and (mujoco.mj_id2name(m, T.mjOBJ_GEOM, g) or "")
                           .endswith("_head"))
            nsg = frozenset(
                g for g in range(m.ngeom)
                if m.geom_bodyid[g] == fb
                and "_nest_" in (mujoco.mj_id2name(m, T.mjOBJ_GEOM, g) or ""))
            welds = {}
            if geo.spec.implemented:
                for name, _x, _y in self._pallet_plan():
                    eid = mujoco.mj_name2id(m, T.mjOBJ_EQUALITY,
                                            f"clamp_{s}_{name}")
                    if eid < 0:
                        raise KeyError(f"夹紧约束未注入: clamp_{s}_{name}")
                    welds[name] = eid
            self.presses[s] = PressHandle(
                spec=geo.spec, geo=geo, press_aid=press_aid, push_aid=push_aid,
                press_qadr=m.jnt_qposadr[pj], push_qadr=m.jnt_qposadr[gj],
                head_bid=hb, head_gids=hg, nest_strip_gids=nsg,
                fixture_bid=fb, welds=welds)

    # ------------------------------------------------------------ 操作
    def _press_of(self, pallet: str) -> PressHandle:
        for h in self.presses.values():
            if pallet in h.welds:
                return h
        raise KeyError(f"未配置夹紧约束: {pallet}")

    def clamp(self, pallet: str, on: bool) -> None:
        """夹紧/松开。夹紧 = 在**当前位形**锁定 weld (夹具↔托盘):
        重写 eq_data (relpos = R1ᵀ(p2−p1), relquat = q1⁻¹∘q2, MuJoCo
        3.x weld 布局 [anchor|relpos|relquat|torquescale]) 后激活
        —— 夹紧确认即 eq_active 位, 且物理成立 (未夹紧时冲压可推移
        托盘); 松开仅失活。"""
        h = self._press_of(pallet)
        eid = h.welds[pallet]
        if on:
            m, d = self.model, self.data
            b1, b2 = h.fixture_bid, self.pallets[pallet].bid
            p1, p2 = d.xpos[b1], d.xpos[b2]
            r1 = d.xmat[b1].reshape(3, 3)
            q1, q2 = d.xquat[b1], d.xquat[b2]
            rel_q = _quat_mul(_quat_conj(q1), q2)
            ed = m.eq_data[eid]
            ed[:] = 0.0
            ed[3:6] = r1.T @ (p2 - p1)          # relpos (body1 局部)
            ed[6:10] = rel_q                    # relquat (body2←body1)
            ed[10] = 1.0                        # torquescale
            mujoco.mj_forward(m, d)
        self.data.eq_active[eid] = 1 if on else 0

    def clamp_active(self, pallet: str) -> bool:
        h = self._press_of(pallet)
        return bool(self.data.eq_active[h.welds[pallet]])

    def wp_pose(self, name: str) -> tuple[np.ndarray, np.ndarray]:
        w = self.workpieces[name]
        return (self.data.xpos[w["bid"]].copy(),
                self.data.xquat[w["bid"]].copy())

    def contact_force_n(self, station_id: str, wp_name: str) -> float:
        """冲压头 ↔ 工件 接触法向力合力 (力剖面采样原语)。"""
        h = self.presses[station_id]
        wg = self.workpieces[wp_name]["gids"]
        total, f1 = 0.0, np.zeros(6)
        for i in range(self.data.ncon):
            c = self.data.contact[i]
            if ((c.geom1 in h.head_gids and c.geom2 in wg)
                    or (c.geom2 in h.head_gids and c.geom1 in wg)):
                mujoco.mj_contactForce(self.model, self.data, i, f1)
                total += abs(float(f1[0]))
        return total

    def nest_rect(self, station_id: str) -> tuple[float, float, float, float]:
        return self.geos[station_id].nest_rect()

    def exit_rect(self, station_id: str) -> tuple[float, float, float, float]:
        return self.geos[station_id].exit_rect()
