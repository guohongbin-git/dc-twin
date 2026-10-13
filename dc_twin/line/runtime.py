"""产线供料闭环运行时 (line-feed P0', 蓝图 §13/§14)。

一幕: takt 看板拉料 (DemandEngine pull 源) → 供料 AGV (暂存门架 →
线边库 L-B) ∥ 工位程序 (推料入夹具 → 夹紧 → 冲压+力剖面采样 →
出料) ∥ 清型 AGV (出口 → 产出门架) → 上料/工序证据事件 (C1 时间
三元组, 程序末即采即判) → 台账 prepare/commit/ack (PLC 状态字门)。
全程 sim_t 单时钟; 真值独立导出供 dc_twin.scorer 离线对账 (C2)
—— 运行时不做账实裁决。

六类负向注入 (faults, 第 2 拍 FAULT_K 生效): empty_rack_feed /
forge_op / duplicate_feed / unclamped_press / takt_timeout_unreported /
wrong_station_feed。

AGV 车道分离 (无平面交叉): 供料 A02 西列 x=2.17 (让位 L-B1 门腿柱
2.31, 底盘缘净 10mm) / 投放列 x=2.50; 清型 A03 拾取停位 x=2.43,
产出行 y=3.02 (越顶搬运 y=3.30)。暂存支承/条带孔位与 AGV 停位让
空 ≥0.16 (销盘缘/凸台不触)。

携托联动 (P0'.5 修复): 顶升焊拾取确认后, 托盘切换 **mocap 从动**
(_CarrySlave: 位姿逐步拷贝 = 载体位形 + 携托偏移, weld 退出力路)
—— 多体场景 weld ∥ 底盘位置伺服 (30N 封顶) ∥ 销盘 noslip=20 三方
对抗不可收敛的根因因位姿不再经由约束求解而消解; 与 BerthTruck 同
一抽象 (mocap 规格下货物锁止)。落托确认后恢复动力学体 (落座稳定
窗), 台账/证据/评分器「投料=nest 物理落座」与账实一体。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

import mujoco
import numpy as np

from sim.controllers import Ramp
from sim.demand import DemandEngine
from sim.warehouse_cell import MatrixLayout

from .. import core_version
from . import evidence as _ev
from . import racks as _racks
from .evidence import LineFrame
from .ledger import LineLedger
from .plc import LinePlc
from .publisher import LineStatePublisher, resolve_state_path
from .pull import ReplenishOrder, TaktKanban
from .registry import rack_bindings, station as _station_spec
from .scene import (LPAL_TOP_DZ, PLANE_Z, PRESS_Q_TARGET, WP_HALF_XYZ,
                    LineCell, _quat_conj, _quat_mul)

CARRIER = "A02"                # 供料 AGV (暂存 → L-B)
CLEARER = "A03"                # 清型 AGV (出口 → 产出门架)
#: 携托动力学 = P0 已证参数域 (μ0.8 销盘, 低加速不打滑):
#: 携托段 0.15/0.10, 空车段 0.30/0.20
HAUL_SPEED, HAUL_ACC = 0.30, 0.20
CARRY_SPEED, CARRY_ACC = 0.15, 0.10
CARRY_SETTLE = 0.30           # 携托段 settle (伺服到位)
PIN_SPEED = 0.008


def _pin_seg(view, up: bool) -> dict:
    """销升/销降 (weld 联载下的携托越面/落座: 销行程 0.030 = 越面
    净空, 落座后销回零脱离)。"""
    return {"seg": (view.a["pin"],
                    lambda: Ramp(view.q("pin") + view.off["pin"],
                                 0.030 if up else 0.0, PIN_SPEED, 0.02),
                    view.off["pin"], 0.20)}
TRUNK_S_Y = 1.60               # A02 南干道
CLEAR_Y = 2.80                 # A03 北干道
FEED_RACK = "L-B1"             # ST-PRESS 主供料位
FAULT_K = 1                    # 负向注入生效拍 (第 2 拍)
FRAME_DT = 0.05                # 证据帧采样间隔

FAULTS = ("empty_rack_feed", "forge_op", "duplicate_feed",
          "unclamped_press", "takt_timeout_unreported",
          "wrong_station_feed")


# ---------------------------------------------------------------- 并行通道
class _Channels:
    """多通道并行段执行器 (同一物理步进推进各通道; 确定性单线程)。

    通道项: {"seg": (aid, build, off, settle), "probe": fn, "after": fn}
          | {"do": fn} 即时回调 | {"hold": s} 定长保持。
    """

    def __init__(self, cell):
        self.cell = cell
        self.chans: list[dict] = []
        self.max_pen = 0.0

    def add(self, items: list, delay: float = 0.0) -> None:
        if items:
            self.chans.append({"items": list(items), "i": 0, "cur": None,
                               "delay": delay, "done": False})

    def run(self, t0: float, max_s: float = 600.0) -> None:
        cell = self.cell
        sub = max(1, round(0.01 / cell.model.opt.timestep))
        t_end = t0 + max_s
        while cell.data.time < t_end:
            t = cell.data.time
            for ch in self.chans:
                if ch["done"] or t - t0 < ch["delay"]:
                    continue
                while ch["cur"] is None and ch["i"] < len(ch["items"]):
                    item = ch["items"][ch["i"]]
                    ch["i"] += 1
                    if "do" in item:
                        item["do"]()
                    elif "hold" in item:
                        ch["cur"] = {"kind": "hold", "t0": t,
                                     "dur": item["hold"]}
                    else:
                        aid, build, off, settle = item["seg"]
                        ch["cur"] = {"kind": "seg", "aid": aid,
                                     "ramp": build(), "off": off,
                                     "settle": settle, "t0": t,
                                     "probe": item.get("probe"),
                                     "after": item.get("after")}
                cur = ch["cur"]
                if cur is None:
                    ch["done"] = True
                    continue
                if cur["kind"] == "hold":
                    if t - cur["t0"] >= cur["dur"]:
                        ch["cur"] = None
                    continue
                te = t - cur["t0"]
                if te < cur["ramp"].duration:
                    cell.data.ctrl[cur["aid"]] = cur["ramp"].at(te) \
                        - cur["off"]
                else:
                    cell.data.ctrl[cur["aid"]] = cur["ramp"].target \
                        - cur["off"]
                    if te >= cur["ramp"].duration + cur["settle"]:
                        if cur["after"] is not None:
                            after, cur["after"] = cur["after"], None
                            after()
                        ch["cur"] = None
                if cur and cur.get("probe") is not None:
                    cur["probe"](t)
            self.max_pen = max(self.max_pen, cell.max_penetration())
            cell.step(sub)
            if all(ch["done"] for ch in self.chans):
                return


def _advance(cell, t_target: float) -> None:
    while cell.data.time < t_target:
        cell.step(50)


def _seg(view, axis: str, target: float, *, speed: float = HAUL_SPEED,
         accel: float = HAUL_ACC, settle: float = 0.15,
         carry: bool = False, probe=None) -> dict:
    if carry:
        speed, accel, settle = CARRY_SPEED, CARRY_ACC, CARRY_SETTLE
    out = {"seg": (*view.seg(axis, target, speed, accel), settle)}
    if probe is not None:
        out["probe"] = probe
    return out


def _wait_static(cell, bid: int, *, tol_mps: float = 5e-3,
                 timeout_s: float = 2.0) -> None:
    """等刚体物理静止 (落座稳定窗; P0 同制式, 上界受监控)。``bid``:
    自由铰刚体 id。"""
    m = cell.model
    jadr = m.jnt_dofadr[m.body_jntadr[bid]]
    t_end = cell.data.time + timeout_s
    while cell.data.time < t_end:
        if float(np.linalg.norm(cell.data.qvel[jadr:jadr + 3])) < tol_mps:
            return
        cell.step(10)


class _CarrySlave:
    """携托 mocap 从动 (P0'.5 携托联动修复; 与 BerthTruck 同一抽象:
    「mocap 规格下货物锁止」)。

    拾取确认 (grip weld 激活) 后捕获「携托偏移」(底盘局部系 relpos/
    relquat, 与 clamp/grip weld 同配方; 货物 = 托盘 + 坐顶工件 刚性
    整体), 随即失活 weld、切换从动: 每步将货物全体 6-DOF 位姿/速度
    逐步拷贝 = 载体位形 + 偏移 (trucks.py 的 mocap 驱动同源 —— mocap
    体无速度场, 位姿即受控量)。weld ∥ 底盘位置伺服 (kp500/30N 封顶)
    ∥ 销盘 noslip=20 三方对抗、多体不收敛的根因, 因货物位姿不再经
    由约束求解而消解; 工件须同列从动 (运动学位姿驱动下接触摩擦不携
    运 —— 最小模型实测, 与 weld 约束携带是两类机制)。落托确认后恢
    复动力学体 (从动速度清零 + 落座稳定窗), 货物重新参与接触/夹紧/
    冲压力路。
    """

    def __init__(self, cell, vid: str, pallet: str):
        self.cell, self.vid, self.pallet = cell, vid, pallet
        m, T = cell.model, mujoco.mjtObj
        self._chassis_bid = mujoco.mj_name2id(
            m, T.mjOBJ_BODY, f"{vid}_chassis")
        cj = m.body_jntadr[self._chassis_bid]         # x/y/yaw 连续 dof
        self._cjadr = int(m.jnt_dofadr[cj])
        self._bodies: list[tuple[int, int, int]] = []   # (bid, qadr, dadr)
        self._weld = cell.lift_welds[vid, pallet]
        self._off: list = []                            # (relpos 局部, relquat)
        self._zbias = 0.0                               # 从动抬升 (落座落差)

    @property
    def active(self) -> bool:
        return bool(self._off)

    def pickup(self, lift: float = 0.0) -> None:
        """拾取确认 → 捕获携托偏移 → 切 mocap 从动 (weld 失活)。

        ``lift``: 从动抬升 (m) —— 同高托举 (拾/落位支承顶等高) 时给出
        落座落差, 使落托为实接触承载 (z 逐字拷贝 = 支承面零穿透, 触
        点不生成)。"""
        cell = self.cell
        cell.grip(self.vid, self.pallet, True)
        assert cell.grip_active(self.vid, self.pallet)        # 拾取确认
        m, d = cell.model, cell.data
        b1 = self._chassis_bid
        r1 = d.xmat[b1].reshape(3, 3)
        names = [self.pallet] + [
            w for w, info in cell.workpieces.items()
            if info["pallet"] == self.pallet]
        for name in names:
            info = cell.pallets[name] if name == self.pallet \
                else cell.workpieces[name]
            bid = info.bid if name == self.pallet else info["bid"]
            pj = m.body_jntadr[bid]                   # 自由铰 (唯一)
            b2 = d.xpos[bid]
            self._bodies.append(
                (bid, int(m.jnt_qposadr[pj]), int(m.jnt_dofadr[pj])))
            self._off.append((r1.T @ (b2 - d.xpos[b1]),
                              _quat_mul(_quat_conj(d.xquat[b1]),
                                        d.xquat[bid])))
        d.eq_active[self._weld] = 0           # 从动接管, weld 退出力路
        self._zbias = float(lift)

    def slave(self, _t: float = 0.0) -> None:
        """每步从动钩子 (挂携托段 probe 槽): 位姿 ← 载体 + 偏移。"""
        if not self._off:
            return
        d = self.cell.data
        b1 = self._chassis_bid
        p1, r1, q1 = d.xpos[b1], d.xmat[b1].reshape(3, 3), d.xquat[b1]
        v = d.qvel[self._cjadr:self._cjadr + 3]       # vx, vy, ωz
        for (bid, qadr, dadr), (roff, rq) in zip(self._bodies, self._off):
            d.qpos[qadr:qadr + 3] = p1 + r1 @ roff
            d.qpos[qadr + 2] += self._zbias
            d.qpos[qadr + 3:qadr + 7] = _quat_mul(q1, rq)
            d.qvel[dadr:dadr + 3] = (v[0], v[1], 0.0)
            d.qvel[dadr + 3:dadr + 6] = (0.0, 0.0, v[2])

    def release(self) -> None:
        """落托确认 → 缓降至支承触点生成 → 恢复动力学体 + 稳定窗。

        0.05 m/s 匀速下插, 求解器顶起 (接触生成) 即停 —— 消除等高托
        举「零接触悬停」与 20mm 自由落座冲击, 落座无过盈无弹跳。"""
        bodies, self._off = self._bodies, []
        self.cell.grip(self.vid, self.pallet, False)
        d = self.cell.data
        self._zbias = 0.0
        step_dz = 5e-5
        _b0, qadr0, dadr0 = bodies[0]
        n_max = 3 * int(abs(float(d.qpos[qadr0 + 2]) - PLANE_Z) / step_dz) + 60
        for _ in range(n_max):
            z = float(d.qpos[qadr0 + 2])
            if z <= PLANE_Z + step_dz:
                break
            for _bid, qadr, _dadr in bodies:
                d.qpos[qadr + 2] -= step_dz   # 货物刚性整体同降
            self.cell.step(1)
            if float(d.qvel[dadr0 + 2]) > 1e-4:
                break                         # 支承触点生成 (求解器顶起)
        for _bid, _qadr, dadr in bodies:
            d.qvel[dadr:dadr + 6] = 0.0           # 从动速度残余清零
        for bid, _qadr, _dadr in bodies:
            _wait_static(self.cell, bid)


def _seed_nest(cell, pallet: str, wp: str) -> None:
    """期初物理落位: 首供托盘 + 工件直置 nest (台账「L-B1 交付」的
    物理位 = 工位 nest; rack 预载位仅场景播种)。传送后落座稳定。"""
    m, d = cell.model, cell.data
    nr = cell.nest_rect("ST-PRESS")
    x, y = (nr[0] + nr[1]) / 2, PLACE_Y
    bids = [cell.pallets[pallet].bid, cell.workpieces[wp]["bid"]]
    for name, bid, z in ((pallet, bids[0], PLANE_Z),
                         (wp, bids[1],
                          PLANE_Z + LPAL_TOP_DZ + WP_HALF_XYZ[2])):
        qadr = m.jnt_qposadr[m.body_jntadr[bid]]
        dadr = m.jnt_dofadr[m.body_jntadr[bid]]
        d.qpos[qadr + 0:qadr + 2] = (x, y)
        d.qpos[qadr + 2] = z
        d.qvel[dadr:dadr + 6] = 0.0
    for bid in bids:
        _wait_static(cell, bid, timeout_s=1.0)


IDLE_XY = (1.85, 2.60)       # A02 待命位 (西列; A03 待命 (1.90,3.02))
A03_IDLE_XY = (1.85, 3.02)   # A03 待命位 (产出行西端)
#: 拾取偏置 (AGV 停位点 − 槽位中心): 盘缘/凸台不触槽内支承, 且携托
#: 北行 x=2.17 列让位 L-B1 门腿柱 x=2.31 (底盘缘 2.29, 净 10mm)
PICK_OFFSET_X = 0.33
#: nest 投放 y (AGV 摆位): 托盘北面顶靠背止挡面 (止挡面 = 2.355,
#: 板半宽 0.027 → 摆位 2.328), 位置精度 = y 伺服 ±2mm < 容差 8mm
PLACE_Y = 2.328
#: A03 清型停位/投放补偿 (拾取停位 (2.43, 2.475) → 携托偏移
#: (+0.07, −0.147); 投放 = 槽位 + 偏移反解, 落点 = out_slot 精确位)
CLEAR_PICK_XY = (2.43, 2.475)
CLEAR_OFF = (0.07, 2.475 - PLACE_Y)
#: 从动抬升 (m): 凸台底悬挂 origin−0.030, 抬升须令凸台底越过多体路
#: 径上的条带/块顶 (0.075) —— 滑行越障而非零距穿缝; 上界 = 工件顶
#: 距冲压头静位 (0.130) 的净空 → 0.126
FEED_LIFT_M = 0.015            # 暂存 0.095 → 0.110 (凸台底 0.080)
CLEAR_LIFT_M = 0.035           # nest 0.075 → 0.110 (凸台底 0.080)
#: 清型出发延迟: 工位程序 ≈9s 收尾 → A03 先取成品 (程序无夹紧冲突);
#: A03 投出 nest 净空 ≈+11.8s → A02 延迟 5.5s (到位 ≈+13.9s) 后投
#: 下料 —— nest 单 LOW 位, 取空再投 (定长段 → 时序确定性)
CLEAR_DELAY_S = 6.0
FEED_DELAY_S = 5.5
#: A03 越顶搬运高度 (AGV y): 携托悬于产出行坐托 (y≈3.15) 北侧上空
#: 东行, 不跨已落位 (西行永不跨位语义 → 越顶等效)
CLEAR_OVER_Y = 3.30


def _feed_trip_items(cell, view, *, pallet, slot_xy, nest_y) -> tuple[list, dict]:
    """A02 供料环 (西列 x=2.17 / 投放列 x=2.50 双列制): 待命 → 南下暂
    存行 sy (空车: 销盘顶 0.064 < 支承顶 0.075, 净空 11mm) → 东至拾取
    位 x=2.17 (盘缘距槽内支承 60mm; 北行列让位 L-B1 门腿柱 2.31) →
    顶升焊拾取 (mocap 从动联载, _CarrySlave) → 北上 nest (托盘 x=2.50
    凸台穿条带孔位, 净空 1mm = P0 同链; 北行诸槽已取空) → 背止挡定位
    落座 (恢复动力学体 + 稳定窗, 落差 20mm) → 南归位。"""
    sy = slot_xy[1]
    slave = _CarrySlave(cell, CARRIER, pallet)
    items = [_seg(view, "y", sy),
             _seg(view, "x", 2.50 - PICK_OFFSET_X),
             {"do": lambda: slave.pickup(FEED_LIFT_M)},
             _seg(view, "x", 2.50 - PICK_OFFSET_X, carry=True,
                  probe=slave.slave),
             _seg(view, "y", PLACE_Y, carry=True, probe=slave.slave),
             {"do": slave.release},
             _seg(view, "y", IDLE_XY[1], carry=True)]
    hook = {"t": None}
    items[-1]["after"] = lambda: hook.__setitem__("t", cell.data.time)
    return items, hook


def _clear_trip_items(cell, view, *, pallet, out_xy) -> tuple[list, dict]:
    """A03 清型环: 待命 (1.85,3.02) → 东 x=2.43 → 南下 nest 北缘
    2.475 (盘北面 2.352, 停距 120mm) → 顶升焊拾取成品托 (mocap 从动
    联载, _CarrySlave) → 北上越顶 (AGV y=3.30, 携托悬于产出行坐托
    北侧上空) → 东延槽位 x (不跨已落位) → 南落产出行 (托盘 y=3.02
    双轨承托, 恢复动力学体 + 稳定窗) → 西归位。"""
    ox, oy = out_xy
    px_, py_ = CLEAR_PICK_XY
    slave = _CarrySlave(cell, CLEARER, pallet)
    items = [_seg(view, "x", px_),
             _seg(view, "y", py_),
             {"do": lambda: slave.pickup(CLEAR_LIFT_M)},
             _seg(view, "y", CLEAR_OVER_Y, carry=True, probe=slave.slave),
             _seg(view, "x", ox - CLEAR_OFF[0], carry=True,
                  probe=slave.slave),
             _seg(view, "y", oy + CLEAR_OFF[1], carry=True,
                  probe=slave.slave),
             {"do": slave.release},
             _seg(view, "x", A03_IDLE_XY[0])]
    hook = {"t": None}
    items[-1]["after"] = lambda: hook.__setitem__("t", cell.data.time)
    return items, hook


# ---------------------------------------------------------------- 工位程序
def _sampler(cell, h, wp, st: dict):
    """证据帧采样器 (每 FRAME_DT 一帧: 力/夹紧/工件位姿/头位)。"""
    last = {"t": -1e9}

    def probe(t: float) -> None:
        if t - last["t"] < FRAME_DT:
            return
        last["t"] = t
        if wp is not None:
            wpos = cell.wp_pose(wp)[0]
            px, py, pz = float(wpos[0]), float(wpos[1]), float(wpos[2])
        else:
            px = py = pz = 0.0
        m, d = cell.model, cell.data
        jadr = m.jnt_dofadr[m.body_jntadr[h.head_bid]]
        speed = float(np.linalg.norm(d.qvel[jadr:jadr + 1]))
        f = (cell.contact_force_n(h.station_id, wp)
             if wp is not None else 0.0)
        st["frames"].append(LineFrame(
            t=t, clamp_on=cell.clamp_active(st["pallet"]) if st["pallet"]
            else False, part_x=px, part_y=py, part_z=pz, speed=speed,
            head_z=h.head_bottom_z(cell), force_n=f))
        st["forces"].append((t, f))

    return probe


def _in_rect(xyz, rect) -> bool:
    return (rect[0] <= xyz[0] <= rect[1] and rect[2] <= xyz[1] <= rect[3])


def _program_items(cell, h, *, pallet, wp, spec, faults, k, st,
                   evidence_cb, phases) -> list:
    """工位程序通道: 推料 → 夹紧 → 冲压(力剖面采样) → 回程 → 出料 →
    回刀 → finalize (真值单 + 证据事件 + 台账提交, 即采即判)。"""
    unclamped = "unclamped_press" in faults and k == FAULT_K
    takt_hold = "takt_timeout_unreported" in faults and k == FAULT_K
    st["pallet"] = pallet
    st["wp"] = wp
    st["frames"], st["forces"] = [], []
    probe = _sampler(cell, h, wp, st)
    press0 = h.q_press(cell)

    def do_clamp():
        phases["station"] = "pressing"
        if not unclamped and pallet:
            cell.clamp(pallet, True)
        st["clamp_done"] = cell.clamp_active(pallet) if pallet else False

    def do_finish():
        if h.in_position(cell):
            st["plc"].finish(st["op_id"])
        st["t_press_bottom"] = cell.data.time
        if wp is not None:
            wpos = cell.wp_pose(wp)[0]
            # nest 坐托工件 z 窗: 托盘顶 0.085 + 半高 0.003 = 0.088
            # (冲压刚性压入 2mm → 0.086; 窗口留落座/压制余量)
            st["wp_in_nest"] = bool(_in_rect(wpos, h.geo.nest_rect())
                                    and PLANE_Z + LPAL_TOP_DZ - 4e-3
                                    <= float(wpos[2])
                                    <= PLANE_Z + LPAL_TOP_DZ + 6e-3)
        else:
            st["wp_in_nest"] = False
        st["clamp_at_press"] = (cell.clamp_active(pallet)
                                if pallet else False)

    def do_unclamp():
        phases["station"] = "eject"
        if pallet and cell.clamp_active(pallet):
            cell.clamp(pallet, False)

    def finalize():
        st["t_end"] = cell.data.time
        st["program_ran"] = True
        st["clamp_during"] = (all(f.clamp_on for f in st["frames"])
                              if st["frames"] else False)
        st["peak_n"] = max((f for _t, f in st["forces"]), default=0.0)
        st["mean_n"] = (sum(f for _t, f in st["forces"]) / len(st["forces"])
                        if st["forces"] else 0.0)
        st["force_ok"] = st["peak_n"] >= spec.force_min_n
        phases["station"], phases["part"] = "idle", None
        evidence_cb(st)

    items = [
        {"do": do_clamp},
        {"seg": (h.press_aid,
                 lambda: Ramp(press0, PRESS_Q_TARGET,
                              spec.press_speed_mps, 0.02), 0.0,
                 spec.force_window_s + 0.05), "probe": probe},
        {"do": do_finish},
    ]
    if takt_hold:
        # 超时节拍保持: 程序基线 ≈8s, 保持 1.15×takt 使 cycle_s 必然
        # 越窗 (账面仍声称 0.8×takt 窗内 → V_TAKT 可判)
        items.append({"hold": 1.15 * spec.takt_s})
    items += [
        {"seg": (h.press_aid,
                 lambda: Ramp(h.q_press(cell), 0.0, spec.press_speed_mps,
                              0.02), 0.0, 0.10), "probe": probe},
        {"do": do_unclamp},
        {"do": finalize},
    ]
    return items


def _forged_frames(cell, h, *, force_n: float = 18.0, n: int = 5) -> tuple:
    """伪造证据帧 (负向注入): 落在当前时刻之前, 支撑/位姿/夹紧全绿
    —— 伪造的是观测不是时钟 (同 P0 forge_commit 语义)。"""
    t0 = cell.data.time
    nest = h.geo.nest_rect()
    cx, cy = (nest[0] + nest[1]) / 2, (nest[2] + nest[3]) / 2
    return tuple(LineFrame(
        t=t0 - 0.20 + 0.05 * i, clamp_on=True, part_x=cx, part_y=cy,
        part_z=PLANE_Z + 0.020 + WP_HALF_XYZ[2], speed=1e-4,
        head_z=0.140, force_n=force_n) for i in range(n))




class _LiveState:
    """运行中状态快照源 (面板数据源; 蓝图 §13 面板 tab 产线)。

    ``snapshot()`` 为**纯读** (只取当前 cell.data/账目/证据缓存, 不步
    进仿真, 无副作用); 字段契约稳定 (面板 REST 轮询/WS 复用皆可):

    {
      "sim_t": float,
      "takt": {"takt_s": f, "k": 拍序, "next_tick_s": f,
               "stock": {"L-B1": {"L0": n, "L1": n}, ...}},
      "station": {"ST-PRESS": {
          "state": "idle|feeding|pressing|eject",
          "part": pallet_id|null, "plc_word": "BUSY|DONE|-",
          "last_event": {证据事件 dict (含时间三元组)}|null}},
      "carrier":  {"vid": "A02", "pos": [x, y], "task": str},
      "clearer":  {"vid": "A03", "pos": [x, y], "task": str},
      "evidence_recent": [≤20 条, 最新在后],
      "production": {"physical_done": n, "ledger_committed": m,
                     "mismatches": [op_id...] (运行时遥测口径;
                     权威对账以 dc_twin.scorer.line.score_line_run 为准)}
    }
    """

    def __init__(self, cell, takt, stock, plc, ledger, truth, events_out,
                 phases, orders, t0_s):
        self._cell = cell
        self._takt = takt
        self._stock = stock
        self._plc = plc
        self._ledger = ledger
        self._truth = truth
        self._events = events_out
        self._phases = phases
        self._orders = orders
        self._t0 = t0_s
        self._static = {"format": "mjcf-mirror-v1",
                        **cell.geom_manifest(),
                        "layout": cell.scene_static()}

    def snapshot(self) -> dict:
        d = self._cell.data
        m = self._cell.model
        def pos(vid):
            return [round(float(x), 4) for x in self._cell.veh[vid].xy()]

        def heading(vid):
            v = self._cell.veh[vid]
            return round(float(v.q("yaw") + v.off["yaw"]), 4)

        def xy_of(name):
            p = (self._cell.pallet_pose(name)[0] if name.startswith("pl")
                 else self._cell.wp_pose(name)[0])
            return [round(float(p[0]), 4), round(float(p[1]), 4),
                    round(float(p[2]), 4)]
        stock = {n: {"L0": len(v), "L1": 0} for n, v in self._stock.items()}
        k = len(self._truth["cycles"]) + len(self._truth["stoppage_ticks"])
        next_tick = (self._t0 + (k + 1) * self._takt) if self._orders else None
        part = self._phases.get("part")
        word = "-"
        if part is not None and self._plc.words.get(self._phases.get("op", "")):
            word = self._plc.words[self._phases["op"]]
        committed = sum(1 for e in self._ledger.events
                        if e["type"] == "OpCommitted"
                        and not e.get("duplicate"))
        physical = sum(1 for c in self._truth["cycles"] if c["force_ok"])
        mismatches = [c["op_id"] for c in self._truth["cycles"]
                      if not c["force_ok"]]
        last_ev = self._events[-1] if self._events else None
        h = self._cell.presses["ST-PRESS"]
        wps = [{"name": w, **dict(zip(("x", "y", "z"), xy_of(w))),
                "in_fixture": bool(
                    self._cell.geos["ST-PRESS"].nest_rect()[0]
                    <= xy_of(w)[0] <= self._cell.geos["ST-PRESS"].nest_rect()[1]
                    and self._cell.geos["ST-PRESS"].nest_rect()[2]
                    <= xy_of(w)[1] <= self._cell.geos["ST-PRESS"].nest_rect()[3])}
               for w in self._cell.workpieces]
        plts = [{"name": p, **dict(zip(("x", "y"), xy_of(p)[:2]))}
                for p in self._cell.pallets]
        return {
            "sim_t": round(float(d.time), 4),
            "takt": {"takt_s": self._takt, "k": k,
                     "next_tick_s": round(next_tick, 4)
                     if next_tick is not None else None,
                     "stock": stock},
            "station": {"ST-PRESS": {
                "state": self._phases.get("station", "idle"),
                "part": part, "plc_word": word,
                "slide_q": round(h.q_press(self._cell), 4),
                "plunger_z": round(h.head_bottom_z(self._cell), 4),
                "last_event": last_ev}},
            "carrier": {"vid": CARRIER, "pos": pos(CARRIER),
                        "heading": heading(CARRIER),
                        "task": self._phases.get("carrier", "idle")},
            "clearer": {"vid": CLEARER, "pos": pos(CLEARER),
                        "heading": heading(CLEARER),
                        "task": self._phases.get("clearer", "idle")},
            "workpieces": wps,
            "pallets": plts,
            "scene_static": self._static,
            "evidence_recent": list(self._events[-20:]),
            "production": {"physical_done": physical,
                           "ledger_committed": committed,
                           "mismatches": mismatches}}


_LATEST: list = []


def latest_state() -> "_LiveState | None":
    """最近一次 run_line 的活动状态 (无则 None; 面板进程内轮询用)。"""
    return _LATEST[-1] if _LATEST else None


# ---------------------------------------------------------------- 结果
@dataclass(frozen=True)
class LineRun:
    run_id: str
    out_dir: Path
    audit: dict
    state: "_LiveState | None" = None

    @property
    def truth(self) -> dict:
        return json.loads((self.out_dir / "truth.json").read_text())

    @property
    def ledger_doc(self) -> dict:
        return json.loads((self.out_dir / "line_ledger.json").read_text())

    @property
    def evidence_events(self) -> list[dict]:
        return json.loads(
            (self.out_dir / "evidence_events.json").read_text())

    @property
    def cycles(self) -> list[dict]:
        return self.truth["cycles"]


# ---------------------------------------------------------------- 主幕
def run_line(*, faults=(), out_dir=None, horizon_s: float = 1800.0,
             takt_s: float | None = None, t0_s: float = 12.0,
             line_pallets: int = 32, run_id: str | None = None,
             state_path=None, state_interval_s: float = 5.0) -> LineRun:
    """``state_path``: 状态发布 JSON 路径 (显式参数 → 环境变量
    DC_TWIN_LINE_STATE_PATH → publisher.DEFAULT_LINE_STATE_PATH;
    False 关闭发布, 测试隔离用); ``state_interval_s`` 发布节拍
    (仿真秒)。"""
    faults = frozenset(faults)
    unknown = faults - set(FAULTS)
    if unknown:
        raise ValueError(f"未知注入: {sorted(unknown)}; 可用 {FAULTS}")
    run_id = run_id or f"LINE-{uuid.uuid4().hex[:8]}"
    out_dir = Path(out_dir) if out_dir else Path("reports") / "line" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # 场景载荷注册 (core 可扩展点) + 布景
    _racks.register()
    _ev.register()
    spec = _station_spec("ST-PRESS")
    takt = float(takt_s) if takt_s else spec.takt_s
    lay = MatrixLayout(seed_pallets=0)
    lay.p["park"] = {**lay.p["park"], CARRIER: (*IDLE_XY, 0.0),
                     CLEARER: (*A03_IDLE_XY, 0.0)}
    cell = LineCell(layout=lay, line_pallets=line_pallets,
                    preload_racks=(FEED_RACK, "L-B2"),
                    agvs=(CARRIER, CLEARER), forklifts=(), seed_slots=[])
    cell.warmup(0.5)
    feed_v, clear_v = cell.veh[CARRIER], cell.veh[CLEARER]
    h = cell.presses["ST-PRESS"]
    _seed_nest(cell, "pl_000", "wp_000")   # 期初: 首供托盘物理在位 (L-B1)

    # takt 看板 → DemandEngine pull 源 (core 可扩展点; 引擎只管归并)
    kanban = TaktKanban(station_id="ST-PRESS", takt_s=takt, t0_s=t0_s,
                        rack_node=FEED_RACK)
    engine = DemandEngine(initial_pallets=64, arrival_sources=(kanban,))
    orders = [o for o in engine.generate_until(horizon_s)
              if isinstance(o, ReplenishOrder)]

    ledger = LineLedger()
    plc = LinePlc()
    plan = cell._pallet_plan()
    staged = [name for name, _x, _y in plan[len(cell._preload_racks):]]
    stock: dict[str, list[str]] = {n: [] for n in cell.racks}
    stock[FEED_RACK].append("pl_000")
    stock["L-B2"].append("pl_001")
    for node, p in ((FEED_RACK, "pl_000"), ("L-B2", "pl_001")):
        ledger.record_delivery(p, node, t_s=cell.data.time,
                               station_bound=rack_bindings().get(node))
    staged_idx = 0
    wp_of = {d["pallet"]: w for w, d in cell.workpieces.items()}

    truth: dict = {
        "run_id": run_id, "core_version": core_version(),
        "clock_domain": "sim_t", "horizon_s": horizon_s, "takt_s": takt,
        "t0_s": t0_s,
        "stations": {"ST-PRESS": {
            "rack_primary": FEED_RACK, "takt_s": takt,
            "force_min_n": spec.force_min_n,
            "nest_rect": list(h.geo.nest_rect()),
            "exit_rect": list(h.geo.exit_rect()),
            "rack_bindings": rack_bindings()}},
        "deliveries": [], "cycles": [], "stock_timeline": [],
        "stoppage_ticks": [], "finished": [], "exit_residual": None}
    for node, p in ((FEED_RACK, "pl_000"), ("L-B2", "pl_001")):
        truth["deliveries"].append({
            "pallet_id": p, "order_id": "PRELOAD", "rack_node": node,
            "rack_node_target": node, "order_station": "ST-PRESS",
            "station_bound": rack_bindings().get(node),
            "t_s": round(cell.data.time, 4)})
    events_out: list[dict] = []
    seq = {"n": 0}
    # nest 单 LOW 位占用账 (种子托盘即首件在制): haul_k = 本拍派出清
    # 型的托盘; nest["hold"] = nest 内在制/待取托盘 (供料投放的门)
    haul_k: str | None = None
    nest: dict = {"hold": "pl_000"}
    phases: dict = {"station": "idle", "carrier": "idle", "clearer": "idle",
                    "part": None, "op": None}
    live = _LiveState(cell, takt, stock, plc, ledger, truth, events_out,
                      phases, orders, t0_s)
    _LATEST.append(live)
    pub = None                                   # 面板状态发布器 (§15)
    sp = resolve_state_path(state_path)
    if sp is not None:
        pub = LineStatePublisher(sp, live, run_id=run_id,
                                 interval_s=state_interval_s)
        pub.publish()                            # 开机即有在线快照

    def _eid() -> str:
        seq["n"] += 1
        return f"{run_id}-EVT-{seq['n']:03d}"

    for k, order in enumerate(orders):
        _advance(cell, order.t_s)
        t_tick = cell.data.time
        truth["stock_timeline"].append(
            {"t": round(t_tick, 4), "k": k,
             "stock": {n: len(v) for n, v in stock.items()}})
        forge_np = k == FAULT_K and "forge_op" in faults
        if forge_np:
            pallet = None                 # 伪造工单不触料 (料仍在架)
        else:
            pallet = stock[FEED_RACK].pop(0) \
                if stock[FEED_RACK] else None
        wp = wp_of.get(pallet) if pallet else None
        forged_empty = k == FAULT_K and "empty_rack_feed" in faults
        phantom = f"pl_XXX{k}"
        st: dict = {"op_id": f"OPL-{k + 1:03d}", "plc": plc,
                    "t_start": t_tick}

        # ---- 证据/提交回调 (程序末即采即判; C1 TTL 内闭合)
        def evidence_cb(st=st, k=k, pallet=pallet, wp=wp,
                        forged_empty=forged_empty, t_tick=t_tick):
            op_id = st["op_id"]
            use_forge = forged_empty and pallet is None
            led_pallet = phantom if use_forge else pallet
            if use_forge:
                # 幽灵补料 (谎称已到架) —— 真值侧无此交付
                ledger.record_delivery(phantom, FEED_RACK, t_s=t_tick)
            ledger.prepare_op(op_id, pallet_id=led_pallet,
                              workpiece_id=wp_of.get(led_pallet,
                                                     f"wp_XXX{k}"),
                              station_id="ST-PRESS", rack_node=FEED_RACK,
                              t_s=t_tick)
            ledger.snapshot(t_s=t_tick)
            lie = ("takt_timeout_unreported" in faults and k == FAULT_K)
            t_end_claim = (st["t_start"] + 0.8 * takt) if lie \
                else st["t_end"]
            frames = (_forged_frames(cell, h) if use_forge
                      else tuple(st["frames"]))
            feed_frames = frames[:3] or _forged_frames(cell, h, n=3)
            cell.step(20)
            t_ing = cell.data.time
            cell.step(20)
            t_dec = cell.data.time
            feed_ev = _ev.build_feed_event(
                event_id=_eid(), operation_id=op_id,
                pallet_id=led_pallet,
                workpiece_id=wp_of.get(led_pallet, f"wp_XXX{k}"),
                station_id="ST-PRESS", rack_node=FEED_RACK,
                frames=feed_frames, t_ingest=t_ing, t_decide=t_dec,
                verdict_failures=())
            op_ev = _ev.build_op_event(
                event_id=_eid(), operation_id=op_id,
                pallet_id=led_pallet,
                workpiece_id=wp_of.get(led_pallet, f"wp_XXX{k}"),
                station_id="ST-PRESS", frames=frames, t_ingest=t_ing,
                t_decide=t_dec, force_peak_n=st["peak_n"],
                force_mean_n=st["mean_n"],
                force_min_n=spec.force_min_n, takt_s=takt,
                t_start=st["t_start"], t_end=t_end_claim,
                clamp_confirmed=True, verdict_failures=())
            events_out.extend([json.loads(feed_ev.to_json()),
                               json.loads(op_ev.to_json())])
            ledger.commit_op(op_id, t_s=t_dec,
                             evidence_ref=op_ev.event_id,
                             peak_n=op_ev.force["peak_n"],
                             cycle_s=op_ev.takt["t_end"]
                             - op_ev.takt["t_start"], in_window=True)
            if "duplicate_feed" in faults and k == FAULT_K:
                ledger.commit_op(op_id, t_s=t_dec + 0.1,
                                 evidence_ref=op_ev.event_id,
                                 peak_n=op_ev.force["peak_n"],
                                 cycle_s=op_ev.takt["t_end"]
                                 - op_ev.takt["t_start"],
                                 in_window=True, _duplicate=True)
            ledger.ack_op(op_id, t_s=t_dec + 0.2)
            ledger.snapshot(t_s=t_dec + 0.2)
            truth["cycles"].append({
                "op_id": op_id, "k": k, "pallet_id": pallet,
                "workpiece_id": wp, "rack_node": FEED_RACK,
                "station_id": "ST-PRESS",
                "t_start": round(st["t_start"], 4),
                "t_feed_end": round(st.get("t_press_bottom",
                                           st["t_end"]), 4),
                "t_end": round(st["t_end"], 4),
                "cycle_s": round(st["t_end"] - st["t_start"], 4),
                "peak_n": round(st["peak_n"], 4),
                "mean_n": round(st["mean_n"], 4),
                "force_ok": st["force_ok"],
                "clamp_active_during": st["clamp_at_press"],
                "wp_in_nest": st["wp_in_nest"],
                "fixture_empty": not st["wp_in_nest"],
                "plc_done": plc.ack(op_id),
                "force_samples": [[round(t, 4), round(f, 3)]
                                  for t, f in st["forces"][::4]]})

        chans = _Channels(cell)
        hold = (1.15 * spec.takt_s
                if "takt_timeout_unreported" in faults and k == FAULT_K
                else 0.0)
        # ---- 工位通道 (有料 / 空料伪造程序)
        if pallet is not None or forged_empty:
            plc.issue(st["op_id"])
            phases["station"] = "pressing"
            phases["part"], phases["op"] = pallet, st["op_id"]
            chans.add(_program_items(cell, h, pallet=pallet, wp=wp,
                                     spec=spec, faults=faults, k=k, st=st,
                                     evidence_cb=evidence_cb,
                                     phases=phases))
        elif forge_np:
            pass                          # 伪造: 无物理程序, 仅账面/证据
        else:
            truth["stoppage_ticks"].append(k)
        # ---- A03 清型通道 (本拍成品即取: 延迟越过程序收尾, 同拍腾空
        # nest → 供料投放不叠载)
        if pallet is not None:
            haul_k = pallet
            nest_hold = None              # 派出即腾空 (时序由延迟保证)
            slot_j = len(truth["finished"])
            items, hook = _clear_trip_items(
                cell, clear_v, pallet=haul_k,
                out_xy=cell.out_slot(slot_j))

            def _cleared(hook=hook, pal_c=haul_k, wp_c=wp, slot_j=slot_j):
                t = hook["t"] or cell.data.time
                truth["finished"].append({
                    "pallet_id": pal_c, "workpiece_id": wp_c,
                    "t_exit": round(t, 4),
                    "out_slot": list(cell.out_slot(slot_j))})
            items[-1]["after"] = _cleared
            items.insert(0, {"do": lambda: phases.__setitem__(
                "clearer", f"clear:{haul_k}→out{slot_j}")})
            items.insert(len(items) - 1, {"do": lambda: phases.__setitem__(
                "clearer", "idle")})
            chans.add(items, delay=CLEAR_DELAY_S + hold)
        # ---- A02 供料通道 (空料注入: 跳过首拍补料; nest 待压/待取
        # 占用时持料等下一拍 —— 单 LOW 位不叠载)
        skip_feed = k == 0 and "empty_rack_feed" in faults
        nest_free = nest["hold"] is None or nest["hold"] == haul_k
        p_next = staged[staged_idx] if staged_idx < len(staged) else None
        if p_next is not None and not skip_feed and nest_free:
            dest = ("L-B3" if k == FAULT_K
                    and "wrong_station_feed" in faults else FEED_RACK)
            items, hook = _feed_trip_items(
                cell, feed_v, pallet=p_next,
                slot_xy=cell.staging_slot(staged_idx),
                nest_y=cell.geos["ST-PRESS"].nest_y)
            staged_idx += 1
            pal, ordf, dst = p_next, order, dest

            def _delivered(hook=hook, pal=pal, dst=dst, ordf=ordf):
                t = hook["t"] or cell.data.time
                nest["hold"] = pal         # 投放入位
                ledger.record_delivery(pal, dst, t_s=t,
                                       station_bound=rack_bindings().get(dst))
                stock[dst].append(pal)
                truth["deliveries"].append({
                    "pallet_id": pal, "order_id": ordf.order_id,
                    "rack_node": dst, "rack_node_target": ordf.rack_node,
                    "order_station": ordf.station_id,
                    "station_bound": rack_bindings().get(dst),
                    "t_s": round(t, 4)})
            items[-1]["after"] = _delivered
            items.insert(0, {"do": lambda: phases.__setitem__(
                "carrier", f"feed:{pal}→{dst}")})
            items.insert(len(items) - 1, {"do": lambda: phases.__setitem__(
                "carrier", "idle")})
            chans.add(items, delay=FEED_DELAY_S + hold)
        chans.run(t0=t_tick)
        if pub is not None:
            pub.maybe_publish(cell.data.time)

        # ---- 无程序伪造 (forge_op): 账面 + 证据全伪造
        if forge_np:
            op_id = st["op_id"]
            ledger.record_delivery(phantom, FEED_RACK, t_s=t_tick)
            ledger.prepare_op(op_id, pallet_id=phantom,
                              workpiece_id=f"wp_XXX{k}",
                              station_id="ST-PRESS", rack_node=FEED_RACK,
                              t_s=t_tick)
            ledger.snapshot(t_s=t_tick)
            frames = _forged_frames(cell, h)
            feed_frames = frames[:3]
            cell.step(20)
            t_ing = cell.data.time
            cell.step(20)
            t_dec = cell.data.time
            feed_ev = _ev.build_feed_event(
                event_id=_eid(), operation_id=op_id, pallet_id=phantom,
                workpiece_id=f"wp_XXX{k}", station_id="ST-PRESS",
                rack_node=FEED_RACK, frames=feed_frames, t_ingest=t_ing,
                t_decide=t_dec, verdict_failures=())
            op_ev = _ev.build_op_event(
                event_id=_eid(), operation_id=op_id, pallet_id=phantom,
                workpiece_id=f"wp_XXX{k}", station_id="ST-PRESS",
                frames=frames, t_ingest=t_ing, t_decide=t_dec,
                force_peak_n=18.2, force_mean_n=12.0,
                force_min_n=spec.force_min_n, takt_s=takt,
                t_start=t_tick, t_end=t_tick + 0.8 * takt,
                clamp_confirmed=True, verdict_failures=())
            events_out.extend([json.loads(feed_ev.to_json()),
                               json.loads(op_ev.to_json())])
            ledger.commit_op(op_id, t_s=t_dec, evidence_ref=op_ev.event_id,
                             peak_n=18.2, cycle_s=0.8 * takt,
                             in_window=True)
            ledger.ack_op(op_id, t_s=t_dec + 0.2)
            ledger.snapshot(t_s=t_dec + 0.2)

    _advance(cell, cell.data.time + 0.5)
    if nest["hold"] is not None:
        # 残留: nest 内在制未取托盘 (负向注入路径的悬挂在制品)
        truth["exit_residual"] = {"pallet_id": nest["hold"],
                                  "workpiece_id": wp_of.get(nest["hold"]),
                                  "t": round(cell.data.time, 4)}
    truth["pallets_final"] = {
        p: [round(float(v), 5) for v in cell.pallet_pose(p)[0]]
        for p in cell.pallets}
    truth["workpieces_final"] = {
        w: [round(float(v), 5) for v in cell.wp_pose(w)[0]]
        for w in cell.workpieces}
    truth["audit"] = {"max_pen_m": round(chans.max_pen, 6)}

    (out_dir / "truth.json").write_text(
        json.dumps(truth, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "line_ledger.json").write_text(
        json.dumps(ledger.export(run_id=run_id), ensure_ascii=False,
                   indent=1), encoding="utf-8")
    (out_dir / "evidence_events.json").write_text(
        json.dumps(events_out, ensure_ascii=False, indent=1),
        encoding="utf-8")
    if pub is not None:
        pub.publish()                            # 收官终态 (面板定格)
    return LineRun(run_id=run_id, out_dir=out_dir,
                   audit={"max_pen_m": truth["audit"]["max_pen_m"],
                          "cycles": len(truth["cycles"]),
                          "stoppage_ticks": list(truth["stoppage_ticks"])},
                   state=live)
