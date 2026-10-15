# t2m_handoff_cell — text2mujoco→ESR 全链试点场景包

conveyor-to-arm handoff 变体: 六辊驱动辊床送纸箱 → 三关节立柱式机械臂
(yaw/lift/reach + 平行夹爪) 抓取 → 放入分拣箱 → 固定 RGB-D 相机核验。

与 dc-twin 产线互补 (产线 AGV 搬运链之外的单工位交接单元), 由
text2mujoco skill 工作流生成, 并按 ESR 语义接入场景进化门。

## 包内容

| 文件 | 说明 |
| --- | --- |
| `scene_spec.json` | 稳定场景契约 (skill validator PASS) |
| `model.xml` | MJCF (编译 PASS, qpos0 零穿透) |
| `environment.py` | 可执行交互契约 (5 interaction points, 无 GL 依赖) |
| `interaction_manifest.json` | 机器可读契约 (action envelope + 依赖序) |
| `physics_smoke.py` | 物理烟囱 + model audit 八检查 |
| `render_smoke.py` | RGB-D 渲染层 (独立进程, GL 后端) |

## 交互序 (dependency_order)

```text
belt_feed → approach_pick → grip_close → place_box → verify_sort
```

- `belt_feed`: 辊床驱动 (速度伺服) + 有界质心牵引 (接触门控, 0.8·μmg 上限),
  挡停面制动到位; 不写箱体 qpos
- `grip_close`: 夹爪闭合到接触 + equality weld 按实测 relpose 激活
- `place_box`: 抬升→回转→横移→箱口内释放 (weld 失活, 中空释放坠落实测 129mm)

## 运行

```bash
MUJOCO_GL=disable python3 physics_smoke.py          # 物理 + 八项 audit
MUJOCO_GL=glfw python3 render_smoke.py              # macOS 渲染层
mjpython  render_smoke.py                           # 等价 (glfw/cgl)
python3 environment.py                              # 交互契约自检
```

实测 (mujoco 3.14.0, 2026-10-15): 八项 audit 全 PASS —
起始最深穿透 -4.0e-7 m / 序列最深 -6.6e-4 m (限 -1e-3) / 伺服保持漂移
~0 (gravcomp) / 连杆连续性 3 mm (限 5) / marker 全落地 / 无未声明自穿透 /
中空释放坠落 129 mm / 确定性重放一致 / RGB-D 像素变化 1.18e6。

## ESR 接入

回归证据工件: `tests/fixtures/esr/pilot_t2m/runs/run_t2m.json`;
事故记录 (四件套): `tests/fixtures/esr/pilot_t2m/incident.json`;
新节点类型 `T2M_HANDOFF_CELL` + 事件对 `HANDOFF_PICK`/`HANDOFF_PLACE`
经 ESR-Loop 场景进化门落册, 产物在 `reports/esr/pilot_t2m/loop/`。
复现全链: `python3 scripts/esr_t2m_pilot.py`。
