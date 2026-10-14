# 数值判据与阈值 (validation thresholds)

ESR 的数值判据**声明制**: 阈值随回归用例预注册 (四件套 `asserts`), 不
是校验器内建常量 —— ⑤ 消融按用例工件逐条求值。本文档汇总既有样本判据,
并预留与 Text2MuJoCo 校验器阈值的对齐位。

## 1. DetFix 样本 (tests/fixtures/esr/detfix_m2, INC-DETFIX-M2-001)

六层故障链 → 三修复 (FIX-1 lane-lock 联锁 / FIX-2 op_pin / FIX-3
op_unpin), seed=7 determinism:

| 用例 | 断言 | 阈值 | 工件实值 |
|---|---|---|---|
| REG-1 run4 契约回归 | `max_pen_m <= 1.6e-05` | **≤ 0.016 mm** (m 制 1.6e-05) | 1.2e-05 ✓ |
| REG-1 | `op_errors empty` / `violations empty` | 零操作错误/零违规 | `[]` ✓ |
| REG-1 | `determinism_passed eq true` | seed=7 确定性复现 | true ✓ |
| REG-2 pin-hook 回归 | `pin_hook_events eq 0` | 对中夹后零钩挂 | 0 ✓ |
| REG-3 y-recenter 坠托回归 | `drops_in_transit eq 0` | 途中零坠托 | 0 ✓ |
| REG-3 | `all_complete eq true` / `head_on_passed eq true` | 全任务完成/对向通过 | true ✓ |

三个工件快照 (`runs/run4_postfix.json` 等) 全部过断言 —— 离线 ⑤ 回放
绿, 也是 `scripts/esr_detfix_walkthrough.py` 走通 ⑤ 拍的依据。

## 2. Conveyor 样本 (tests/fixtures/esr/conveyor, INC-CONV-L1-001)

判据声明 (incident.json asserts; m 制):

| 用例 | 断言 | 阈值 |
|---|---|---|
| REG-CONV-1 段输送推进 | `max_pen_m <= 0.0025` | ≤ 2.5 mm |
| REG-CONV-1 | `feed_seat_in_rect eq true` | 带端落座入 nest 矩形 |
| REG-CONV-1 | `out_seat_err_m <= 0.005` | 末端落座误差 ≤ 5 mm |
| REG-CONV-2 交接原子性 | `event_pairs_ok eq true` / `handoff_atomic eq true` | in/out 事件对配对 + 落账一一对应 |
| REG-CONV-3 端到端 | `finished_at_slot eq true` / `cycles >= 2` / `reconciled eq true` | 产出行到位/≥2 循环/账实一致 |

注意: 随包工件快照 `runs/run_conv.json` 是**修复前 (pre-FIX) 快照**
(`max_pen_m=0.0265`、`cycles=1` 等, 断言未过属预期) —— 传送带线仍在
收敛中, 以 `scripts/esr_conveyor_walkthrough.py` 真运行刷新工件后 ⑤
回放为准 (其 ① 拍 runner 即把真运行工件导出到事故目录)。

## 3. Text2MuJoCo 对齐位 (预留, 待吸收)

Text2MuJoCo skill 的 physics smoke 用**校验器内建**阈值:

| 场景 | Text2MuJoCo 阈值 | 含义 |
|---|---|---|
| 静态/reset 初态接触 | `dist < -1e-4 m` 即败 | **0.1 mm** 静态穿透 (编译 qpos0 与 post-reset 两个状态都审) |
| 序列全程接触 | 重叠深于 `-1e-3 m` 即败 | **1 mm** 序列穿透 (整段 mj_step 逐帧审, 报告最深接触 geom 对与时刻) |

**状态: 待吸收 —— 见 gate TODO。** 两套口径尚未统一 (ESR 声明制 vs
Text2MuJoCo 内建常量), 该对齐未合入 `dc_twin.esr.gate`; 跟踪入口是
场景进化门的 TODO 项。吸收路径: 以一次 `revision_kind="amend"` 的证据
契约修订, 把 0.1 mm/1 mm 作为预注册指标经门入册 (准入三件套 + 回归
用例, 见 evolution_gate.md), 而不是绕过门直接改判据。在那之前, 新事故
的 `asserts` 建议沿用同量级: 静态/落座类 ≤ 1e-4~2.5e-3 m, 序列类以
既有样本为锚 (DetFix 1.6e-05 m)。

## 4. 求值纪律

- 断言经 `evaluate_assert` 结构化求值, **不 eval 任意表达式**;
- 缺指标 / 缺工件 / 未知算子 / 类型不可比 → 一律按**失败**处理
  (宁严勿漏);
- 断言全过 ⑤ 拍才 passed; ⑤ 未过则 ④ 的进化不得视为生效
  (每一次进化都要过消融门)。
