#!/usr/bin/env python3
"""text2mujoco→ESR 全链试点演示 (一条命令复现)。

链路: 场景包校验 → 物理烟囱+八项 audit → 渲染层 → 证据固化 →
经验结晶 → ESR-Loop 五拍 (①运行 ②结晶 ③沉淀 ④进化门 ⑤消融) →
门重放幂等证明 → 结论。

    python3 scripts/esr_t2m_pilot.py

产物: reports/esr/pilot_t2m/{scene/output,loop} 与
tests/fixtures/esr/pilot_t2m/runs/run_t2m.json (实测证据工件)。
不修改任何既有文件; 渲染层按后端逐进程尝试 (glfw→osmesa), 不可用
则记录失败类别并降级 (物理/审计证据不受影响)。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

os.environ["MUJOCO_GL"] = "disable"          # 主进程物理面无 GL

_REPO = Path(__file__).resolve().parent.parent
_CORE = Path("~/1-Projects/desktop-agv-demo").expanduser().resolve()
for _p in (str(_REPO), str(_CORE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

SCENE = _REPO / "reports" / "esr" / "pilot_t2m" / "scene"
FIXTURE = _REPO / "tests" / "fixtures" / "esr" / "pilot_t2m"
LOOP_WS = _REPO / "reports" / "esr" / "pilot_t2m" / "loop"
VALIDATOR = (Path("~/.omp/agent/skills/text2mujoco/scripts/"
                  "validate_scene_spec.py").expanduser())

FAILURES: list[str] = []


def banner(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


def run(cmd: list[str], env: dict | None = None,
        cwd: Path | None = None, timeout: int = 240) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    if env:
        e.update(env)
    # cwd=SCENE: MuJoCo 的 MUJOCO_LOG.TXT 落在场景包内, 不写仓库根;
    # timeout: GL 后端探测可能挂死, 超时按类别降级
    try:
        return subprocess.run(cmd, capture_output=True, text=True, env=e,
                              cwd=str(cwd or SCENE), timeout=timeout)
    except subprocess.TimeoutExpired:
        class _Expired(subprocess.CompletedProcess):
            returncode = -99
            stdout = ""
            stderr = "TimeoutExpired"
        return _Expired(cmd, -99)


def check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)
    return ok


# ---------------------------------------------------------------- 0 场景包
def step_scene_package() -> None:
    banner("⓪ 场景包校验 (text2mujoco skill validator + XML + MJCF 编译)")
    if VALIDATOR.exists():
        proc = run([sys.executable, str(VALIDATOR), str(SCENE / "scene_spec.json")])
        check("scene_spec.json validator", proc.returncode == 0,
              proc.stdout.strip().splitlines()[0] if proc.stdout else "")
    else:
        print("  [SKIP] skill validator 未安装 (降级为静态检查)")
    import xml.etree.ElementTree as ET
    try:
        ET.parse(SCENE / "model.xml")
        check("model.xml parse", True)
    except ET.ParseError as exc:
        check("model.xml parse", False, type(exc).__name__)
        return
    import mujoco
    try:
        model = mujoco.MjModel.from_xml_path(str(SCENE / "model.xml"))
        check("MJCF compile", True,
              f"nq={model.nq} nu={model.nu} neq={model.neq} "
              f"mujoco={mujoco.__version__}")
    except Exception as exc:                              # noqa: BLE001
        check("MJCF compile", False, type(exc).__name__)


# ---------------------------------------------------------------- ① 物理
def step_physics() -> dict:
    banner("① 物理烟囱 + model audit 八检查 (MUJOCO_GL=disable)")
    proc = run([sys.executable, str(SCENE / "physics_smoke.py")],
               env={"MUJOCO_GL": "disable"})
    report_path = SCENE / "output" / "physics_smoke_report.json"
    if not report_path.exists():
        check("physics_smoke 运行", False,
              f"exit={proc.returncode} {proc.stderr.strip()[-160:]}")
        return {}
    report = json.loads(report_path.read_text("utf-8"))
    m = report["metrics"]
    check("八项 model audit 全过", report.get("audit8_all_pass", False),
          "collision/mass/servo/continuity/marker/start/sequence/overlap")
    check("静态层全过 (validator/parity/mass/marker 解析)",
          all((v.get("status", v) == "PASS" if isinstance(v, dict)
               else v == "PASS") for v in report["static"].values()))
    check("序列接触深度 ≥ -1e-3 m", m["max_pen_seq_m"] >= -1e-3,
          f"deepest={m['max_pen_seq_m']:.2e} @ {m['seq_steps']} steps")
    check("起始位形 ≥ -1e-4 m", m["max_pen_start_m"] >= -1e-4,
          f"{m['max_pen_start_m']:.2e}")
    check("带→臂携运到位 (±30mm)", m["carry_arrival_err_m"] <= 0.03,
          f"err={m['carry_arrival_err_m'] * 1000:.1f} mm")
    check("中空释放坠落 ≥ 20 mm", m["weld_release_drop_m"] >= 0.02,
          f"{m['weld_release_drop_m'] * 1000:.0f} mm")
    check("HANDOFF_PICK/PLACE 事件对成对", m["handoff_pair_ok"],
          str([(e["kind"], round(e["t"], 2)) for e in report["events"]]))
    check("确定性重放一致", m["determinism_ok"])
    check("任务成功 (箱入分拣腔 + 臂归位)", report["physics"]["task_success"])

    # 证据固化 (⑤拍消融回放的工件)
    ev = {
        "run_id": "T2M-PILOT-FIX",
        "scene": "t2m_handoff_cell",
        "cycles": 1,
        "max_pen_start_m": m["max_pen_start_m"],
        "max_pen_seq_m": m["max_pen_seq_m"],
        "seq_steps": m["seq_steps"],
        "carry_arrival_err_m": m["carry_arrival_err_m"],
        "weld_release_drop_m": m["weld_release_drop_m"],
        "carton_in_bin": m.get("carton_in_bin", False),
        "handoff_pair_ok": m["handoff_pair_ok"],
        "audit8_all_pass": m["audit8_all_pass"],
        "determinism_ok": m["determinism_ok"],
        "task_success": report["physics"]["task_success"],
        "events": [[e["kind"], round(e["t"], 3)] for e in report["events"]],
    }
    out = FIXTURE / "runs" / "run_t2m.json"
    out.write_text(json.dumps(ev, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"  [PASS] 证据工件固化 → {out.relative_to(_REPO)}")
    return report


# ---------------------------------------------------------------- ② 渲染
def step_render() -> None:
    banner("② 渲染层 RGB-D (逐进程: glfw → osmesa; 不可用则记录类别降级)")
    attempts = [("glfw", None), ("osmesa", None)]
    if sys.platform == "darwin":
        mjpython = subprocess.run(["bash", "-c", "command -v mjpython"],
                                  capture_output=True, text=True
                                  ).stdout.strip()
        if mjpython:
            attempts.insert(0, ("glfw", "mjpython"))
    for backend, launcher in attempts:
        env = {"MUJOCO_GL": backend}
        cmd = ([launcher, str(SCENE / "render_smoke.py")] if launcher
               else [sys.executable, str(SCENE / "render_smoke.py")])
        proc = run(cmd, env=env, timeout=120)
        out = SCENE / "output" / "render_results.json"
        if proc.returncode == 0 and out.exists():
            r = json.loads(out.read_text("utf-8"))
            check(f"render ({backend}{'+mjpython' if launcher else ''})",
                  r.get("status") == "PASS",
                  f"pixel_change={r.get('pixel_change_sum')}")
            return
        print(f"  [..] {backend} 不可用 (exit={proc.returncode})")
    print("  [SKIP] 渲染层降级: 无可用 GL 后端 (物理/审计证据不受影响); "
          "失败类别已记录于 stderr")
    FAILURES.append("render (degraded)")


# ---------------------------------------------------------------- ③ 结晶
def step_crystallize() -> None:
    banner("③ 经验结晶预检 (validate_semantics + crystallize)")
    from dc_twin.esr.crystallize import crystallize, validate_semantics
    raw = json.loads((FIXTURE / "incident.json").read_text("utf-8"))
    errs = validate_semantics(raw)
    check("四件套 schema+语义门", not errs, "; ".join(errs))
    rec = crystallize(FIXTURE)
    print(f"  incident_id : {rec.incident_id}")
    print(f"  根因假设    : {rec.root_cause.statement[:56]}… "
          f"(confidence={rec.root_cause.confidence})")
    print(f"  修复动作    : {[a.action_id + ' ' + a.target[:24] for a in rec.fix_actions]}")
    print(f"  回归用例    : {[(c.case_id, c.covers_action) for c in rec.regression_cases]}")
    regs = [c.get("register") for c in rec.contract.proposed_changes
            if isinstance(c, dict) and c.get("register")]
    print(f"  契约修订    : {rec.contract.revision_kind}/{rec.contract.status} "
          f"registers={[(r['as'], r.get('kind') or r.get('name')) for r in regs]}")


# ---------------------------------------------------------------- ④ 五拍
def step_loop() -> None:
    banner("④⑤ ESR-Loop 五拍 (①运行 ②结晶 ③沉淀 ④进化门 ⑤消融)")
    from dc_twin.esr.loop import BEATS, ESRLoop, LoopConfig
    cfg = LoopConfig(loop_id="loop-t2m-pilot-001",
                     incident_dir=FIXTURE, workspace=LOOP_WS)
    report = ESRLoop(cfg).run(resume=False)
    check("五拍闭环 complete", report.status == "complete",
          f"beats={report.beats_done} (序 {BEATS})")
    check("场景进化门 approved", bool(report.gate and report.gate.approved),
          f"registered={report.gate.registered if report.gate else None} "
          f"rejections={report.gate.rejections if report.gate else None}")
    check("消融回放全过 (缺工件/任一断言不过即败)",
          bool(report.ablation and report.ablation["passed"]),
          str([(c["case_id"], c["passed"])
               for c in (report.ablation or {}).get("cases", [])]))
    check("无拍错误", not report.errors, "; ".join(report.errors))

    banner("⑤' 门重放幂等证明 (同进程重跑 → @idempotent)")
    report2 = ESRLoop(cfg).run(resume=False)
    check("重跑闭环 complete + 落册幂等",
          report2.status == "complete"
          and any("@idempotent" in r for r in (report2.gate.registered
                                               if report2.gate else ())),
          f"registered={report2.gate.registered if report2.gate else None}")

    banner("落盘产物 (每拍持久化阶段记录 + 知识沉淀)")
    for f in sorted(LOOP_WS.rglob("*.json")):
        print(f"  {f.relative_to(LOOP_WS)}")
    skill = json.loads(next((LOOP_WS / "knowledge" / "skills")
                            .glob("*.json")).read_text("utf-8"))
    print(f"  技能条目: {skill['skill_id']} "
          f"前置={len(skill['prerequisite'])} 执行={len(skill['procedure'])} "
          f"后置={len(skill['postcondition'])}")


def main() -> int:
    print(f"text2mujoco→ESR 全链试点  "
          f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    step_scene_package()
    step_physics()
    step_render()
    step_crystallize()
    step_loop()

    banner("结论")
    degraded_render = FAILURES == ["render (degraded)"]
    hard_failures = [f for f in FAILURES if f != "render (degraded)"]
    if not hard_failures:
        print("  text → scene → 进化 全链成立:")
        print("  · 自然语言场景包通过 skill validator + 八项 model audit "
              "(实测账, 非声明)")
        print("  · 实测值固化为回归证据工件, 四件套过 schema+语义门")
        print("  · ESR-Loop 五拍闭环: gate approved + T2M_HANDOFF_CELL 落册 "
              "+ 事件对 HANDOFF_PICK/HANDOFF_PLACE 入契约 + 消融回放全过")
        if degraded_render:
            print("  · 渲染层降级 (无 GL 后端), 已记录失败类别 — 不影响链路")
        return 0
    print(f"  链路存在硬失败: {hard_failures}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
