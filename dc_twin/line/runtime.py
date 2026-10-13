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

AGV 车道分离 (无平面交叉): 供料 A02 列 x=2.10/2.31/2.50; 清型
A03 线 x=1.90/2.50 行 y=3.02。结构立柱距 AGV 停位点 ≥0.16。

已知边界 (P0'.5): 顶升焊 (lift_*) 在隔离探针中 Z 向拾取与最小模型
拖曳均验证通过, 但多体场景下 (位置伺服保持的底盘 + noslip=20) 携
托 X/Y 拖曳未联动 —— 托盘滞留拾取位, 投放落点偏移, 工位接触力为
零; 根因定位到 weld 约束与底盘位置伺服 (30N 封顶) 的力路交互。
替代闭环 (已验证): 台账/证据/评分器以「投料=nest 账面落座」记账,
物理拾放联动留 P0'.5。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

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
from .scene import (PLANE_Z, PRESS_Q_TARGET, WP_HALF_XYZ, LineCell)

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
#: 清型出发延迟 = 出料推完 (程序 ≈22.5s) 前先行 21.8s, 到出口
#: ≈22.9s (余量 ~0.6s; 定长段 → 时序确定性)
CLEAR_DELAY_S = 21.8
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
         carry: bool = False) -> dict:
    if carry:
        speed, accel, settle = CARRY_SPEED, CARRY_ACC, CARRY_SETTLE
    return {"seg": (*view.seg(axis, target, speed, accel), settle)}


IDLE_XY = (1.85, 2.60)       # A02 待命位 (西列; A03 待命 (1.90,3.02))
A03_IDLE_XY = (1.85, 3.02)   # A03 待命位 (产出行西端)
#: 拾取偏置 (AGV 停位点 − 槽位中心): 盘缘/凸台不触槽内支承
PICK_OFFSET_X = 0.20
#: nest 投放 y (AGV 摆位): 托盘北面顶靠背止挡面 (止挡面 = 2.355,
#: 板半宽 0.027 → 摆位 2.328), 位置精度 = y 伺服 ±2mm < 容差 8mm
PLACE_Y = 2.328


def _feed_trip_items(cell, view, *, pallet, slot_xy, nest_y) -> tuple[list, dict]:
    """A02 供料环 (西列 x=2.10 / 投放列 x=2.50 双列制): 待命 → 南下暂
    存行 sy (空车: 销盘顶 0.064 < 支承顶 0.075, 净空 11mm) → 东 0.20m
    拾取位 (盘缘距槽内支承 60mm) → 顶升焊拾取 (刚性联载) → 西退 (凸台
    穿槽对间隙 12.5mm) → 北上 nest (携托板底 0.076 > 北行诸结构顶
    0.075, 净空 1mm = P0 同链; 北行诸槽已取空) → 背止挡定位落座 →
    南归位。"""
    sy = slot_xy[1]
    items = [_seg(view, "y", sy),
             _seg(view, "x", 2.30),
             {"do": lambda: cell.grip(CARRIER, pallet, True)},
             _seg(view, "x", 2.50 - PICK_OFFSET_X, carry=True),
             _seg(view, "y", PLACE_Y, carry=True),
             {"do": lambda: cell.grip(CARRIER, pallet, False)},
             _seg(view, "y", IDLE_XY[1], carry=True)]
    hook = {"t": None}
    items[-1]["after"] = lambda: hook.__setitem__("t", cell.data.time)
    return items, hook


def _clear_trip_items(cell, view, *, pallet, out_xy) -> tuple[list, dict]:
    """A03 清型环: 待命 (1.90,3.02) → 东 x=2.50 → 南下 nest 北缘
    2.475 (盘北面 2.352, 停距 120mm) → 顶升焊拾取成品托 → 北归行 3.02
    (携托板底 0.076 > 行顶 0.075, 净空 1mm) → 东延槽位落托 (东满优
    先, 西行永不跨已落位) → 西归位。"""
    ox, oy = out_xy
    items = [_seg(view, "x", 2.50),
             _seg(view, "y", 2.475),
             {"do": lambda: cell.grip(CLEARER, pallet, True)},
             _seg(view, "y", oy, carry=True),
             _seg(view, "x", ox, carry=True),
             {"do": lambda: cell.grip(CLEARER, pallet, False)},
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
            st["wp_in_nest"] = bool(_in_rect(wpos, h.geo.nest_rect())
                                    and 0.10 <= float(wpos[2]) <= 0.16)
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
        items.append({"hold": 0.55 * spec.takt_s})
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
        ledger.record_delivery(p, node, t_s=cell.data.time)   # 期初建账
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
    exit_pallet: tuple[str, str] | None = None   # (pallet, wp) 待清型
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
        # ---- A02 供料通道 (空料注入: 跳过首拍补料)
        skip_feed = k == 0 and "empty_rack_feed" in faults
        p_next = staged[staged_idx] if staged_idx < len(staged) else None
        if p_next is not None and not skip_feed:
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
                ledger.record_delivery(pal, dst, t_s=t)
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
            chans.add(items)
        # ---- A03 清型通道 (上一拍出料成品)
        if exit_pallet is not None:
            pal_c, wp_c = exit_pallet
            slot_j = len(truth["finished"])
            items, hook = _clear_trip_items(
                cell, clear_v, pallet=pal_c,
                out_xy=cell.out_slot(slot_j))

            def _cleared(hook=hook, pal_c=pal_c, wp_c=wp_c, slot_j=slot_j):
                t = hook["t"] or cell.data.time
                truth["finished"].append({
                    "pallet_id": pal_c, "workpiece_id": wp_c,
                    "t_exit": round(t, 4),
                    "out_slot": list(cell.out_slot(slot_j))})
            items[-1]["after"] = _cleared
            items.insert(0, {"do": lambda: phases.__setitem__(
                "clearer", f"clear:{pal_c}→out{slot_j}")})
            items.insert(len(items) - 1, {"do": lambda: phases.__setitem__(
                "clearer", "idle")})
            chans.add(items, delay=CLEAR_DELAY_S)
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
        if st.get("program_ran") and pallet is not None:
            exit_pallet = (pallet, wp)

    _advance(cell, cell.data.time + 0.5)
    if exit_pallet is not None:
        truth["exit_residual"] = {"pallet_id": exit_pallet[0],
                                  "workpiece_id": exit_pallet[1],
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
