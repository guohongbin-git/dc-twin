"""esr.loop — ESR-Loop 五拍编排器 (蓝图 §18.①): ①运行 → ②经验结晶
→ ③知识沉淀 → ④场景进化 → ⑤消融验证; 每拍持久化阶段记录 (JSON),
断点续跑; ②③ 可接 LLM (MCP 工具面 stub, 接口预留不强依赖)。

fixture: tests/fixtures/esr/detfix_m2 (DetFix 样本), 全程不依赖真实
运行 —— ①拍 runner / ⑤拍 ablation_runner 均可注入。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dc_twin.esr.crystallize import crystallize
from dc_twin.esr.gate import GateError
from dc_twin.esr.loop import (
    BEATS,
    ESRLoop,
    LLMFacade,
    LoopConfig,
    NullLLM,
)

FIXTURE = Path(__file__).parent / "fixtures" / "esr" / "detfix_m2"


def _config(tmp_path: Path, incident: Path = FIXTURE) -> LoopConfig:
    return LoopConfig(loop_id="loop-detfix-001", incident_dir=incident,
                      workspace=tmp_path / "esr-loop")


def _phase_files(ws: Path) -> dict[str, dict]:
    out = {}
    for f in sorted((ws / "phases").glob("*.json")):
        out[f.name] = json.loads(f.read_text(encoding="utf-8"))
    return out


# ------------------------------------------------------------- 五拍全通
def test_five_beats_complete_on_detfix_fixture(tmp_path):
    calls = []
    loop = ESRLoop(_config(tmp_path),
                   runner=lambda cfg: calls.append(cfg.loop_id) or
                   {"runs": 16, "note": "fixture 压测证据"},
                   event_kind_registrar=lambda k, d: k)
    report = loop.run()
    assert report.status == "complete" and calls == ["loop-detfix-001"]
    assert list(report.beats_done) == list(BEATS)
    phases = _phase_files(_config(tmp_path).workspace)
    assert sorted(phases) == [f"{i:02d}-{b}.json"
                              for i, b in enumerate(BEATS, 1)]
    # ②拍记录 = 结晶四件套定档:
    assert phases["02-crystallize.json"]["record"]["incident_id"] == \
        "INC-DETFIX-M2-001"
    # ③拍知识沉淀: 技能库条目落盘 (前置→执行→证据→后置):
    skills = list((_config(tmp_path).workspace /
                   "knowledge" / "skills").glob("*.json"))
    assert len(skills) == 1
    entry = json.loads(skills[0].read_text(encoding="utf-8"))
    assert entry["procedure"][0]["action_id"] == "FIX-1"
    assert entry["evidence"]["regression"][0]["case_id"] == "REG-1"
    assert entry["postcondition"]
    # ④拍门裁决放行 + LANE_INTERLOCK 落册:
    assert report.gate and report.gate.approved
    assert report.gate.registered == ("event_kind:LANE_INTERLOCK",)
    # ⑤拍消融验证: 三用例全过 (DetFix run4 里程碑):
    assert report.ablation["passed"] is True
    assert [c["case_id"] for c in report.ablation["cases"]] == \
        ["REG-1", "REG-2", "REG-3"]


def test_beat_records_are_self_describing_json(tmp_path):
    loop = ESRLoop(_config(tmp_path),
                   event_kind_registrar=lambda k, d: k)
    loop.run()
    phases = _phase_files(_config(tmp_path).workspace)
    for i, beat in enumerate(BEATS, 1):
        rec = phases[f"{i:02d}-{beat}.json"]
        assert rec["beat"] == beat and rec["loop_id"] == "loop-detfix-001"
        assert rec["status"] == "done"


def test_state_json_tracks_progress(tmp_path):
    loop = ESRLoop(_config(tmp_path), event_kind_registrar=lambda k, d: k)
    loop.run()
    state = json.loads((_config(tmp_path).workspace / "state.json")
                       .read_text(encoding="utf-8"))
    assert state["status"] == "complete"
    assert state["beats_done"] == list(BEATS)
    assert state["config"]["incident_dir"].endswith("detfix_m2")


# ------------------------------------------------------------- 断点续跑
def test_resume_skips_completed_beats(tmp_path):
    cfg = _config(tmp_path)
    calls = []

    def runner(c):
        calls.append(1)
        return {"runs": 16}

    ESRLoop(cfg, runner=runner,
            event_kind_registrar=lambda k, d: k).run()
    assert len(calls) == 1
    report = ESRLoop(cfg, runner=runner,
                     event_kind_registrar=lambda k, d: k).run()
    assert len(calls) == 1                    # ①拍未重跑
    assert report.status == "complete"
    assert report.resumed == tuple(BEATS)     # 五拍全部命中续跑


def test_resume_retries_failed_beat(tmp_path):
    """⑤拍失败 → 断点续跑只补失败拍 (①-④ 不重跑)。"""
    cfg = _config(tmp_path)
    calls = []

    def runner(c):
        calls.append(1)
        return {"runs": 16}

    def bad_harness(case, incident_dir):
        return {"passed": False, "results": [], "reason": "harness 未接"}

    r1 = ESRLoop(cfg, runner=runner, ablation_runner=bad_harness,
                 event_kind_registrar=lambda k, d: k).run()
    assert r1.status == "failed" and r1.beats_done == tuple(BEATS[:4])
    assert len(calls) == 1

    r2 = ESRLoop(cfg, runner=runner,                  # 真 harness 上线
                 event_kind_registrar=lambda k, d: k).run()
    assert r2.status == "complete" and len(calls) == 1
    assert r2.beats_done == tuple(BEATS)
    assert r2.resumed == tuple(BEATS[:4])             # 前四拍续跑命中
    assert r2.ablation["passed"] is True


def test_gate_rejection_fails_loop_before_ablation(tmp_path):
    """进化不过门不落地: ④拍拒绝 → ⑤拍不执行, 状态 failed。"""
    ablate_called = []
    llm = _DraftLLM({"evidence_contract_revision": {
        "revision_kind": "amend", "status": "draft", "summary": "x",
        "proposed_changes": [{"path": "p", "change": "c",
                              "register": {"as": "event_kind",
                                           "kind": "lane-lock!"}}]}})
    loop = ESRLoop(_config(tmp_path), llm=llm,
                   ablation_runner=lambda c, d: ablate_called.append(1) or
                   {"passed": True, "results": []},
                   event_kind_registrar=lambda k, d: k)
    report = loop.run()
    assert report.status == "failed"
    assert report.gate and not report.gate.approved
    assert not ablate_called
    assert any("④" in e or "evolve" in e for e in report.errors)


class _DraftLLM:
    """测试替身: ②拍返回改写草稿 (模拟 LLM 结晶), ③拍不介入。"""

    def __init__(self, overlay: dict):
        self._overlay = overlay

    def draft_crystallization(self, incident: dict) -> dict:
        obj = json.loads(json.dumps(incident, ensure_ascii=False))
        obj.update(self._overlay)
        return obj

    def draft_skill(self, record: dict) -> dict | None:
        return None


# ------------------------------------------------------------- LLM 面
def test_llm_draft_is_used_for_crystallization(tmp_path):
    llm = _DraftLLM({"title": "LLM 改写标题"})
    report = ESRLoop(_config(tmp_path), llm=llm,
                     event_kind_registrar=lambda k, d: k).run()
    assert report.status == "complete"
    phases = _phase_files(_config(tmp_path).workspace)
    assert phases["02-crystallize.json"]["record"]["title"] == \
        "LLM 改写标题"
    assert phases["02-crystallize.json"]["crystallized_by"] == "llm"


def test_llm_bad_draft_falls_back_not_crashes(tmp_path):
    class BadLLM:
        def draft_crystallization(self, incident):
            return {"schema": "junk"}

        def draft_skill(self, record):
            return None

    report = ESRLoop(_config(tmp_path), llm=BadLLM(),
                     event_kind_registrar=lambda k, d: k).run()
    assert report.status == "complete"          # 确定性回退兜底
    phases = _phase_files(_config(tmp_path).workspace)
    assert phases["02-crystallize.json"]["crystallized_by"] == \
        "deterministic"
    assert any("草稿" in e for e in report.warnings)


def test_llm_skill_overlay_merges_into_knowledge(tmp_path):
    class SkillLLM(NullLLM):
        def draft_skill(self, record):
            return {"lesson": "跨车接触即可毁掉 13mm 级装配公差"}

    ESRLoop(_config(tmp_path), llm=SkillLLM(),
            event_kind_registrar=lambda k, d: k).run()
    entry = json.loads(next((_config(tmp_path).workspace /
                             "knowledge" / "skills").glob("*.json"))
                       .read_text(encoding="utf-8"))
    assert entry["lesson"] == "跨车接触即可毁掉 13mm 级装配公差"
    assert entry["procedure"]                    # 确定性骨架保留


def test_llm_facade_is_protocol_and_nullllm_is_safe_stub():
    assert issubclass(NullLLM, LLMFacade)
    stub = NullLLM()
    assert stub.draft_crystallization({}) is None
    assert stub.draft_skill({}) is None


# ------------------------------------------------------------- 契约
def test_missing_incident_dir_fails_beat_one_cleanly(tmp_path):
    cfg = LoopConfig(loop_id="l2",
                     incident_dir=tmp_path / "nope",
                     workspace=tmp_path / "ws")
    report = ESRLoop(cfg).run()
    assert report.status == "failed"
    assert report.beats_done == () and report.errors
    assert not (tmp_path / "ws" / "phases").exists() or not list(
        (tmp_path / "ws" / "phases").glob("*.json"))


def test_crystallize_and_gate_agree_with_standalone_paths(tmp_path):
    """Loop ②④拍产物与独立调用 crystallize()/gate 一致 (同源)。"""
    report = ESRLoop(_config(tmp_path),
                     event_kind_registrar=lambda k, d: k).run()
    standalone = crystallize(FIXTURE)
    phases = _phase_files(_config(tmp_path).workspace)
    assert phases["02-crystallize.json"]["record"] == standalone.to_dict()
    assert report.gate.proposal_id == \
        f"PROP-{standalone.incident_id}"
