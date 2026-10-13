"""独立评分器 (C2 Oracle 独立契约; 蓝图 v0.5.1 §11)。

红线:
- 只读独立导出的真值轨迹 (truth.json) + 持久化台账快照/事件日志
  (ledger.json); **不调用被测系统业务判定函数**, 不 import
  dc_twin 运行时/账本模块 (纯 JSON 规则核验);
- 按 pallet_id + location + custody + operation_id 核对归属与事务;
- 快照边界区分「短暂未决」(卡车未离场 → 待恢复) vs「持久账实错误」
  (卡车已离场 → 未结清离场);
- 记录输入哈希 / core 版本 / 规则版本 / run_id。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

RULE_VERSION = "dc_twin.oracle/1"

VERDICT_CONSISTENT = "CONSISTENT"
VERDICT_FORGED = "MISMATCH_FORGED_SUCCESS"
VERDICT_WRONG_TRUCK = "MISMATCH_WRONG_TRUCK"
VERDICT_DEPARTURE_UNSETTLED = "MISMATCH_DEPARTURE_UNSETTLED"
VERDICT_PENDING = "PENDING_RECOVERY"


@dataclass(frozen=True)
class OpVerdict:
    operation_id: str
    pallet_id: str
    verdict: str
    reason: str
    anomalies: tuple[str, ...] = ()
    truck_departed: bool = False
    duplicate_count: int = 0
    durable_commit_time: float | None = None
    truth_settle_time: float | None = None
    window_s: float | None = None
    triple_equal: bool = False


@dataclass(frozen=True)
class OracleReport:
    run_id: str
    core_version: str
    rule_version: str
    input_hashes: dict[str, str]
    ops: tuple[OpVerdict, ...]
    max_nonatomic_window_s: float
    loaded_departed_closed: int

    def to_dict(self) -> dict:
        return {"run_id": self.run_id, "core_version": self.core_version,
                "rule_version": self.rule_version,
                "input_hashes": self.input_hashes,
                "max_nonatomic_window_s": self.max_nonatomic_window_s,
                "loaded_departed_closed": self.loaded_departed_closed,
                "ops": [dict(v.__dict__) for v in self.ops]}


# ---------------------------------------------------------------- 规则
def _ledger_index(ledger_doc: dict) -> dict[str, dict]:
    """由事件日志重建每装运单的持久化事实 (纯 JSON 规则, 不依赖账本类)。"""
    idx: dict[str, dict] = {}
    for e in ledger_doc["events"]:
        op = idx.setdefault(e["operation_id"], {
            "prepared": False, "committed": False, "acked": False,
            "commit_truck": None, "commit_t": None,
            "requests": 0, "commits": 0, "acks": 0})
        if e["type"] == "LoadPrepared":
            op["prepared"] = True
        elif e["type"] == "LoadCommitRequested":
            op["requests"] += 1
        elif e["type"] == "LoadCommitted":
            op["committed"] = True
            op["commits"] += 1
            op["commit_truck"] = e["truck_id"]
            op["commit_t"] = e["ts_s"]
        elif e["type"] == "LoadAcked":
            op["acked"] = True
            op["acks"] += 1
    return idx


def _in_rect(xyz, rect) -> bool:
    return (rect[0] <= xyz[0] <= rect[1] and rect[2] <= xyz[1] <= rect[3])


def _score_op(op: dict, led: dict | None, trucks: dict) -> OpVerdict:
    loaded = op.get("truth_state") == "loaded"
    truth_truck = op.get("truck_id")
    departed = (truth_truck is not None
                and trucks.get(truth_truck, {}).get("departed_t") is not None)
    led = led or {}
    committed = bool(led.get("committed"))
    commit_truck = led.get("commit_truck")
    commit_t = led.get("commit_t")
    anomalies: list[str] = []
    dup = max(0, int(led.get("requests", 0)) - 1) if led else 0
    if dup:
        anomalies.append("duplicate_events")
    if committed and not led.get("acked"):
        anomalies.append("ack_missing")

    # ---- 六类负向判定 (真值 × 持久化台账) ----
    if committed and not loaded:
        verdict, reason = VERDICT_FORGED, (
            "台账提交装车, 真值托盘未上车 "
            f"(truth_state={op.get('truth_state')})")
    elif committed and truth_truck != commit_truck:
        verdict, reason = VERDICT_WRONG_TRUCK, (
            f"custody 不符: 真值在 {truth_truck} 车斗, 台账记 {commit_truck}")
    elif loaded and not committed:
        if departed:
            verdict, reason = VERDICT_DEPARTURE_UNSETTLED, (
                "卡车离场时装运未提交 (未结清货物离场)")
        else:
            verdict, reason = VERDICT_PENDING, (
                "物理已装车但台账未提交 (正确但未闭合 → 待恢复)")
    elif not loaded and not committed:
        verdict, reason = VERDICT_CONSISTENT, (
            "真值未装车且台账无提交 (两侧一致为空)")
    else:
        verdict, reason = VERDICT_CONSISTENT, (
            "装车事实/归属/事务三方一致")

    # ---- 账实三元等式 (pallet_id + location + custody) ----
    triple = (verdict == VERDICT_CONSISTENT and loaded and committed
              and truth_truck == commit_truck
              and _in_rect(op.get("pallet_xyz_at_commit") or (),
                           trucks.get(truth_truck, {}).get("bed_rect", ())))
    window = None
    if committed and op.get("truth_settle_time") is not None:
        window = round(commit_t - op["truth_settle_time"], 6)
    return OpVerdict(
        operation_id=op["operation_id"], pallet_id=op["pallet_id"],
        verdict=verdict, reason=reason, anomalies=tuple(anomalies),
        truck_departed=departed, duplicate_count=dup,
        durable_commit_time=commit_t,
        truth_settle_time=op.get("truth_settle_time"), window_s=window,
        triple_equal=triple)


# ---------------------------------------------------------------- 入口
def score_run(run_dir) -> OracleReport:
    run_dir = Path(run_dir)
    truth_p, ledger_p = run_dir / "truth.json", run_dir / "ledger.json"
    truth = json.loads(truth_p.read_text(encoding="utf-8"))
    ledger_doc = json.loads(ledger_p.read_text(encoding="utf-8"))
    idx = _ledger_index(ledger_doc)
    trucks = {t["truck_id"]: t for t in truth["trucks"]}
    ops = tuple(_score_op(op, idx.get(op["operation_id"]), trucks)
                for op in truth["ops"])
    windows = [o.window_s for o in ops if o.window_s is not None]
    return OracleReport(
        run_id=truth.get("run_id", ""),
        core_version=truth.get("core_version", "unknown"),
        rule_version=RULE_VERSION,
        input_hashes={
            "truth.json": hashlib.sha256(truth_p.read_bytes()).hexdigest(),
            "ledger.json": hashlib.sha256(ledger_p.read_bytes()).hexdigest(),
        },
        ops=ops,
        max_nonatomic_window_s=round(max(windows), 6) if windows else 0.0,
        loaded_departed_closed=sum(
            1 for o in ops
            if o.triple_equal and o.truck_departed
            and "ack_missing" not in o.anomalies))
