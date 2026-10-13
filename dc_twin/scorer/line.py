"""产线独立评分器 (C2 Oracle 独立契约; 蓝图 §13 产量对账 + 物料追溯)。

红线 (同 dc_twin.scorer.oracle):
- 只读独立导出的真值轨迹 (truth.json) + 持久化台账事件日志/快照
  (line_ledger.json) + 运行时证据 (evidence_events.json);
- **不调用被测系统业务判定函数**, 不 import dc_twin 运行时/账本/
  场景模块, 不 import sim.*/services.* (纯 JSON 规则核验);
- 产量对账: 工位物理完成数 (truth.cycles, 力达标) vs 账本提交数;
- 物料追溯: pallet_id → 交付架位 (绑定工位) → 工位 operation_id;
- 六负向判: 空料上料 / 伪造工序完成 / 未夹紧动作 / 节拍超时未报 /
  错工位投料 / 重复投料 (anomaly)。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

RULE_VERSION = "dc_twin.line_oracle/1"

VERDICT_CONSISTENT = "CONSISTENT"
V_FORGED = "MISMATCH_FORGED_OPERATION"
V_EMPTY = "MISMATCH_EMPTY_FEED"
V_UNCLAMPED = "MISMATCH_UNCLAMPED"
V_TAKT = "MISMATCH_TAKT_OVERRUN"
V_WRONG = "MISMATCH_WRONG_STATION"


@dataclass(frozen=True)
class OpVerdict:
    operation_id: str
    verdict: str
    reason: str
    anomalies: tuple[str, ...] = ()
    duplicate_count: int = 0
    claims: dict = field(default_factory=dict)
    truth: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryVerdict:
    pallet_id: str
    rack_node: str
    verdict: str
    reason: str


@dataclass(frozen=True)
class LineOracleReport:
    run_id: str
    rule_version: str
    core_version: str
    input_hashes: dict
    ops: list[OpVerdict]
    deliveries: list[DeliveryVerdict]
    reconciled: bool
    physical_completions: int
    ledger_completions: int
    zero_stoppage: bool
    stoppage_ticks: list
    summary: dict

    def to_json(self) -> str:
        d = dict(self.__dict__)
        d["ops"] = [dict(o.__dict__) for o in self.ops]
        d["deliveries"] = [dict(o.__dict__) for o in self.deliveries]
        return json.dumps(d, ensure_ascii=False, indent=1)


def _load(run_dir: Path) -> tuple[dict, dict, dict]:
    def _j(name):
        return json.loads((run_dir / name).read_text(encoding="utf-8"))

    return _j("truth.json"), _j("line_ledger.json"), \
        _j("evidence_events.json")


def _hash(run_dir: Path) -> dict:
    out = {}
    for name in ("truth.json", "line_ledger.json",
                 "evidence_events.json"):
        p = run_dir / name
        if p.exists():
            out[name] = hashlib.sha256(
                p.read_bytes()).hexdigest()[:16]
    return out


def score_line_run(run_dir) -> LineOracleReport:
    run_dir = Path(run_dir)
    truth, led, _evs = _load(run_dir)
    takt = float(truth["takt_s"])
    station_doc = next(iter(truth["stations"].values()))
    bindings: dict[str, str] = station_doc.get("rack_bindings", {})
    cycles_by_op = {c["op_id"]: c for c in truth["cycles"]}
    deliveries_truth = {d["pallet_id"]: d for d in truth["deliveries"]}

    # ---- 台账侧: 唯一提交 (去重) + 重复计数
    ops_ledger: dict[str, dict] = {}
    dup_count: dict[str, int] = {}
    for e in led["events"]:
        if e["type"] != "OpCommitted":
            continue
        oid = e["operation_id"]
        if e.get("duplicate"):
            dup_count[oid] = dup_count.get(oid, 0) + 1
        else:
            ops_ledger.setdefault(oid, e)

    # ---- 工位单逐笔核对
    ops: list[OpVerdict] = []
    for oid, e in sorted(ops_ledger.items()):
        anomalies: list[str] = []
        if dup_count.get(oid):
            anomalies.append("duplicate_feeds")
        verdict, reason = VERDICT_CONSISTENT, ""
        cyc = cycles_by_op.get(oid)
        if cyc is None:
            verdict, reason = V_FORGED, "台账提交无对应物理工序 (真值无 cycle)"
        else:
            d = deliveries_truth.get(cyc["pallet_id"]) \
                or deliveries_truth.get(e["pallet_id"])
            if d is None:
                verdict, reason = V_EMPTY, \
                    "账面托盘无物理交付 (幽灵补料) / 冲压时夹具无料"
            elif cyc["fixture_empty"]:
                verdict, reason = V_EMPTY, "冲压时夹具无工件 (空料上料)"
            elif not cyc["clamp_active_during"]:
                verdict, reason = V_UNCLAMPED, "冲压窗口夹紧确认缺失"
            elif cyc["cycle_s"] > takt and e["in_window"]:
                verdict, reason = V_TAKT, (
                    f"节拍超时未报 (真值 {cyc['cycle_s']:.1f}s > "
                    f"takt {takt:.1f}s 而账面声称窗内)")
            if verdict == VERDICT_CONSISTENT and d is not None:
                bound = bindings.get(d["rack_node"])
                if (bound != e["station_id"]
                        or d["rack_node"] != d["rack_node_target"]):
                    verdict, reason = V_WRONG, (
                        f"物料 {d['pallet_id']} 交付架位 {d['rack_node']} "
                        f"(绑定 {bound}) 与派单工位 "
                        f"{d['order_station']} 不一致")
        ops.append(OpVerdict(
            operation_id=oid, verdict=verdict, reason=reason,
            anomalies=tuple(anomalies), duplicate_count=dup_count.get(oid, 0),
            claims={"peak_n": e.get("peak_n"), "cycle_s": e.get("cycle_s"),
                    "in_window": e.get("in_window"),
                    "station_id": e.get("station_id")},
            truth={} if cyc is None else {
                "peak_n": cyc["peak_n"], "cycle_s": cyc["cycle_s"],
                "pallet_id": cyc["pallet_id"],
                "clamp_active_during": cyc["clamp_active_during"],
                "fixture_empty": cyc["fixture_empty"],
                "force_ok": cyc["force_ok"]}))

    # ---- 交付逐笔核对 (幽灵交付 / 错架位路由)
    deliveries: list[DeliveryVerdict] = []
    for e in led["events"]:
        if e["type"] != "DeliveryRecorded":
            continue
        d = deliveries_truth.get(e["pallet_id"])
        if d is None:
            deliveries.append(DeliveryVerdict(
                pallet_id=e["pallet_id"], rack_node=e["rack_node"],
                verdict=V_EMPTY, reason="幽灵交付 (真值无此 AGV 交付)"))
            continue
        if d["rack_node"] != e["rack_node"] or \
                d["rack_node"] != d["rack_node_target"]:
            deliveries.append(DeliveryVerdict(
                pallet_id=e["pallet_id"], rack_node=e["rack_node"],
                verdict=V_WRONG, reason=(
                    f"实际交付 {d['rack_node']} (目标 "
                    f"{d['rack_node_target']}, 绑定 "
                    f"{bindings.get(d['rack_node'])})")))
        else:
            deliveries.append(DeliveryVerdict(
                pallet_id=e["pallet_id"], rack_node=e["rack_node"],
                verdict=VERDICT_CONSISTENT, reason=""))

    # ---- 产量对账 + 零停线
    physical = sum(1 for c in truth["cycles"] if c["force_ok"])
    ledger_n = len(ops_ledger)
    reconciled = physical == ledger_n
    stoppage = list(truth["stoppage_ticks"])
    zero_stoppage = not stoppage and all(
        s["stock"].get(station_doc["rack_primary"], 0) >= 1
        for s in truth["stock_timeline"])

    counts: dict[str, int] = {}
    for o in ops:
        counts[o.verdict] = counts.get(o.verdict, 0) + 1
    for d in deliveries:
        counts[d.verdict] = counts.get(d.verdict, 0) + 1
    return LineOracleReport(
        run_id=truth["run_id"], rule_version=RULE_VERSION,
        core_version=truth.get("core_version", "unknown"),
        input_hashes=_hash(run_dir), ops=ops, deliveries=deliveries,
        reconciled=reconciled, physical_completions=physical,
        ledger_completions=ledger_n, zero_stoppage=zero_stoppage,
        stoppage_ticks=stoppage, summary=counts)
