"""产线证据事件 (蓝图 §13 两条证据 + C1 时间模型)。

- FEED 上料事件: 工件入夹具到位 + 夹紧确认 (L2a 几何级);
- OP_COMPLETE 工序完成事件: 冲压力剖面达标 + 节拍窗内 (力证据,
  MuJoCo 强项; §14 叙事: 压盖/冲压类工位的咬合+压线力判定力学抽象)。

运行时证据帧字段 = t_capture / t_ingest / t_decide (同时钟域 sim_t,
单调性在构造期强制); 真值时间不进运行时证据 —— 只存在于独立评分器
的 truth 导出。事件类型经 core register_event_kind 注册。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from services.world.evidence import (EVENT_KINDS, VERDICT_TTL_S,
                                     register_event_kind)

FEED_SCHEMA = "dc_twin.line.feed_event/1"
OP_SCHEMA = "dc_twin.line.op_event/1"
CLOCK_DOMAIN = "sim_t"
KIND_FEED = "FEED"
KIND_OP = "OP_COMPLETE"

_REGISTERED = False


def register() -> None:
    """注册产线事件类型进 core (幂等)。"""
    global _REGISTERED
    if not _REGISTERED:
        if KIND_FEED not in EVENT_KINDS:
            register_event_kind(KIND_FEED, "上料 (工件入夹具+夹紧, L2a)")
        if KIND_OP not in EVENT_KINDS:
            register_event_kind(KIND_OP, "工序完成 (力剖面+节拍窗)")
        _REGISTERED = True


@dataclass(frozen=True)
class LineFrame:
    """单帧工位观测 (纯数值, 可 JSON 序列化; 同时钟 sim_t)。"""

    t: float
    clamp_on: bool              # 夹紧确认位 (eq_active)
    part_x: float               # 工件位姿
    part_y: float
    part_z: float
    speed: float                # 稳定判据 |qvel|
    head_z: float | None = None     # 冲压头底面 z (工序帧)
    force_n: float | None = None    # 头↔工件接触法向力 (N)

    def to_dict(self) -> dict:
        return {"t": round(self.t, 6), "clamp_on": bool(self.clamp_on),
                "part_x": round(self.part_x, 6),
                "part_y": round(self.part_y, 6),
                "part_z": round(self.part_z, 6),
                "speed": round(self.speed, 8),
                "head_z": None if self.head_z is None else round(self.head_z, 6),
                "force_n": None if self.force_n is None else round(self.force_n, 4)}


@dataclass(frozen=True)
class _BaseLineEvent:
    """共享: 时间三元组 + verdict 访问 + JSON 往返。"""

    def to_json(self) -> str:
        d = dict(self.__dict__)
        d["estimated_event_time"] = (list(self.estimated_event_time)
                                     if self.estimated_event_time else None)
        d["frames"] = [f.to_dict() if isinstance(f, LineFrame) else f
                       for f in self.frames]
        return json.dumps(d, ensure_ascii=False)

    @property
    def passed(self) -> bool:
        return bool(self.verdict.get("passed"))

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(self.verdict.get("failures", ()))


@dataclass(frozen=True)
class FeedEvent(_BaseLineEvent):
    """一次上料的运行时证据事件 (纯数据)。"""

    event_id: str
    schema: str
    kind: str
    clock_domain: str
    operation_id: str
    pallet_id: str
    workpiece_id: str
    station_id: str
    rack_node: str
    t_capture: float
    t_ingest: float
    t_decide: float
    estimated_event_time: tuple[float, float] | None
    verdict: dict
    frames: tuple

    @classmethod
    def from_json(cls, s: str) -> "FeedEvent":
        d = json.loads(s)
        eet = d.get("estimated_event_time")
        return cls(**{**d, "estimated_event_time":
                      tuple(eet) if eet else None,
                      "frames": tuple(d["frames"])})


@dataclass(frozen=True)
class OpCompleteEvent(_BaseLineEvent):
    """一次工序完成的运行时证据事件 (力剖面 + 节拍窗)。"""

    event_id: str
    schema: str
    kind: str
    clock_domain: str
    operation_id: str
    pallet_id: str
    workpiece_id: str
    station_id: str
    t_capture: float
    t_ingest: float
    t_decide: float
    force: dict                 # {peak_n, mean_n, min_required_n, samples}
    takt: dict                  # {takt_s, t_start, t_end, in_window}
    clamp_confirmed: bool
    estimated_event_time: tuple[float, float] | None
    verdict: dict
    frames: tuple

    @classmethod
    def from_json(cls, s: str) -> "OpCompleteEvent":
        d = json.loads(s)
        eet = d.get("estimated_event_time")
        return cls(**{**d, "estimated_event_time":
                      tuple(eet) if eet else None,
                      "frames": tuple(d["frames"])})


def _check_triple(frames, t_ingest, t_decide) -> float:
    """时间三元组单调性 + 帧序 + 证据过期 (C1; 返回 failures 追加)。"""
    register()
    t_capture = float(frames[-1].t) if frames else float("nan")
    if not (0.0 <= t_capture <= t_ingest <= t_decide):
        raise ValueError(
            f"时间三元组违反单调性 (sim_t): t_capture={t_capture} "
            f"t_ingest={t_ingest} t_decide={t_decide}")
    if frames:
        caps = [f.t for f in frames]
        if caps != sorted(caps):
            raise ValueError("帧序列时间倒序")
    return t_capture


def _expired(frames, t_decide) -> str | None:
    if frames and t_decide > frames[-1].t + VERDICT_TTL_S:
        return (f"证据过期 (t_decide {t_decide:.3f} > valid_until "
                f"{frames[-1].t + VERDICT_TTL_S:.3f})")
    return None


def build_feed_event(*, event_id: str, operation_id: str, pallet_id: str,
                     workpiece_id: str, station_id: str, rack_node: str,
                     frames: tuple, t_ingest: float, t_decide: float,
                     verdict_failures: tuple[str, ...],
                     estimated_event_time: tuple[float, float] | None = None,
                     ) -> FeedEvent:
    """上料事件唯一构造入口 (单调性 + 过期在此强制)。"""
    t_capture = _check_triple(frames, t_ingest, t_decide)
    failures = list(verdict_failures)
    exp = _expired(frames, t_decide)
    if exp:
        failures.append(exp)
    last = frames[-1] if frames else None
    verdict = {"passed": not failures, "failures": failures,
               "clamp_on": bool(last.clamp_on) if last else False,
               "stable_window_s": (round(frames[-1].t - frames[0].t, 3)
                                   if len(frames) > 1 else 0.0),
               "frames": len(frames)}
    return FeedEvent(
        event_id=event_id, schema=FEED_SCHEMA, kind=KIND_FEED,
        clock_domain=CLOCK_DOMAIN, operation_id=operation_id,
        pallet_id=pallet_id, workpiece_id=workpiece_id,
        station_id=station_id, rack_node=rack_node,
        t_capture=t_capture, t_ingest=float(t_ingest),
        t_decide=float(t_decide),
        estimated_event_time=(tuple(estimated_event_time)
                              if estimated_event_time else None),
        verdict=verdict,
        frames=tuple(f if isinstance(f, dict) else f.to_dict()
                     for f in frames))


def build_op_event(*, event_id: str, operation_id: str, pallet_id: str,
                   workpiece_id: str, station_id: str,
                   frames: tuple, t_ingest: float, t_decide: float,
                   force_peak_n: float, force_mean_n: float,
                   force_min_n: float, takt_s: float, t_start: float,
                   t_end: float, clamp_confirmed: bool,
                   verdict_failures: tuple[str, ...],
                   estimated_event_time: tuple[float, float] | None = None,
                   ) -> OpCompleteEvent:
    """工序完成事件唯一构造入口 (单调性 + 过期 + 节拍窗在此强制)。"""
    t_capture = _check_triple(frames, t_ingest, t_decide)
    failures = list(verdict_failures)
    exp = _expired(frames, t_decide)
    if exp:
        failures.append(exp)
    if t_end < t_start:
        raise ValueError(f"工序时间倒序: t_start={t_start} t_end={t_end}")
    in_window = (t_end - t_start) <= takt_s
    force = {"peak_n": round(float(force_peak_n), 4),
             "mean_n": round(float(force_mean_n), 4),
             "min_required_n": round(float(force_min_n), 4),
             "samples": len(frames)}
    takt = {"takt_s": float(takt_s), "t_start": round(float(t_start), 6),
            "t_end": round(float(t_end), 6), "in_window": in_window}
    if force_peak_n < force_min_n and "force_below_min" not in failures:
        failures.append("force_below_min")
    if not clamp_confirmed and "clamp_not_confirmed" not in failures:
        failures.append("clamp_not_confirmed")
    if not in_window and "takt_exceeded" not in failures:
        failures.append("takt_exceeded")
    verdict = {"passed": not failures, "failures": failures,
               "force": force["peak_n"], "takt_in_window": in_window,
               "frames": len(frames)}
    return OpCompleteEvent(
        event_id=event_id, schema=OP_SCHEMA, kind=KIND_OP,
        clock_domain=CLOCK_DOMAIN, operation_id=operation_id,
        pallet_id=pallet_id, workpiece_id=workpiece_id,
        station_id=station_id, t_capture=t_capture,
        t_ingest=float(t_ingest), t_decide=float(t_decide),
        force=force, takt=takt, clamp_confirmed=bool(clamp_confirmed),
        estimated_event_time=(tuple(estimated_event_time)
                              if estimated_event_time else None),
        verdict=verdict,
        frames=tuple(f if isinstance(f, dict) else f.to_dict()
                     for f in frames))
