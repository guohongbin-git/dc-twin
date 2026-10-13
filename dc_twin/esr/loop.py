"""ESR-Loop 五拍编排器 (蓝图 §18.①):

    ① 运行      ② 经验结晶     ③ 知识沉淀      ④ 场景进化      ⑤ 消融验证
    sim 连续  →  事件/故障/    →  技能库+证据   →  注册表新类型/  →  契约回归门
    运转        停机/对账分歧     契约自动起草     证据契约自动生成
      ↑ ________________________________________________________|
                        每一次进化都要过 ⑤ 的消融门

- 每拍落一个阶段记录 (workspace/phases/NN-<beat>.json) + state.json
  进度账; 断点续跑 = 跳过已落账拍, 失败拍重做 (进化不靠信仰)。
- ②③ 拍可接 LLM: LLMFacade 协议即 MCP 工具面接口预留
  (esr.crystallize_draft / esr.skill_draft 两个 tool 形状), NullLLM
  为无 LLM 安全桩 —— 确定性路径永不强依赖 LLM。
- ④拍: 结晶记录 → EvolutionProposal → esr.gate (审查 + core 注册表
  落册); ⑤拍: 回归用例按证据工件回放断言 (默认) 或注入真消融
  harness。
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from .crystallize import (
    SCHEMA_ID,
    IncidentRecord,
    RegressionCase,
    crystallize,
    evaluate_assert,
    validate_semantics,
)
from .gate import (
    AblationSubset,
    EvolutionProposal,
    EvidenceContractDraft,
    GateVerdict,
    admit,
)

#: 五拍 (蓝图 §18.① 顺序即状态机序)
BEATS = ("run", "crystallize", "consolidate", "evolve", "ablate")

STATE_SCHEMA = "esr.loop_state/0.1"
SKILL_SCHEMA = "esr.skill_entry/0.1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_json(path: Path, obj: dict) -> None:
    """原子落盘 (tmp + replace): 断电/中断不留半截记录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


# ================================================================ 配置
@dataclass(frozen=True)
class LoopConfig:
    """一次 ESR-Loop 的输入: 事故目录 (①拍证据 + ②拍结晶源) 与
    工作区 (阶段记录/知识库落盘处)。"""

    loop_id: str
    incident_dir: Path
    workspace: Path

    def to_dict(self) -> dict:
        return {"loop_id": self.loop_id,
                "incident_dir": str(self.incident_dir),
                "workspace": str(self.workspace)}

    @classmethod
    def from_dict(cls, obj: dict) -> "LoopConfig":
        return cls(loop_id=obj["loop_id"],
                   incident_dir=Path(obj["incident_dir"]),
                   workspace=Path(obj["workspace"]))


# ================================================================ LLM 面
@runtime_checkable
class LLMFacade(Protocol):
    """②③拍可选 LLM 接入点 —— MCP 工具面接口预留 (不强依赖)。

    对应 MCP tools (宿主绑定传输层, 本包不管):
    - esr.crystallize_draft(incident) -> dict | None   # ②拍草稿
    - esr.skill_draft(record) -> dict | None           # ③拍润色
    返回 None → 确定性回退; 草稿不过校验 → 回退并记 warning
    (LLM 出错不阻断闭环)。
    """

    def draft_crystallization(self, incident: dict) -> dict | None: ...

    def draft_skill(self, record: dict) -> dict | None: ...


class NullLLM:
    """无 LLM 安全桩 (MCP 工具面 stub): 恒回退确定性路径。"""

    def draft_crystallization(self, incident: dict) -> dict | None:
        return None

    def draft_skill(self, record: dict) -> dict | None:
        return None


# ================================================================ 报告
@dataclass(frozen=True)
class LoopReport:
    loop_id: str
    status: str                       # complete | failed
    beats_done: tuple[str, ...]
    resumed: tuple[str, ...]          # 本次续跑命中的已完成拍
    gate: GateVerdict | None = None
    ablation: dict | None = None
    errors: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)


# ================================================================ 编排器
class ESRLoop:
    """五拍状态机: run() 顺序推进, 每拍成功即落账, 失败即停。

    注入面 (全部可选):
    - runner: ①拍真运行 (接 sim/MCP); 缺省核验 incident_dir/runs 证据;
    - llm: ②③拍 LLM (LLMFacade; 缺省 NullLLM);
    - ablation_runner: ⑤拍真消融 harness; 缺省按回归用例的 evidence
      工件回放断言 (fixture/离线模式);
    - node_source_registrar / event_kind_registrar: ④拍注册器
      (缺省 core 真接口, 测试可注入)。
    """

    def __init__(self, config: LoopConfig, *, runner: Callable | None = None,
                 llm: LLMFacade | None = None,
                 ablation_runner: Callable | None = None,
                 node_source_registrar: Callable | None = None,
                 event_kind_registrar: Callable | None = None):
        self._cfg = config
        self._runner = runner
        self._llm = llm or NullLLM()
        self._ablation_runner = ablation_runner
        self._ns_reg = node_source_registrar
        self._ek_reg = event_kind_registrar

    # ------------------------------------------------------------ 持久层
    @property
    def workspace(self) -> Path:
        return self._cfg.workspace

    def _phase_path(self, beat: str) -> Path:
        return self.workspace / "phases" / (
            f"{BEATS.index(beat) + 1:02d}-{beat}.json")

    def _load_state(self) -> dict:
        p = self.workspace / "state.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        return {}

    def _save_state(self, beats_done: list[str], status: str,
                    beat_errors: dict[str, str]) -> None:
        _atomic_json(self.workspace / "state.json", {
            "schema": STATE_SCHEMA, "loop_id": self._cfg.loop_id,
            "config": self._cfg.to_dict(), "beats_done": beats_done,
            "status": status, "beat_errors": beat_errors,
            "updated": _now()})

    def _record(self, beat: str, status: str, **payload) -> None:
        _atomic_json(self._phase_path(beat), {
            "schema": STATE_SCHEMA, "loop_id": self._cfg.loop_id,
            "beat": beat, "status": status, "at": _now(), **payload})

    # ------------------------------------------------------------ 主入口
    def run(self, resume: bool = True) -> LoopReport:
        """推进状态机至完成或失败; resume=True 跳过已落账拍 (断点
        续跑), 失败拍重做。拍内异常捕获入报告, 不外抛。"""
        state = self._load_state() if resume else {}
        beats_done = [b for b in state.get("beats_done", [])
                      if self._phase_path(b).exists()]
        resumed = list(beats_done)
        beat_errors: dict[str, str] = dict(state.get("beat_errors", {})) \
            if state.get("status") == "failed" else {}
        warnings: list[str] = []
        gate: GateVerdict | None = None
        ablation: dict | None = None

        for beat in BEATS:
            if beat in beats_done:
                continue
            record = self._record_if_done(beats_done)
            try:
                if beat == "run":
                    self._beat_run()
                elif beat == "crystallize":
                    rec, by, warn = self._beat_crystallize()
                    if warn:
                        warnings.append(warn)
                    self._record(beat, "done", record=rec.to_dict(),
                                 crystallized_by=by)
                elif beat == "consolidate":
                    skill_path = self._beat_consolidate(record)
                    self._record(beat, "done",
                                 skill_path=str(skill_path))
                elif beat == "evolve":
                    gate = self._beat_evolve(record)
                    self._record(beat, "done",
                                 proposal_id=gate.proposal_id,
                                 gate_approved=gate.approved,
                                 rejections=list(gate.rejections),
                                 registered=list(gate.registered))
                    if not gate.approved:
                        beat_errors[beat] = (
                            f"④ 场景进化: 门拒绝 — "
                            f"{'; '.join(gate.rejections)}")
                        break
                elif beat == "ablate":
                    ablation = self._beat_ablate(record)
                    self._record(beat, "done", **ablation)
                    if not ablation.get("passed"):
                        beat_errors[beat] = "⑤ 消融验证: 契约回归门未过"
                        break
            except Exception as exc:                     # noqa: BLE001
                beat_errors[beat] = f"拍 {beat} 失败: {exc}"
                break
            beats_done.append(beat)
            beat_errors.pop(beat, None)     # 重试成功即销账

        complete = BEATS[-1] in beats_done
        failed = bool(beat_errors) and not complete
        self._save_state(beats_done,
                         "failed" if failed else
                         ("complete" if complete else "running"),
                         beat_errors)
        if gate is None and "evolve" in beats_done:
            gate = self._verdict_from_phase()
        if ablation is None and "ablate" in beats_done:
            rec = json.loads(self._phase_path("ablate")
                             .read_text(encoding="utf-8"))
            ablation = {"passed": rec.get("passed"),
                        "cases": rec.get("cases", [])}
        return LoopReport(
            loop_id=self._cfg.loop_id,
            status="failed" if failed else
            ("complete" if complete else "running"),
            beats_done=tuple(beats_done), resumed=tuple(resumed),
            gate=gate, ablation=ablation,
            errors=tuple(beat_errors.values()),
            warnings=tuple(warnings))

    def _record_if_done(self, beats_done: list[str]) -> IncidentRecord | None:
        """续跑上下文: ②拍已落账则还原结晶定档。"""
        if "crystallize" not in beats_done:
            return None
        rec = json.loads(self._phase_path("crystallize")
                         .read_text(encoding="utf-8"))
        return IncidentRecord.from_dict(rec["record"])

    def _verdict_from_phase(self) -> GateVerdict | None:
        """续跑上下文: ④拍已落账则还原门裁决。"""
        rec = json.loads(self._phase_path("evolve").read_text(
            encoding="utf-8"))
        return GateVerdict(
            proposal_id=rec.get("proposal_id", ""),
            approved=bool(rec.get("gate_approved")),
            rejections=tuple(rec.get("rejections", ())),
            registered=tuple(rec.get("registered", ())))

    # ------------------------------------------------------------ 各拍
    def _beat_run(self) -> None:
        """① 运行: 真.runner 接 sim; 缺省核验 ① 拍证据在位。"""
        incident_dir = self._cfg.incident_dir
        if self._runner is not None:
            summary = self._runner(self._cfg) or {}
            self._record("run", "done", mode="runner", summary=summary)
            return
        if not (incident_dir / "incident.json").exists():
            raise FileNotFoundError(
                f"① 运行: 事故证据目录缺 incident.json "
                f"({incident_dir}); 或注入 runner 接真运行")
        runs = sorted(p.name for p in (incident_dir / "runs").glob("*.json")
                      ) if (incident_dir / "runs").is_dir() else []
        self._record("run", "done", mode="reuse_existing",
                     incident_dir=str(incident_dir), runs=runs)

    def _beat_crystallize(self) -> tuple[IncidentRecord, str, str | None]:
        """② 经验结晶: LLM 草稿 (可选) → 校验; 不过 → 确定性回退。"""
        raw = json.loads((self._cfg.incident_dir / "incident.json")
                         .read_text(encoding="utf-8"))
        try:
            draft = self._llm.draft_crystallization(raw)
        except Exception as exc:                     # noqa: BLE001
            return (crystallize(self._cfg.incident_dir), "deterministic",
                    f"LLM 草稿异常, 已回退: {exc}")
        if draft is None:
            return crystallize(self._cfg.incident_dir), "deterministic", None
        errs = validate_semantics(draft)
        if errs or draft.get("schema") != SCHEMA_ID:
            return (crystallize(self._cfg.incident_dir), "deterministic",
                    "LLM 草稿未过校验, 已回退: " + "; ".join(errs))
        return IncidentRecord.from_dict(draft), "llm", None

    def _beat_consolidate(self, record: IncidentRecord) -> Path:
        """③ 知识沉淀: 四件套 → Voyager 式技能条目 (前置→执行→证据
        →后置); LLM 可润色 (overlay), 确定性骨架不动。"""
        entry = self._skill_entry(record)
        try:
            overlay = self._llm.draft_skill(record.to_dict())
        except Exception:                            # noqa: BLE001
            overlay = None
        if overlay:
            for k, v in overlay.items():
                if k not in ("schema", "skill_id", "incident_id"):
                    entry[k] = v
        skills_dir = self.workspace / "knowledge" / "skills"
        path = skills_dir / f"{record.incident_id}.skill.json"
        _atomic_json(path, entry)
        return path

    def _skill_entry(self, record: IncidentRecord) -> dict:
        return {
            "schema": SKILL_SCHEMA,
            "skill_id": f"skill.{record.incident_id}",
            "incident_id": record.incident_id,
            "title": record.title,
            "prerequisite": [
                f"根因假设: {record.root_cause.statement}",
                *[f"机制: {m}" for m in record.root_cause.mechanism]],
            "procedure": [
                {"action_id": a.action_id, "target": a.target,
                 "description": a.description}
                for a in record.fix_actions],
            "evidence": {
                "contract_revision": {
                    "revision_kind": record.contract.revision_kind,
                    "summary": record.contract.summary,
                    "status": record.contract.status},
                "regression": [
                    {"case_id": c.case_id, "scenario": c.scenario,
                     "asserts": [dict(a) for a in c.asserts]}
                    for c in record.regression_cases]},
            "postcondition": [a.expected_effect for a in
                              record.fix_actions if a.expected_effect],
            "provenance": {"source": record.source,
                           "crystallized_at": _now()},
        }

    def _beat_evolve(self, record: IncidentRecord) -> GateVerdict:
        """④ 场景进化: 契约修订草案 + 回归用例 + 消融子集 → 门。"""
        proposal = self._proposal_from(record)
        return admit(proposal,
                     node_source_registrar=self._ns_reg,
                     event_kind_registrar=self._ek_reg)

    def _proposal_from(self, record: IncidentRecord) -> EvolutionProposal:
        rev = record.contract
        registers = [c.get("register") for c in rev.proposed_changes
                     if isinstance(c, dict) and c.get("register")]
        if registers:
            reg = registers[0]
            kind = reg.get("as", "event_kind")
            payload = ({"kind": reg.get("kind", ""),
                        "description": reg.get("description", "")}
                       if kind == "event_kind" else
                       {"fn": reg.get("fn"), "name": reg.get("name", "")})
        else:
            kind, payload = "event_kind", {"kind": "", "description": ""}
        scenarios, seeds, metrics = [], [], []
        for c in record.regression_cases:
            if c.scenario not in scenarios:
                scenarios.append(c.scenario)
            for s in c.seeds:
                if s not in seeds:
                    seeds.append(s)
            for spec in c.asserts:
                if spec.get("metric") and spec["metric"] not in metrics:
                    metrics.append(spec["metric"])
        return EvolutionProposal(
            proposal_id=f"PROP-{record.incident_id}", kind=kind,
            payload=payload,
            evidence_contract=EvidenceContractDraft(
                name=f"esr.contract.{record.incident_id.lower()}/0.1",
                event_kinds=tuple(
                    r.get("kind", "") for r in registers
                    if r.get("as") == "event_kind") or ("N/A",),
                guarantees=tuple(
                    c.get("change", "") for c in rev.proposed_changes
                    if isinstance(c, dict)),
                summary=rev.summary),
            ablation=AblationSubset(
                name=f"ablation.{record.incident_id}",
                base_subset="B0-B4", scenarios=tuple(scenarios),
                seeds=tuple(seeds), metrics=tuple(metrics)),
            regression_cases=record.regression_cases)

    def _beat_ablate(self, record: IncidentRecord) -> dict:
        """⑤ 消融验证: 逐回归用例跑断言 (缺省 = evidence 工件回放;
        注入 ablation_runner 接真 30-seed 契约回归门)。"""
        cases_out = []
        passed = True
        for case in record.regression_cases:
            out = (self._ablation_runner(case, self._cfg.incident_dir)
                   if self._ablation_runner is not None
                   else self._replay_case(case, self._cfg.incident_dir))
            cases_out.append({"case_id": case.case_id,
                              "passed": bool(out.get("passed")),
                              "results": out.get("results", [])})
            passed = passed and bool(out.get("passed"))
        return {"passed": passed, "cases": cases_out}

    @staticmethod
    def _replay_case(case: RegressionCase, incident_dir: Path) -> dict:
        """离线回放: case.evidence 工件 (fixture/真实 run 导出, 相对
        incident_dir 解析) 上求值全部断言 —— 宁严勿漏, 缺工件/任一
        断言不过即败。"""
        if not case.evidence:
            return {"passed": False,
                    "results": [{"assert": "evidence", "ok": False,
                                 "detail": "回归用例未附证据工件"}]}
        path = Path(case.evidence)
        if not path.is_absolute():
            path = incident_dir / path
        if not path.exists():
            return {"passed": False,
                    "results": [{"assert": "evidence", "ok": False,
                                 "detail": f"证据工件缺失: {path}"}]}
        artifact = json.loads(path.read_text(encoding="utf-8"))
        results = []
        for spec in case.asserts:
            ok, detail = evaluate_assert(artifact, spec)
            results.append({"assert": dict(spec), "ok": ok,
                            "detail": detail})
        return {"passed": all(r["ok"] for r in results),
                "results": results, "artifact": str(path)}

    # ------------------------------------------------------------ 断点
    @classmethod
    def resume(cls, workspace: str | Path, **overrides) -> "ESRLoop":
        """从既有工作区重建编排器 (配置取自 state.json)。"""
        state = json.loads((Path(workspace) / "state.json")
                           .read_text(encoding="utf-8"))
        cfg = LoopConfig.from_dict(state["config"])
        return cls(cfg, **overrides)
