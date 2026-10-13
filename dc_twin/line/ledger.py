"""产线台账 (LineLedger) —— 场景侧持久化事务日志 (蓝图 §13)。

段语义 (core 七段事务的裁剪版): prepare → (证据) → commit → ack。
- events = 传输/审计日志: 原始请求照实追加 (含重复提交), 物化状态
  幂等 (重复提交抑制并留痕) —— 评分器据日志识别重复投料;
- snapshots = 冻结状态快照序列 (评分器快照边界判定的输入);
- 投料事务 = 一次工位循环 (上料 + 冲压工序), pallet_id 贯穿追溯。
"""
from __future__ import annotations


class LineLedger:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.snapshots: list[dict] = []
        self._ops: dict[str, dict] = {}
        self._stock: dict[str, int] = {}      # rack → 在架托盘数
        self._wip: str | None = None
        self._finished = 0

    # ------------------------------------------------------------ 事件
    def record_delivery(self, pallet_id: str, rack_node: str, *,
                        t_s: float, station_bound: str | None = None) -> None:
        """补料到架 (物流事件; 库存 +1)。``station_bound``: 架位绑定
        工位 (物料追溯字段; None = 幽灵/未绑定)。"""
        self._stock[rack_node] = self._stock.get(rack_node, 0) + 1
        self.events.append({"type": "DeliveryRecorded", "ts_s": float(t_s),
                            "pallet_id": pallet_id, "rack_node": rack_node,
                            "station_bound": station_bound})

    # ------------------------------------------------------------ 事务
    def prepare_op(self, operation_id: str, *, pallet_id: str,
                   workpiece_id: str, station_id: str, rack_node: str,
                   t_s: float) -> None:
        if operation_id in self._ops:
            raise ValueError(f"工位单重复: {operation_id}")
        if self._stock.get(rack_node, 0) < 1:
            raise ValueError(f"投料时 {rack_node} 账面无料: {operation_id}")
        self._stock[rack_node] -= 1
        self._wip = pallet_id
        self._ops[operation_id] = {
            "operation_id": operation_id, "pallet_id": pallet_id,
            "workpiece_id": workpiece_id, "station_id": station_id,
            "rack_node": rack_node, "state": "prepared",
            "commits": [], "acked_t": None, "claims": {}}
        self.events.append({"type": "OpPrepared", "ts_s": float(t_s),
                            "operation_id": operation_id,
                            "pallet_id": pallet_id,
                            "workpiece_id": workpiece_id,
                            "station_id": station_id,
                            "rack_node": rack_node})

    def commit_op(self, operation_id: str, *, t_s: float,
                  evidence_ref: str, peak_n: float, cycle_s: float,
                  in_window: bool = True,
                  _duplicate: bool = False) -> None:
        """提交工位事务; _duplicate=True 模拟重复请求 (照录+抑制)。"""
        op = self._ops.get(operation_id)
        if op is None:
            raise KeyError(f"未准备的工位单: {operation_id}")
        if op["commits"] and not _duplicate:
            raise ValueError(f"工位单重复提交: {operation_id}")
        if not op["commits"]:
            op["state"] = "committed"
            op["commits"].append(float(t_s))
            op["claims"] = {"evidence_ref": evidence_ref,
                            "peak_n": float(peak_n),
                            "cycle_s": float(cycle_s),
                            "in_window": bool(in_window)}
            self._wip = None
            self._finished += 1
        self.events.append({"type": "OpCommitted", "ts_s": float(t_s),
                            "operation_id": operation_id,
                            "evidence_ref": evidence_ref,
                            "peak_n": float(peak_n),
                            "cycle_s": float(cycle_s),
                            "in_window": bool(in_window),
                            "pallet_id": op["pallet_id"],
                            "workpiece_id": op["workpiece_id"],
                            "station_id": op["station_id"],
                            "rack_node": op["rack_node"],
                            "duplicate": bool(_duplicate)})

    def ack_op(self, operation_id: str, *, t_s: float) -> None:
        op = self._ops.get(operation_id)
        if op is None:
            raise KeyError(f"未准备的工位单: {operation_id}")
        op["state"] = "acked"
        op["acked_t"] = float(t_s)
        self.events.append({"type": "OpAcked", "ts_s": float(t_s),
                            "operation_id": operation_id})

    # ------------------------------------------------------------ 视图
    def state_of(self, operation_id: str) -> dict:
        if operation_id not in self._ops:
            raise KeyError(f"未知工位单: {operation_id}")
        return dict(self._ops[operation_id])

    def snapshot(self, *, t_s: float) -> dict:
        snap = {"t_s": float(t_s), "stock": dict(self._stock),
                "wip": self._wip, "finished": self._finished,
                "committed_ops": sum(1 for o in self._ops.values()
                                     if o["commits"])}
        self.snapshots.append(snap)
        return snap

    def export(self, *, run_id: str) -> dict:
        return {"run_id": run_id, "events": list(self.events),
                "snapshots": list(self.snapshots)}

    @classmethod
    def from_export(cls, doc: dict) -> "LineLedger":
        """跨重启恢复: 由持久化事件日志重建物化状态 (幂等重放)。"""
        led = cls()
        for e in doc["events"]:
            t, oid = e["ts_s"], e.get("operation_id", "")
            if e["type"] == "DeliveryRecorded":
                led.record_delivery(e["pallet_id"], e["rack_node"], t_s=t)
            elif e["type"] == "OpPrepared":
                led.prepare_op(oid, pallet_id=e["pallet_id"],
                               workpiece_id=e["workpiece_id"],
                               station_id=e["station_id"],
                               rack_node=e["rack_node"], t_s=t)
            elif e["type"] == "OpCommitted":
                led.commit_op(oid, t_s=t, evidence_ref=e["evidence_ref"],
                              peak_n=e["peak_n"], cycle_s=e["cycle_s"],
                              in_window=e["in_window"],
                              _duplicate=e.get("duplicate", False))
            elif e["type"] == "OpAcked":
                led.ack_op(oid, t_s=t)
        led.snapshots = [dict(s) for s in doc.get("snapshots", [])]
        return led
