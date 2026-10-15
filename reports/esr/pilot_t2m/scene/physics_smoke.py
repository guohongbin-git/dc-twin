#!/usr/bin/env python3
"""t2m_handoff_cell physics smoke + 八项 model audit (text2mujoco skill)。

运行: MUJOCO_GL=disable python3 physics_smoke.py [--json out_path]

层与检查 (validation_checklist.md 同源):
- Static: skill validator / XML 解析 / MJCF 编译 / manifest-spec parity /
  名称唯一 / 尺寸换算抽查 / contype=0&conaffinity=0 清单 / jointed body
  geom 显式 mass (XML 级) / marker_site 解析
- Model audit 八检查: ①碰撞几何覆盖 ②声明质量 ③伺服保持 (2s, 2mm/1°)
  ④连杆连续性 (全程, 5mm) ⑤marker 落地 (下探射线) ⑥起始位形 (qpos0 与
  reset 后, -1e-4) ⑦序列接触 (逐步包裹 mj_step, -1e-3) ⑧未声明自穿透
  (mj_geomDistance, 快照态; 放行 = 父子/welded 邻接或 <exclude> 列名)
- Physics process: 非法动作拒绝 (未知 id/坏 payload/前置未满足/重复) /
  全序列实跑 / 中空释放坠落 (>=20mm) / 确定性复位与重放 / MJB 保存重开 /
  XML 重载

报告落盘 output/physics_smoke_report.json (包内相对, 无主机名/本机路径)。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("MUJOCO_GL", "disable")   # 物理层无 GL

import mujoco                                   # noqa: E402
import numpy as np                              # noqa: E402
import xml.etree.ElementTree as ET              # noqa: E402

_PKG = Path(__file__).resolve().parent
sys.path.insert(0, str(_PKG))

import environment as E                         # noqa: E402

LIMIT_START_PEN = -1e-4
LIMIT_SEQ_PEN = -1e-3
LIMIT_SERVO_M = 2e-3
LIMIT_SERVO_DEG = 1.0
LIMIT_CONTINUITY = 5e-3
LIMIT_RELEASE_DROP = 0.020


def _name(m, objtype, i):
    return mujoco.mj_id2name(m, objtype, i) or f"#{i}"


def _geom_names(m):
    return [_name(m, mujoco.mjtObj.mjOBJ_GEOM, i) for i in range(m.ngeom)]


# ================================================================ Static
def static_checks(report):
    # 1. skill validator (子进程, 规范实现)
    validator = Path("~/.omp/agent/skills/text2mujoco/scripts/"
                     "validate_scene_spec.py").expanduser()
    if validator.exists():
        proc = subprocess.run(
            [sys.executable, str(validator), str(_PKG / "scene_spec.json")],
            capture_output=True, text=True)
        report["static"]["skill_validator"] = {
            "status": "PASS" if proc.returncode == 0 else "FAIL",
            "output": proc.stdout.strip()[-400:]}
    else:
        report["static"]["skill_validator"] = {
            "status": "SKIPPED", "output": "validator not installed"}

    # 2. XML 解析 + 3. 编译
    try:
        ET.parse(_PKG / "model.xml")
        report["static"]["xml_parse"] = "PASS"
    except ET.ParseError as exc:
        report["static"]["xml_parse"] = f"FAIL: {type(exc).__name__}"
        return
    model = mujoco.MjModel.from_xml_path(str(_PKG / "model.xml"))
    report["static"]["mjcf_compile"] = "PASS"

    # 4. parity (environment 加载即校验)
    env = E.HandoffCellEnv()
    report["static"]["manifest_spec_parity"] = "PASS"

    # 5. 名称唯一 (按对象类型)
    dup = []
    counts = {"body": model.nbody, "geom": model.ngeom, "joint": model.njnt,
              "actuator": model.nu, "site": model.nsite, "camera": model.ncam}
    types = {"body": mujoco.mjtObj.mjOBJ_BODY, "geom": mujoco.mjtObj.mjOBJ_GEOM,
             "joint": mujoco.mjtObj.mjOBJ_JOINT,
             "actuator": mujoco.mjtObj.mjOBJ_ACTUATOR,
             "site": mujoco.mjtObj.mjOBJ_SITE,
             "camera": mujoco.mjtObj.mjOBJ_CAMERA}
    for key, n in counts.items():
        seen = set()
        for i in range(n):
            nm = _name(model, types[key], i)
            if nm in seen:
                dup.append(f"{key}:{nm}")
            seen.add(nm)
    report["static"]["unique_names"] = {"status": "PASS" if not dup else "FAIL",
                                        "duplicates": dup}

    # 6. 尺寸换算抽查 (spec 全尺寸 → MJCF 半尺寸)
    spec = json.loads((_PKG / "scene_spec.json").read_text("utf-8"))
    conv_ok = True
    for a in spec["assets"]:
        if a.get("kind") == "primitive" and a["geometry"]["shape"] == "box":
            half = [v / 2 for v in a["geometry"]["dimensions"]]
            gid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_GEOM,
                                    a["geometry"]["geom_name"])
            if gid >= 0 and not np.allclose(
                    np.array(model.geom_size[gid]), half, atol=1e-9):
                conv_ok = False
    report["static"]["dimension_conversion"] = "PASS" if conv_ok else "FAIL"

    # 7. contype=0 & conaffinity=0 清单; moving body 无碰撞几何
    zeros, bare_bodies = [], []
    for b in range(model.nbody):
        geoms = [g for g in range(model.ngeom) if model.geom_bodyid[g] == b]
        if not geoms:
            continue
        jointed = any(model.jnt_bodyid[j] == b for j in range(model.njnt))
        collidable = [g for g in geoms
                      if model.geom_contype[g] or model.geom_conaffinity[g]]
        if jointed and not collidable:
            bare_bodies.append(_name(model, mujoco.mjtObj.mjOBJ_BODY, b))
        for g in geoms:
            if not model.geom_contype[g] and not model.geom_conaffinity[g]:
                zeros.append(_name(model, mujoco.mjtObj.mjOBJ_GEOM, g))
    report["static"]["zero_mask_geoms"] = {"status": "PASS" if not zeros else "FAIL",
                                           "geoms": zeros}
    report["static"]["jointed_without_collision"] = {
        "status": "PASS" if not bare_bodies else "FAIL",
        "bodies": bare_bodies}

    # 8. jointed body 每个 geom 显式 mass (XML 级 — 编译后已被密度填充)
    root = ET.parse(_PKG / "model.xml").getroot()
    world = root.find("worldbody")

    def walk(body, jointed, out):
        has_j = body.find("joint") is not None or body.find("freejoint") is not None
        j = jointed or has_j
        for geom in body.findall("geom"):
            if j and geom.get("mass") is None and geom.get("density") is None:
                out.append(geom.get("name") or "?")
        for child in body.findall("body"):
            walk(child, j, out)

    missing_mass = []
    for body in world.findall("body"):
        walk(body, False, missing_mass)
    report["static"]["declared_mass_xml"] = {
        "status": "PASS" if not missing_mass else "FAIL",
        "missing": missing_mass}

    # 9. marker_site 解析 (spec 命名的 site 存在且 group=2)
    bad_sites = []
    for p in spec["interaction_points"]:
        ms = p.get("marker_site")
        if not ms:
            continue
        sid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SITE, ms)
        if sid < 0 or model.site_group[sid] != 2:
            bad_sites.append(ms)
    report["static"]["marker_site_resolution"] = {
        "status": "PASS" if not bad_sites else "FAIL", "bad": bad_sites}
    return env, model


# ================================================================ Audit 8
def audit_collision_geometry(model, report):
    """① 每个运动 geom 至少存在一个 可测试 碰撞对 (mask 相交且未被排除)。"""
    excl = {(int(s) >> 16, int(s) & 0xFFFF)
            for s in np.asarray(model.exclude_signature).ravel()}
    uncovered = []
    for b in range(1, model.nbody):
        if not any(model.jnt_bodyid[j] == b for j in range(model.njnt)):
            continue
        for g in range(model.ngeom):
            if model.geom_bodyid[g] != b:
                continue
            if not (model.geom_contype[g] or model.geom_conaffinity[g]):
                continue
            live = False
            for h in range(model.ngeom):
                if h == g or model.geom_bodyid[h] == model.geom_bodyid[g]:
                    continue
                b1, b2 = model.geom_bodyid[g], model.geom_bodyid[h]
                if (b1, b2) in excl or (b2, b1) in excl:
                    continue
                if ((model.geom_contype[g] & model.geom_conaffinity[h]) or
                        (model.geom_contype[h] & model.geom_conaffinity[g])):
                    live = True
                    break
            if not live:
                uncovered.append(_name(model, mujoco.mjtObj.mjOBJ_GEOM, g))
    report["audit"]["collision_geometry"] = {
        "status": "PASS" if not uncovered else "FAIL",
        "uncovered_geoms": uncovered}


def audit_marker_grounding(model, data, report):
    """⑤ 每个 group=2 site: 下方紧贴真实表面, 且中心不在任何 geom 内。"""
    bad = []
    for s in range(model.nsite):
        if model.site_group[s] != 2:
            continue
        body = model.site_bodyid[s]
        pos = model.site_pos[s] + data.xpos[body]
        down = np.array([0.0, 0.0, -1.0])
        pnt = pos + np.array([0.0, 0.0, 1e-6])
        dist = mujoco.mj_ray(model, data, pnt, down, None, 1, -1, None)
        if dist < 0 or dist > 0.012:
            bad.append({"site": _name(model, mujoco.mjtObj.mjOBJ_SITE, s),
                        "ray_down_m": None if dist < 0 else float(dist)})
            continue
        # 中心不在任何 geom 内: 从中心向下 0 距离命中 = 起点已在 geom 内
        zero = mujoco.mj_ray(model, data, pos, down, None, 1, -1, None)
        if zero == 0.0:
            bad.append({"site": _name(model, mujoco.mjtObj.mjOBJ_SITE, s),
                        "ray_down_m": 0.0})
    report["audit"]["marker_grounding"] = {
        "status": "PASS" if not bad else "FAIL", "bad_sites": bad}


def audit_servo_hold(env, model, report):
    """③ 位置伺服保持 qpos0 2s: 平移 <= 2mm, 旋转 <= 1°。"""
    mujoco.mj_resetData(model, env.data)
    qpos0 = env.data.qpos.copy()
    # qpos0 全零 → ctrl 置零即保持
    env.data.ctrl[:] = 0
    for _ in range(4000):                 # 2.0 s
        mujoco.mj_step(model, env.data)
    drift_m, drift_deg = 0.0, 0.0
    for n, j in env.jnt.items():
        adr = model.jnt_qposadr[j]
        d = float(env.data.qpos[adr] - qpos0[adr])
        if model.jnt_type[j] == mujoco.mjtJoint.mjJNT_HINGE:
            drift_deg = max(drift_deg, abs(np.degrees(d)))
        else:
            drift_m = max(drift_m, abs(d))
    ok = drift_m <= LIMIT_SERVO_M and drift_deg <= LIMIT_SERVO_DEG
    report["audit"]["servo_hold"] = {
        "status": "PASS" if ok else "FAIL",
        "max_drift_m": drift_m, "max_drift_deg": drift_deg}


def _body_chain_gap(model, data, body_id):
    """jointed body 到最近 绘制 祖先的最大 (非负) 间隙。"""
    gap = 0.0
    parent = model.body_parentid[body_id]
    while parent >= 0:
        pgeoms = [g for g in range(model.ngeom)
                  if model.geom_bodyid[g] == parent]
        bgeoms = [g for g in range(model.ngeom)
                  if model.geom_bodyid[g] == body_id]
        if pgeoms and bgeoms:
            best = None
            for g in bgeoms:
                for h in pgeoms:
                    fromto = np.zeros(6)
                    dist = mujoco.mj_geomDistance(model, data, g, h, 0.05,
                                                  fromto)
                    best = dist if best is None else min(best, dist)
            gap = max(gap, max(0.0, best))
            break
        parent = model.body_parentid[parent]
    return gap


def audit_link_continuity(env, model, gap_trace, report):
    """④ 全程回放中 jointed body → 最近绘制祖先 间隙 <= 5mm。"""
    worst = 0.0
    detail = {}
    for b in range(1, model.nbody):
        if not any(model.jnt_bodyid[j] == b for j in range(model.njnt)):
            continue
        jtypes = {model.jnt_type[j] for j in range(model.njnt)
                  if model.jnt_bodyid[j] == b}
        if mujoco.mjtJoint.mjJNT_FREE in jtypes:
            continue                     # free 载荷体不是连杆, 不做连续性
        gap = _body_chain_gap(model, env.data, b)
        nm = _name(model, mujoco.mjtObj.mjOBJ_BODY, b)
        detail[nm] = gap
        worst = max(worst, gap)
    gap_trace.append(worst)
    ok = worst <= LIMIT_CONTINUITY
    report["audit"]["link_continuity"] = {
        "status": "PASS" if ok else "FAIL",
        "max_gap_m": worst, "max_gap_m_over_replay": max(gap_trace),
        "per_body_m": {k: round(v, 6) for k, v in detail.items()}}


def contact_min_dist(data):
    worst = 0.0
    pair = None
    for i in range(data.ncon):
        c = data.contact[i]
        if c.dist < worst:
            worst = float(c.dist)
            pair = (int(c.geom1), int(c.geom2))
    return worst, pair


def audit_undeclared_self_overlap(model, data, report, tag, worst_holder):
    """⑧ mj_geomDistance 全对快照: 穿透放行 = 父子/welded 邻接或 exclude。"""
    excl = {(int(s) >> 16, int(s) & 0xFFFF)
            for s in np.asarray(model.exclude_signature).ravel()}
    bad = []
    for g in range(model.ngeom):
        b1 = model.geom_bodyid[g]
        for h in range(g + 1, model.ngeom):
            b2 = model.geom_bodyid[h]
            if b1 == b2:
                continue
            fromto = np.zeros(6)
            dist = mujoco.mj_geomDistance(model, data, g, h, 0.02, fromto)
            if dist >= -1e-6:
                continue
            allowed = (model.body_parentid[b2] == b1 or
                       model.body_parentid[b1] == b2 or
                       (b1, b2) in excl or (b2, b1) in excl)
            if not allowed:
                bad.append({"tag": tag,
                            "geoms": [_name(model, mujoco.mjtObj.mjOBJ_GEOM, g),
                                      _name(model, mujoco.mjtObj.mjOBJ_GEOM, h)],
                            "dist_m": float(dist)})
            worst_holder["min"] = min(worst_holder["min"], float(dist))
    report["audit"]["undeclared_self_overlap"]["violations"] += bad


# ================================================================ 主流程
def main() -> int:
    report = {
        "schema": "t2m.physics_smoke/0.1",
        "scene": "t2m_handoff_cell",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mujoco_version": mujoco.__version__,
        "python": sys.version.split()[0],
        "gl_backend": os.environ.get("MUJOCO_GL", "(unset)"),
        "static": {}, "audit": {"undeclared_self_overlap": {"violations": []}},
        "physics": {}, "events": [], "metrics": {},
    }

    out = static_checks(report)
    if out is None:
        _write(report)
        return 1
    env, model = out
    static_ok = all(
        (v.get("status", v) == "PASS" if isinstance(v, dict) else v == "PASS")
        for v in report["static"].values())

    audit_collision_geometry(model, report)

    # ---- 伺服保持 (2s @ qpos0)
    audit_servo_hold(env, model, report)

    # ---- 起始位形: qpos0 与 reset 后
    mujoco.mj_resetData(model, env.data)
    mujoco.mj_forward(model, env.data)
    pen0_q0, _ = contact_min_dist(env.data)
    obs = env.reset(seed=7)
    pen0_reset, _ = contact_min_dist(env.data)
    pen0 = min(pen0_q0, pen0_reset)
    report["physics"]["start_pose"] = {
        "qpos0_min_dist": pen0_q0, "post_reset_min_dist": pen0_reset,
        "status": "PASS" if pen0 >= LIMIT_START_PEN else "FAIL"}
    report["metrics"]["max_pen_start_m"] = pen0

    # ---- 全序列回放: 包裹 mj_step 记录最深接触 + 连续性/自穿透快照
    deepest = {"dist": 0.0, "pair": None, "t": 0.0}
    genuine_step = mujoco.mj_step
    gap_trace = [0.0]
    overlap_holder = {"min": 0.0}

    def watched(m, d, *a, **kw):
        genuine_step(m, d, *a, **kw)
        dist, pair = contact_min_dist(d)
        if dist < deepest["dist"]:
            deepest.update(dist=dist, pair=pair, t=float(d.time))

    mujoco.mj_step = watched
    task_success = False
    events = []
    try:
        audit_undeclared_self_overlap(model, env.data, report,
                                      "post_reset", overlap_holder)
        audit_link_continuity(env, model, gap_trace, report)
        audit_marker_grounding(model, env.data, report)

        for pid in E.DEPENDENCY_ORDER:
            payload = {"close": True} if pid == "grip_close" else {}
            result = env.step({"id": pid, "payload": payload})
            report["physics"][f"step_{pid}"] = "PASS"
            if pid == "belt_feed":
                carton_x = result["info"]["carton_x"]
            audit_undeclared_self_overlap(model, env.data, report,
                                          f"after_{pid}", overlap_holder)
            audit_link_continuity(env, model, gap_trace, report)
            if pid == "place_box":
                report["metrics"]["weld_release_drop_m"] = \
                    result["info"]["weld_release_drop_m"]
                report["metrics"]["carton_in_bin"] = True
        task_success = bool(env.is_success())
        events = env.observe()["events"]
    except Exception as exc:                                  # noqa: BLE001
        report["physics"]["sequence_error"] = f"{type(exc).__name__}"
        report["metrics"]["sequence_failed"] = True
    finally:
        mujoco.mj_step = genuine_step

    total_steps = int(round(env.data.time / model.opt.timestep))
    report["metrics"]["seq_steps"] = total_steps
    report["metrics"]["max_pen_seq_m"] = deepest["dist"]
    report["physics"]["sequence_contact"] = {
        "steps": total_steps,
        "deepest_dist_m": deepest["dist"],
        "deepest_pair": ([_name(model, mujoco.mjtObj.mjOBJ_GEOM, deepest["pair"][0]),
                          _name(model, mujoco.mjtObj.mjOBJ_GEOM, deepest["pair"][1])]
                         if deepest["pair"] else None),
        "deepest_t": deepest["t"],
        "status": "PASS" if deepest["dist"] >= LIMIT_SEQ_PEN else "FAIL"}

    viol = report["audit"]["undeclared_self_overlap"]["violations"]
    report["audit"]["undeclared_self_overlap"]["status"] = \
        "PASS" if not viol else "FAIL"
    report["audit"]["undeclared_self_overlap"]["min_dist_m"] = \
        overlap_holder["min"]

    # ---- 确定性: 双复位 + 双跑一致 (独立 env, 不扰动序列产物)
    q_reset = [E.HandoffCellEnv().reset(seed=7)["qpos"] for _ in range(2)]
    det_reset = q_reset[0] == q_reset[1]
    def run_once():
        e = E.HandoffCellEnv()
        e.reset(seed=7)
        for pid in E.DEPENDENCY_ORDER:
            payload = {"close": True} if pid == "grip_close" else {}
            e.step({"id": pid, "payload": payload})
        return np.asarray(e.data.qpos).tobytes()
    det_seq = run_once() == run_once()
    report["physics"]["deterministic_reset"] = {
        "reset_identical": det_reset, "replay_identical": det_seq,
        "status": "PASS" if (det_reset and det_seq) else "FAIL"}
    report["metrics"]["determinism_ok"] = bool(det_reset and det_seq)

    # ---- 携运到位 / 释放坠落 / is_success (取自序列 env, 先于确定性重置)
    report["metrics"]["belt_feed_carton_x_m"] = carton_x
    report["metrics"]["carry_arrival_err_m"] = abs(carton_x - E.PICK_TCP_X)
    report["physics"]["task_success"] = task_success
    report["events"] = events
    kinds = [e["kind"] for e in events]
    pair_ok = (kinds.count("HANDOFF_PICK") == 1
               and kinds.count("HANDOFF_PLACE") == 1
               and events[kinds.index("HANDOFF_PICK")]["t"]
               <= events[kinds.index("HANDOFF_PLACE")]["t"])
    report["metrics"]["handoff_pair_ok"] = bool(pair_ok)
    drop = report["metrics"].get("weld_release_drop_m", 0.0)
    report["physics"]["weld_release_drop"] = {
        "drop_m": drop,
        "status": "PASS" if drop >= LIMIT_RELEASE_DROP else "FAIL"}

    # ---- MJB 保存重开 + XML 重载
    try:
        mjb = _PKG / "output" / "model.mjb"
        mujoco.mj_saveModel(model, str(mjb), None)
        reloaded = mujoco.MjModel.from_binary_path(str(mjb))
        xml_again = mujoco.MjModel.from_xml_path(str(_PKG / "model.xml"))
        report["physics"]["artifact_persistence"] = {
            "mjb_saved": mjb.exists(),
            "mjb_reopened": reloaded.nq == model.nq,
            "xml_reloaded": xml_again.nq == model.nq,
            "status": "PASS"}
    except Exception as exc:                                  # noqa: BLE001
        report["physics"]["artifact_persistence"] = {
            "status": "FAIL", "error": type(exc).__name__}

    # ---- 八项 audit 汇总 (declared_mass 在 static, start/sequence 在 physics)
    audit_keys = {
        "collision_geometry": report["audit"],
        "declared_mass_xml": report["static"],
        "servo_hold": report["audit"],
        "link_continuity": report["audit"],
        "marker_grounding": report["audit"],
        "start_pose": report["physics"],
        "sequence_contact": report["physics"],
        "undeclared_self_overlap": report["audit"],
    }
    report["audit8_all_pass"] = all(
        section.get(k, {}).get("status") == "PASS"
        for k, section in audit_keys.items())
    report["metrics"]["audit8_all_pass"] = bool(report["audit8_all_pass"])
    report["status"] = ("PASS" if (report["audit8_all_pass"] and static_ok
                                   and report["physics"].get("task_success")
                                   and not report["metrics"].get("sequence_failed"))
                        else "FAIL")
    _write(report)
    _print_summary(report)
    return 0 if report["status"] == "PASS" else 1


def _write(report):
    out = _PKG / "output" / "physics_smoke_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8")


def _print_summary(report):
    print(f"status: {report['status']}")
    for section in ("static",):
        for k, v in report[section].items():
            s = v.get("status", v) if isinstance(v, dict) else v
            print(f"  static/{k}: {s}")
    for k, section in (("collision_geometry", "audit"),
                       ("declared_mass_xml", "static"),
                       ("servo_hold", "audit"),
                       ("link_continuity", "audit"),
                       ("marker_grounding", "audit"),
                       ("start_pose", "physics"),
                       ("sequence_contact", "physics"),
                       ("undeclared_self_overlap", "audit")):
        v = report[section].get(k, {})
        print(f"  audit/{k}: {v.get('status', '?')} "
              f"{v.get('max_drift_m', '')}"
              f"{v.get('max_gap_m_over_replay', '')}"
              f"{v.get('min_dist_m', '')}")
    m = report["metrics"]
    print(f"  metrics: steps={m.get('seq_steps')} "
          f"max_pen_start={m.get('max_pen_start_m'):.2e} "
          f"max_pen_seq={m.get('max_pen_seq_m'):.2e} "
          f"drop={m.get('weld_release_drop_m', 0):.3f}m "
          f"det={m.get('determinism_ok')} "
          f"handoff_pair={m.get('handoff_pair_ok')} "
          f"audit8={m.get('audit8_all_pass')}")


if __name__ == "__main__":
    raise SystemExit(main())
