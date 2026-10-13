#!/usr/bin/env python3
"""DetFix 样本走通 crystallize→gate (ESR-Loop 五拍) 示例。

样本: tests/fixtures/esr/detfix_m2 (蓝图 §18.② DetFix 行的取证移植:
16 runs → 三修复)。运行产出 = reports/esr/detfix_loop_v0.1/ 下的
state.json + phases/*.json + knowledge/skills/*.json + 本日志 ——
即 Loop 每拍持久化阶段记录的实样。④拍经 core 真注册表落册
(register_event_kind LANE_INTERLOCK)。

用法: python3 scripts/esr_detfix_walkthrough.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_CORE = Path("~/1-Projects/desktop-agv-demo").expanduser().resolve()
for p in (str(_REPO), str(_CORE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from dc_twin.esr.crystallize import crystallize, validate_semantics
from dc_twin.esr.loop import BEATS, ESRLoop, LoopConfig

FIXTURE = _REPO / "tests" / "fixtures" / "esr" / "detfix_m2"
WORKSPACE = _REPO / "reports" / "esr" / "detfix_loop_v0.1"


def banner(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


def main() -> int:
    banner("② 经验结晶: crystallize(incident_dir) -> IncidentRecord")
    raw = json.loads((FIXTURE / "incident.json").read_text("utf-8"))
    errs = validate_semantics(raw)
    print(f"validate_semantics → {errs if errs else 'PASS (schema+语义门)'}")
    record = crystallize(FIXTURE)
    print(f"incident_id      : {record.incident_id}")
    print(f"根因假设         : {record.root_cause.statement[:60]}… "
          f"(confidence={record.root_cause.confidence})")
    print(f"修复动作         : "
          f"{[a.action_id + ' ' + a.target for a in record.fix_actions]}")
    print(f"回归用例         : "
          f"{[(c.case_id, c.covers_action) for c in record.regression_cases]}")
    print(f"证据契约修订     : {record.contract.revision_kind} / "
          f"{record.contract.status}")

    banner("①→⑤ ESR-Loop 五拍 (④=gate: 契约草案+消融子集+回归用例缺一拒绝)")
    cfg = LoopConfig(loop_id="loop-detfix-001", incident_dir=FIXTURE,
                     workspace=WORKSPACE)
    report = ESRLoop(cfg).run(resume=False)
    print(f"status      : {report.status}")
    print(f"beats_done  : {report.beats_done}  (五拍: {BEATS})")
    print(f"gate 裁决   : approved={report.gate.approved} "
          f"registered={report.gate.registered}")
    print(f"消融验证    : passed={report.ablation['passed']} cases="
          f"{[(c['case_id'], c['passed']) for c in report.ablation['cases']]}")
    print(f"errors      : {report.errors or '无'}")

    banner("落盘产物 (每拍持久化阶段记录 + 知识沉淀)")
    for f in sorted(WORKSPACE.rglob("*.json")):
        print(f"  {f.relative_to(WORKSPACE)}")
    skill = json.loads(next((WORKSPACE / "knowledge" / "skills")
                            .glob("*.json")).read_text("utf-8"))
    print(f"技能条目 (Voyager 式): {skill['skill_id']} "
          f"前置={len(skill['prerequisite'])} 执行={len(skill['procedure'])} "
          f"证据={list(skill['evidence'])} 后置={len(skill['postcondition'])}")
    return 0 if report.status == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
