# 五拍状态机 / 断点续跑 / LLM 可选面 (loop)

实现: `dc_twin/esr/loop.py`。蓝图出处: §18.① (五拍闭环图) + §18.③
(Loop 编排器)。

```
① 运行      ② 经验结晶     ③ 知识沉淀      ④ 场景进化      ⑤ 消融验证
sim 连续  →  事件/故障/    →  技能库+证据   →  注册表新类型/  →  契约回归门
运转        停机/对账分歧     契约自动起草     证据契约自动生成
  ↑ ________________________________________________________|
                      每一次进化都要过 ⑤ 的消融门
```

## 0. 事故目录 (Loop 输入)

```text
incident_dir/
├── incident.json     # ② 拍结晶源 (四件套, 见 crystallization_format.md)
└── runs/*.json       # ① 拍证据 + ⑤ 拍离线回放工件 (case.evidence 相对本目录)
```

样本: `tests/fixtures/esr/detfix_m2/`、`tests/fixtures/esr/conveyor/`。

## 1. 五拍语义 (BEATS = run, crystallize, consolidate, evolve, ablate)

| 拍 | 做什么 | 缺省行为 | 注入面 |
|---|---|---|---|
| ① run | 确认/产生运行证据 | 核验 `incident.json` 在列 + `runs/*.json` 清单 (`mode=reuse_existing`) | `runner(cfg) -> summary` 接真 sim |
| ② crystallize | 四件套定档 | `crystallize(incident_dir)` | `llm.draft_crystallization` 草稿 |
| ③ consolidate | 四件套 → 技能条目 | 确定性骨架生成, 落 `knowledge/skills/<incident_id>.skill.json` | `llm.draft_skill` overlay |
| ④ evolve | 结晶记录 → 提案过门 | `admit(proposal, idempotent=True)` | `node_source_registrar` / `event_kind_registrar` |
| ⑤ ablate | 逐回归用例跑断言 | `evidence` 工件回放 (缺工件/任一断言不过即败, 宁严勿漏) | `ablation_runner(case, incident_dir)` 接真 30-seed harness |

顺序推进, **每拍成功即落账, 失败即停**; 拍内异常捕获进报告, 不外抛。
④ 门拒绝或 ⑤ 断言未过都会把该拍记入 `beat_errors` 并终止 (status=
failed), 后拍不执行。

### ② 拍细节: LLM 草稿 → 确定性回退

1. `llm.draft_crystallization(raw)`: 异常 → 回退 + warning; `None` →
   确定性回退 (无 warning); 返回 dict → `validate_semantics` + schema
   版本检查, 不过 → 回退 + warning (清单附 warning);
2. 通过 → `IncidentRecord.from_dict(draft)`, 阶段记录
   `crystallized_by="llm"`; 回退则 `"deterministic"`。

### ③ 拍细节: Voyager 式技能条目 (schema `esr.skill_entry/0.1`)

```json
{"schema": "esr.skill_entry/0.1",
 "skill_id": "skill.<incident_id>",
 "incident_id": "…", "title": "…",
 "prerequisite":  ["根因假设: …", "机制: …"],
 "procedure":     [{"action_id", "target", "description"}],
 "evidence":      {"contract_revision": {...}, "regression": [...]},
 "postcondition": [各 expected_effect],
 "provenance":    {"source", "crystallized_at"}}
```

LLM overlay 只许润色: overlay 的键合并进条目, 但 `schema` /
`skill_id` / `incident_id` 三键不可覆盖。

### ④ 拍细节

`_proposal_from(record)` 装配提案 (装配规则见 evolution_gate.md §
"Loop ④ 拍的装配"); 门拒绝 → 逐条 `rejections` 进阶段记录与
`beat_errors`。

### ⑤ 拍细节: 离线回放 `_replay_case`

- `case.evidence` 缺失 → 该用例败 (`回归用例未附证据工件`);
- 工件相对 `incident_dir` 解析, 缺文件 → 败;
- 逐条 `evaluate_assert(artifact, spec)`, 全过才 passed;
- 结果逐断言落账 (`assert`/`ok`/`detail`)。

## 2. 持久层与断点续跑

- 每拍阶段记录: `workspace/phases/NN-<beat>.json`
  (`01-run.json` … `05-ablate.json`), `schema=esr.loop_state/0.1`,
  带 `loop_id`/`beat`/`status`/`at` + 拍载荷;
- 进度账: `workspace/state.json` (`beats_done` / `status` /
  `beat_errors` / `config` / `updated`);
- **原子落盘**: `tmpfile + os.replace`, 断电/中断不留半截记录;
- **断点续跑**: `run(resume=True)` (缺省) 跳过已落账拍 (且校验阶段
  文件仍在盘), 失败拍重做, 重试成功即销账; 续跑上下文从阶段记录还原
  (② 结晶定档、④ 门裁决);
- `ESRLoop.resume(workspace, **overrides)`: 从既有 `state.json` 的
  `config` 重建编排器。

## 3. LLMFacade 可选面 (MCP 工具面接口预留)

```python
@runtime_checkable
class LLMFacade(Protocol):
    def draft_crystallization(self, incident: dict) -> dict | None: ...
    def draft_skill(self, record: dict) -> dict | None: ...

class NullLLM:            # 无 LLM 安全桩: 恒回退确定性路径
    ...
```

对应 MCP tools (宿主绑定传输层, 本包不管): `esr.crystallize_draft`
(② 拍草稿) / `esr.skill_draft` (③ 拍润色)。**确定性路径永不强依赖
LLM**: 返回 `None` → 回退; 草稿不过校验 → 回退 + warning; LLM 出错
不阻断闭环。

## 4. 驱动方式

```python
from pathlib import Path
from dc_twin.esr.loop import ESRLoop, LoopConfig, BEATS   # BEATS: 五拍序

cfg = LoopConfig(loop_id="loop-detfix-001",
                 incident_dir=Path("tests/fixtures/esr/detfix_m2"),
                 workspace=Path("reports/esr/detfix_loop_v0.1"))
report = ESRLoop(cfg).run(resume=False)      # -> LoopReport

report.status        # "complete" | "failed" | "running"
report.beats_done    # 已落账拍
report.resumed       # 本次续跑命中的已完成拍
report.gate          # ④ GateVerdict (approved / rejections / registered)
report.ablation      # ⑤ {"passed": bool, "cases": [...]}
report.errors / report.warnings
```

走通脚本 (真 core 注册表落册实证): 仓库 `scripts/esr_detfix_walkthrough.py`
(DetFix, ④ 落 LANE_INTERLOCK) 与 `scripts/esr_conveyor_walkthrough.py`
(① runner 接真运行, ④ 落 node_source conv_nodes + 事件对 CONVEYOR_IN/
CONVEYOR_OUT, 幂等复跑)。仓库回归: `tests/test_esr_loop.py`。
