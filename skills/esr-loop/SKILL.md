---
name: esr-loop
description: This skill should be used when the user asks to "crystallize incident", "run ESR loop", "evolve simulation scene", or mentions "ESR 进化门", "技能结晶", "自进化闭环", "证据契约修订", or "场景进化门" — packaging a simulation incident into the four-piece crystallization record and driving the five-beat Evidence-Simulation-Ready (ESR) self-evolution loop with its evolution gate and ablation replay.
---

# ESR-Loop

把一次仿真事故炼成可复用的技能, 并让场景凭运行经验进化 —— SimReady 的
规格由委员会发布后冻结, ESR 的规格由**运行经验持续改写**, 每一次进化都
要过消融门: 进化不靠信仰, 靠消融。实现为 `dc_twin.esr` 包 (crystallize
/ gate / loop 三件), 本 skill 是其 agent 操作面。

## Workflow

按五拍推进 (蓝图 v0.5.1 §18.①); 每拍成功即落账, 失败即停, 断点续跑。

1. **① 运行 (run)**: 确认事故证据在位 —— `incident_dir/incident.json`
   是 ② 拍结晶源, `incident_dir/runs/*.json` 是 ⑤ 拍回放工件。缺省
   (不注入 runner) 即核验既有证据 (`mode=reuse_existing`); 要真跑仿真
   则注入 `runner(cfg) -> summary`。事故目录结构见
   [references/loop_beats.md](references/loop_beats.md) §0。
2. **② 经验结晶 (crystallize)**: 把事件/故障/对账分歧写成四件套
   incident → {根因假设, 修复动作, 回归用例, 证据契约修订}。字段逐条
   语义与语义门 (动作必有回归保护 / 契约修订恒 draft) 见
   [references/crystallization_format.md](references/crystallization_format.md)。
   写完先跑 `python3 scripts/validate_incident.py <incident_dir>`,
   校验器返回错误清单 (不抛异常), 迭代修完再让
   `dc_twin.esr.crystallize.crystallize()` 定档; 定档即进 ③。
3. **③ 知识沉淀 (consolidate)**: 四件套 → Voyager 式技能条目
   (前置→执行→证据→后置), 落盘
   `knowledge/skills/<incident_id>.skill.json`。可选注入
   `llm` (LLMFacade) 润色 overlay —— 骨架确定性生成, overlay 不过
   校验则回退并记 warning。状态机与断点续跑见
   [references/loop_beats.md](references/loop_beats.md)。
4. **④ 场景进化 (evolve)**: 结晶记录里的契约修订草案 →
   `EvolutionProposal` → `dc_twin.esr.gate.admit()`。新注册类型必须
   附 (证据契约草案 + 消融子集定义 + 回归用例), 缺一拒绝; 同描述重
   注册幂等放行, 描述漂移/异源重名 → `GateError`。准入三件套与幂等
   语义见 [references/evolution_gate.md](references/evolution_gate.md)。
5. **⑤ 消融验证 (ablate)**: 逐回归用例回放断言 (缺省按用例 `evidence`
   工件求值全部断言, 宁严勿漏; 注入 `ablation_runner` 接真 30-seed
   契约回归门)。任一断言不过 → ⑤ 拍失败, ④ 的进化不得视为生效。
   数值判据 (DetFix max_pen≤0.016mm / determinism 等) 与 Text2MuJoCo
   对齐位见 [references/validation_thresholds.md](references/validation_thresholds.md)。

一条命令走完全程 (DetFix 样本):

```bash
python3 scripts/esr_detfix_walkthrough.py   # 仓库 scripts/ 下, 产 reports/esr/detfix_loop_v0.1/
```

或以 API 驱动:

```python
from dc_twin.esr.loop import ESRLoop, LoopConfig
report = ESRLoop(LoopConfig(
    loop_id="loop-001",
    incident_dir=Path("tests/fixtures/esr/detfix_m2"),
    workspace=Path("reports/esr/loop-001"))).run(resume=False)
assert report.status == "complete" and report.ablation["passed"]
```

## Output Package

一次完整 Loop 在 `workspace` 下产出 (每拍一个阶段记录, 原子落盘):

```text
workspace/
├── state.json                          # 进度账 (schema esr.loop_state/0.1):
│                                       #   beats_done / status / beat_errors / config
├── phases/
│   ├── 01-run.json                     # ① 运行证据核验 (mode + runs 清单)
│   ├── 02-crystallize.json             # ② 结晶定档 record + crystallized_by
│   ├── 03-consolidate.json             # ③ skill_path
│   ├── 04-evolve.json                  # ④ gate 裁决 (proposal_id / gate_approved /
│   │                                   #    rejections / registered)
│   └── 05-ablate.json                  # ⑤ passed + 逐用例断言结果
└── knowledge/skills/
    └── <incident_id>.skill.json        # Voyager 式技能条目 (esr.skill_entry/0.1)
```

**Gate 裁决** (④ 拍, 即 `GateVerdict`) 是 Output Package 的一部分:
`approved=False` 时 `phases/04-evolve.json` 携带逐条 `rejections`,
直接回喂草稿回路; `registered` 记录落册结果 (含 `@idempotent` 标记)。
报告 (`LoopReport`) 汇总 `status / beats_done / resumed / gate /
ablation / errors / warnings`。

## Runtime Boundaries

- 校验器是纯函数 (`validate_semantics` / `validate_proposal`), 无 core
  依赖, 错误清单可直接回喂 LLM 草稿回路; 只有 ④ 拍落册 (`admit` 缺省
  路径) 需要 core 注册表 (`services.world.evidence` /
  `sim.warehouse_cell`, 懒导入)。
- 确定性路径永不强依赖 LLM: `NullLLM` 恒回退; LLM 草稿不过校验 →
  回退 + warning, 不阻断闭环。
- 回归断言是结构化判据 (`{"metric","op","value"?}`), 经
  `evaluate_assert` 求值 —— 不 eval 任意表达式; 缺指标/未知算子按失败
  处理。
- 阶段记录用 `tmp + os.replace` 原子写, 断电不留半截记录; `resume=True`
  跳过已落账拍, 失败拍重做。
- 结晶层只起草契约修订 (status 恒 draft), 是否入册由 ④ 拍场景进化门
  裁决 —— 两层职责不越界。

## Response

中文请求以中文回复。汇报时给出: 五拍各自落账文件与 gate 裁决摘要、
技能条目路径、消融逐用例通过情况、以及校验器错误清单 (若有)。被跳过
或失败的拍要如实标注, 不得暗示已验证。
