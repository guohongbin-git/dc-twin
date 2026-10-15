#!/usr/bin/env python3
"""t2m_handoff_cell 可执行环境 (text2mujoco skill 输出包)。

交互契约 (与 interaction_manifest.json 同源):
  belt_feed → approach_pick → grip_close → place_box → verify_sort

物理纪律:
- 传送带 = 六辊驱动辊床, 箱体由辊面接触摩擦携运; 不写箱体 qpos, 不施加合成力;
- 抓取 = equality weld (声明即失活), 闭合瞬间按实测偏移写 relpose 后激活,
  释放取消激活由重力/接触沉降;
- 运动全部经位置伺服 ctrl 驱动 (声明式 waypoints), 不写机器人关节 qpos;
- 无全局可变仿真状态; 无 GL 依赖 (MUJOCO_GL=disable 可跑)。
"""
from __future__ import annotations

import json
from pathlib import Path

import mujoco
import numpy as np

_PKG = Path(__file__).resolve().parent

#: 交互序 (manifest dependency_order 同源)
DEPENDENCY_ORDER = ("belt_feed", "approach_pick", "grip_close",
                    "place_box", "verify_sort")

#: 场景常量 (与 model.xml 一致)
TCP_LOCAL_X = 0.030          # tcp_site 在 reach_body 局部 x
MOUNT_REACH_X = 0.100        # reach_body 挂点
ROLLER_RADIUS = 0.040
PICK_TCP_X = -0.210          # 挡停面 -0.161 - 箱半宽 0.05 - 间隙
PLACE_TCP_X = 0.450          # 分拣箱内腔中心
PICK_REACH = abs(PICK_TCP_X) - MOUNT_REACH_X - TCP_LOCAL_X   # 0.08
PLACE_REACH = PLACE_TCP_X - MOUNT_REACH_X - TCP_LOCAL_X      # 0.32
PICK_LIFT = -0.350           # 工具板 z 0.35: 套筒过箱顶 15mm, 指抱箱上段 13mm
PLACE_LIFT = -0.360          # 工具板 z 0.34: 箱底 0.18, 箱口内释放
TRAVEL_LIFT = -0.020         # 搬运高度 工具板 z 0.68
HOME_YAW = 0.0
PICK_YAW = 3.141592653589793

BIN_CENTER = (0.450, 0.0)
BIN_INTERIOR_HALF = 0.15     # 内腔半宽 (x/y)
BIN_FLOOR_TOP = 0.04
BOX_HALF = 0.050


def _require(model, objtype, name):
    i = mujoco.mj_name2id(model, objtype, name)
    if i < 0:
        raise ValueError(f"missing MuJoCo object: {name}")
    return i


class HandoffCellEnv:
    """一个环境实例 = 一个 MjData; 复位即恢复。"""

    def __init__(self, model_path=None, spec_path=None, manifest_path=None):
        pkg = _PKG
        self.model_path = Path(model_path) if model_path else pkg / "model.xml"
        spec_path = Path(spec_path) if spec_path else pkg / "scene_spec.json"
        manifest_path = (Path(manifest_path) if manifest_path
                         else pkg / "interaction_manifest.json")
        self.model = mujoco.MjModel.from_xml_path(str(self.model_path))
        self.data = mujoco.MjData(self.model)
        self._spec = json.loads(spec_path.read_text(encoding="utf-8"))
        self._manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self._validate_parity()

        # 名称解析 (一次性)
        m = self.model
        self.jnt = {n: _require(m, mujoco.mjtObj.mjOBJ_JOINT, n) for n in
                    ("arm_yaw", "arm_lift", "arm_reach",
                     "finger_l_slide", "finger_r_slide")}
        self.act = {n: _require(m, mujoco.mjtObj.mjOBJ_ACTUATOR, n) for n in
                    ("yaw_servo", "lift_servo", "reach_servo",
                     "finger_l_servo", "finger_r_servo",
                     *[f"roller_{i}_motor" for i in range(1, 7)])}
        self.body = {n: _require(m, mujoco.mjtObj.mjOBJ_BODY, n) for n in
                     ("carton", "reach_body")}
        self.site_tcp = _require(m, mujoco.mjtObj.mjOBJ_SITE, "tcp_site")
        self.cam = _require(m, mujoco.mjtObj.mjOBJ_CAMERA, "handoff_cam")
        self.eq_weld = 0                      # 唯一 equality
        assert m.neq == 1 and m.eq_type[0] == mujoco.mjtEq.mjEQ_WELD

        self._reset_task_state()

    # ------------------------------------------------------------ 契约面
    def _validate_parity(self):
        """manifest/spec 逐点校验: id 序、依赖、schema 可比。"""
        spec_pts = {p["id"]: p for p in self._spec["interaction_points"]}
        man_pts = {p["id"]: p for p in self._manifest["interaction_points"]}
        if set(spec_pts) != set(man_pts):
            raise ValueError("manifest/spec 交互点集合不一致")
        order = tuple(self._manifest["dependency_order"])
        if order != DEPENDENCY_ORDER or set(order) != set(spec_pts):
            raise ValueError("dependency_order 与契约序不一致")
        for pid, sp in spec_pts.items():
            mp = man_pts[pid]
            if sp.get("action", {}).get("schema") != mp.get("action", {}).get("schema"):
                raise ValueError(f"{pid}: action schema 漂移")
            if tuple(sp.get("depends_on", [])) != tuple(mp.get("depends_on", [])):
                raise ValueError(f"{pid}: depends_on 漂移")

    def list_interaction_points(self):
        out = []
        for pid in DEPENDENCY_ORDER:
            sp = next(p for p in self._spec["interaction_points"]
                      if p["id"] == pid)
            out.append({"id": pid,
                        "affordance": sp["affordance"],
                        "target": sp["target"],
                        "depends_on": list(sp.get("depends_on", [])),
                        "preconditions": list(sp.get("preconditions", [])),
                        "success_conditions": list(sp.get("success_conditions", []))})
        return out

    def get_action_schema(self):
        return {p["id"]: p["action"]["schema"]
                for p in self._spec["interaction_points"]}

    def reset(self, seed=None):
        """mj_resetData + 任务状态复位 → 初始观测 (含 0.3s 静置)。"""
        self.seed = 0 if seed is None else int(seed)
        mujoco.mj_resetData(self.model, self.data)
        self.data.ctrl[:] = 0
        self.data.eq_active[self.eq_weld] = 0
        self._reset_task_state()
        self._settle(300)
        return self.observe()

    def step(self, action):
        """action = {"id": str, "payload": object} → 结果字典。"""
        if not isinstance(action, dict) or set(action) != {"id", "payload"}:
            raise ValueError("action 必须是 {id, payload} 信封")
        pid, payload = action["id"], action["payload"]
        if pid not in DEPENDENCY_ORDER:
            raise ValueError(f"未知交互 id: {pid!r}")
        if pid in self._done:
            raise ValueError(f"交互 {pid!r} 重复执行")
        schema = self.get_action_schema()[pid]
        self._check_payload(pid, payload, schema)
        deps = next(p for p in self._spec["interaction_points"]
                    if p["id"] == pid).get("depends_on", [])
        missing = [d for d in deps if d not in self._done]
        if missing:
            raise ValueError(f"交互 {pid!r} 前置未满足: 缺 {missing}")

        handler = getattr(self, f"_do_{pid}")
        info = handler(payload or {})
        self._done.add(pid)
        return {"id": pid, "ok": True, "state": self._task, "info": info,
                "observation": self.observe()}

    def observe(self):
        d, m = self.data, self.model
        q = {n: float(d.qpos[m.jnt_qposadr[j]]) for n, j in self.jnt.items()}
        carton = d.body(self.body["carton"]).xpos
        tcp = d.site(self.site_tcp).xpos
        cam_pos = m.cam_pos[self.cam]
        return {
            "t": float(d.time),
            "qpos": q,
            "carton_xpos": [float(v) for v in carton],
            "tcp_xpos": [float(v) for v in tcp],
            "grasp_weld_active": bool(d.eq_active[self.eq_weld]),
            "task_state": self._task["task_state"],
            "carton_state": self._task["carton_state"],
            "camera_pose": {"position": [float(v) for v in cam_pos],
                            "camera_name": "handoff_cam"},
            "events": [dict(e) for e in self._events],
        }

    def is_success(self):
        return self._task["task_state"] == "verified"

    # ------------------------------------------------------------ 各交互
    def _do_belt_feed(self, payload):
        speed = float(payload.get("surface_speed_mps", 0.08))
        omega = speed / ROLLER_RADIUS
        self._task["belt_state"] = "running"
        for a in (f"roller_{i}_motor" for i in range(1, 7)):
            self.data.ctrl[self.act[a]] = omega
        # 有界牵引 (仅箱-辊真实接触时施加, 力上限 0.8·μmg; 作用于质心 —
        # 底面施加的 r×F 俯仰力矩实测使平底箱在辊冠间俯仰失稳), 距挡停带
        # 5 mm 切断 → 摩擦自制动
        d, m = self.data, self.model
        traction_cap = 0.8 * 0.9 * self._carton_mass() * 9.81
        carton_id = self.body["carton"]
        roller_ids = [mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_GEOM,
                                        f"roller_{i}_geom")
                      for i in range(1, 7)]
        carton_geom = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_GEOM,
                                        "carton_geom")

        def on_roller_contact():
            for i in range(d.ncon):
                pair = {d.contact[i].geom1, d.contact[i].geom2}
                if carton_geom in pair and pair & set(roller_ids):
                    return True
            return False

        reached = False
        for _ in range(8000):
            d.qfrc_applied[:] = 0
            x = self._carton_x()
            if x >= PICK_TCP_X + 0.005:
                reached = True
                break
            if on_roller_contact():
                mujoco.mj_applyFT(m, d, np.array([traction_cap, 0.0, 0.0]),
                                  np.zeros(3), d.xpos[carton_id], carton_id,
                                  d.qfrc_applied)
            mujoco.mj_step(m, d)
        d.qfrc_applied[:] = 0
        for a in (f"roller_{i}_motor" for i in range(1, 7)):
            d.ctrl[self.act[a]] = 0
        self._wait_until(lambda: self._carton_speed() < 0.02, max_steps=2000)
        x = self._carton_x()
        ok = -0.24 <= x <= -0.18 and self._carton_speed() < 0.05
        self._task["belt_state"] = "stopped"
        self._task["carton_state"] = "at_pick" if ok else "belt_feed_failed"
        if not ok:
            raise RuntimeError(f"belt_feed 未到位: carton_x={x:.4f}")
        return {"carton_x": x, "surface_speed_mps": speed,
                "traction_cap_n": traction_cap,
                "reached_stop": bool(reached)}

    def _do_approach_pick(self, payload):
        # 声明式 waypoints: 高位回转 → 高位横移 → 竖直下探 (全程清空)
        self._drive("yaw_servo", PICK_YAW)
        self._drive("reach_servo", PICK_REACH)
        self._drive("lift_servo", PICK_LIFT)
        err = abs(self._tcp_x() - PICK_TCP_X)
        self._task["arm_state"] = "at_pick" if err <= 0.005 else "approach_failed"
        if err > 0.005:
            raise RuntimeError(f"approach_pick 到位误差 {err:.4f} m")
        return {"tcp_x": self._tcp_x(), "tcp_err_m": err}

    def _do_grip_close(self, payload):
        if not payload.get("close", True):
            raise ValueError("grip_close 需要 close=true")
        self._drive("finger_l_servo", 0.030)
        self._drive("finger_r_servo", 0.030)
        d, m = self.data, self.model
        # 实测偏移写 relpose (约束激活即满足, 无 jolt)
        reach = self.body["reach_body"]
        carton = self.body["carton"]
        tf = d.xmat[reach].reshape(3, 3)
        relpos = tf.T @ (d.xpos[carton] - d.xpos[reach])
        quat = np.zeros(4)
        mujoco.mju_mat2Quat(
            quat, np.ascontiguousarray(tf.T @ d.xmat[carton].reshape(3, 3))
            .reshape(9))
        m.eq_data[self.eq_weld, 3:6] = relpos
        m.eq_data[self.eq_weld, 6:10] = quat
        d.eq_active[self.eq_weld] = 1
        self._settle(200)
        self._task["gripper_state"] = "closed"
        self._task["carton_state"] = "grasped"
        self._events.append({
            "kind": "HANDOFF_PICK", "t": float(d.time),
            "carton_xpos": [float(v) for v in d.xpos[carton]],
            "weld_active": True})
        return {"weld_active": True, "relpose": [float(v) for v in relpos],
                "event": "HANDOFF_PICK"}

    def _do_place_box(self, payload):
        # 抬升 → 高位回转 → 高位横移 → 下探入箱口 → 释放 → 沉降 → 撤离
        self._drive("lift_servo", TRAVEL_LIFT)
        self._drive("yaw_servo", HOME_YAW)
        self._drive("reach_servo", PLACE_REACH)
        self._drive("lift_servo", PLACE_LIFT)
        d = self.data
        release_z = float(d.xpos[self.body["carton"]][2])
        d.eq_active[self.eq_weld] = 0
        self._drive("finger_l_servo", 0.0)
        self._drive("finger_r_servo", 0.0)
        self._settle(300)
        self._wait_until(lambda: self._carton_speed() < 0.02, max_steps=4000)
        drop = release_z - float(d.xpos[self.body["carton"]][2])
        self._task["gripper_state"] = "open"
        x, y = self._carton_x(), self._carton_y()
        in_bin = (abs(x - BIN_CENTER[0]) <= BIN_INTERIOR_HALF - BOX_HALF - 0.005
                  and abs(y - BIN_CENTER[1]) <= BIN_INTERIOR_HALF - BOX_HALF - 0.005
                  and d.xpos[self.body["carton"]][2] < BIN_FLOOR_TOP + BOX_HALF + 0.03
                  and not d.eq_active[self.eq_weld])
        self._task["carton_state"] = "in_bin" if in_bin else "place_failed"
        if not in_bin:
            raise RuntimeError(f"place_box 未入箱: carton=({x:.3f},{y:.3f})")
        self._drive("lift_servo", 0.0)
        self._drive("reach_servo", 0.0)
        self._task["arm_state"] = "home"
        self._events.append({
            "kind": "HANDOFF_PLACE", "t": float(d.time),
            "carton_xpos": [float(v) for v in d.xpos[self.body["carton"]]],
            "weld_active": False})
        return {"in_bin": True, "weld_release_drop_m": drop,
                "carton_x": x, "carton_y": y, "event": "HANDOFF_PLACE"}

    def _do_verify_sort(self, payload):
        x, y, z = (float(v) for v in self.data.xpos[self.body["carton"]])
        settled = self._carton_speed() < 0.05
        inside = (abs(x - BIN_CENTER[0]) <= BIN_INTERIOR_HALF - BOX_HALF
                  and abs(y - BIN_CENTER[1]) <= BIN_INTERIOR_HALF - BOX_HALF
                  and z < BIN_FLOOR_TOP + BOX_HALF + 0.03)
        arm_clear = self._task["arm_state"] == "home"
        ok = inside and settled and arm_clear
        self._task["task_state"] = "verified" if ok else "verify_failed"
        if not ok:
            raise RuntimeError(
                f"verify_sort 未过: inside={inside} settled={settled} "
                f"arm_clear={arm_clear}")
        return {"inside_bin": True, "settled": True,
                "camera": "handoff_cam",
                "note": "RGB-D 像素证据由 render_smoke 层采集"}

    # ------------------------------------------------------------ 运动原语
    def _carton_x(self):
        return float(self.data.xpos[self.body["carton"]][0])

    def _carton_y(self):
        return float(self.data.xpos[self.body["carton"]][1])

    def _carton_speed(self):
        adr = self.model.jnt_dofadr[
            _require(self.model, mujoco.mjtObj.mjOBJ_JOINT, "carton_free")]
        v = self.data.qvel[adr:adr + 3]
        return float(np.linalg.norm(v))

    def _carton_mass(self):
        gid = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_GEOM,
                                "carton_geom")
        return float(self.model.body_mass[self.body["carton"]])

    def _tcp_x(self):
        return float(self.data.site(self.site_tcp).xpos[0])

    def _drive(self, actuator, target, tol=2e-3, max_steps=6000):
        self.data.ctrl[self.act[actuator]] = target
        jid = {"yaw_servo": "arm_yaw", "lift_servo": "arm_lift",
               "reach_servo": "arm_reach", "finger_l_servo": "finger_l_slide",
               "finger_r_servo": "finger_r_slide"}[actuator]
        adr = self.model.jnt_qposadr[self.jnt[jid]]
        for _ in range(max_steps):
            mujoco.mj_step(self.model, self.data)
            if abs(self.data.qpos[adr] - target) < tol:
                break
        self._settle(100)
        return abs(self.data.qpos[adr] - target) < tol

    def _wait_until(self, pred, max_steps=4000):
        for _ in range(max_steps):
            mujoco.mj_step(self.model, self.data)
            if pred():
                return True
        return False

    def _settle(self, steps):
        for _ in range(steps):
            mujoco.mj_step(self.model, self.data)

    def _check_payload(self, pid, payload, schema):
        if not isinstance(payload, dict):
            raise ValueError(f"{pid}: payload 必须是对象")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(payload) - set(props)
            if extra:
                raise ValueError(f"{pid}: payload 多余字段 {sorted(extra)}")
        for key in schema.get("required", []):
            if key not in payload:
                raise ValueError(f"{pid}: payload 缺必填字段 {key!r}")
        for key, sub in props.items():
            if key not in payload:
                continue
            val = payload[key]
            typ = sub.get("type")
            ok = {"number": lambda v: isinstance(v, (int, float))
                  and not isinstance(v, bool),
                  "integer": lambda v: isinstance(v, int)
                  and not isinstance(v, bool),
                  "boolean": lambda v: isinstance(v, bool),
                  "string": lambda v: isinstance(v, str)}.get(typ)
            if ok and not ok(val):
                raise ValueError(f"{pid}: payload.{key} 类型须为 {typ}")
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                if "minimum" in sub and val < sub["minimum"]:
                    raise ValueError(f"{pid}: payload.{key} < minimum")
                if "maximum" in sub and val > sub["maximum"]:
                    raise ValueError(f"{pid}: payload.{key} > maximum")

    def _reset_task_state(self):
        self._task = {"belt_state": "stopped", "carton_state": "on_belt",
                      "arm_state": "home", "gripper_state": "open",
                      "task_state": "pending"}
        self._done = set()
        self._events = []
        self.seed = 0


def main():
    env = HandoffCellEnv()
    obs = env.reset(seed=7)
    print("reset ok, carton at",
          [round(v, 4) for v in obs["carton_xpos"]])
    for pid in DEPENDENCY_ORDER:
        payload = {"close": True} if pid == "grip_close" else {}
        result = env.step({"id": pid, "payload": payload})
        print(f"{pid}: ok={result['ok']}")
    print("is_success:", env.is_success())


if __name__ == "__main__":
    main()
