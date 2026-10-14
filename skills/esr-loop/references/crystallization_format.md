# 技能结晶四件套格式 (crystallize)

实现: `dc_twin/esr/crystallize.py` (纯函数, 无 core 依赖)。
权威定义: `dc_twin/esr/schemas/incident_record.schema.json`
(`$id: esr.incident_record/0.1`, JSON Schema 2020-12 子集; 内置校验器
按同一子集执行, 不引入第三方依赖)。

蓝图出处: §18.③ —— `incident → {根因假设, 修复动作, 回归用例, 证据
契约修订}`。DetFix (16 runs 把 6 层故障链炼成三修复) 是首个样本, 取证
移植在 `tests/fixtures/esr/detfix_m2/`; 产线输送缺失样本在
`tests/fixtures/esr/conveyor/`。

## 顶层字段

| 字段 | 必填 | 约束 | 语义 |
|---|---|---|---|
| `schema` | ✓ | const `esr.incident_record/0.1` | 格式版本; `crystallize()` 先查此项再校验 |
| `incident_id` | ✓ | `^INC-[A-Za-z0-9-]+$` | 事故唯一 ID; ③ 拍技能条目文件名取自它 |
| `title` | ✓ | minLength 1 | 一句话标题 |
| `window` | — | object, additionalProperties false | 事故时间窗: `t_start`/`t_end` (number, 必填), `clock_domain` (string, 可选, 如 `sim_t`) |
| `runs` | — | string[] | 涉及的 run 标识 (如 `["run1","run3","run4","run12"]`) |
| `source` | — | string | 取证来源 (报告路径/用户反馈), 可审计性入口 |

`additionalProperties: false` 逐层生效 —— 多余字段一律报错, 草稿不许
夹带私货。

## 第一件: root_cause_hypothesis (根因假设)

| 字段 | 必填 | 约束 | 语义 |
|---|---|---|---|
| `statement` | ✓ | minLength 1 | 陈述式根因 (如 "lane-lock 导航到位即放、无联锁 → 挤碰 → 托盘坠落 → NaN 崩塌") |
| `mechanism` | — | string[] | 机制链逐环节 (级联因果, 有序) |
| `confidence` | ✓ | number ∈ [0.0, 1.0] | 置信度 |
| `evidence_refs` | — | string[] | 取证引用 (报告/日志/蓝图章节路径) |

## 第二件: fix_actions (修复动作, minItems 1)

| 字段 | 必填 | 语义 |
|---|---|---|
| `action_id` | ✓ | 如 `FIX-1`; 被回归用例 `covers_action` 引用 |
| `target` | ✓ | 作用对象 (如 `lane-lock 语义`、`op_pin`、`场景注册表`) |
| `description` | ✓ | 做法 (含关键数值依据) |
| `expected_effect` | — | 预期效果; ③ 拍沉淀为技能条目 `postcondition` |

## 第三件: regression_cases (回归用例, minItems 1)

| 字段 | 必填 | 语义 |
|---|---|---|
| `case_id` | ✓ | 如 `REG-1` |
| `name` | — | 人类可读名 |
| `seeds` | — | integer[] (如 `[7]`; DetFix 全链 seed=7 determinism) |
| `scenario` | ✓ | 场景标识 (如 `m2_matrix.8task.determinism`); ④ 拍据此组装消融子集 scenarios |
| `asserts` | ✓ | minItems 1; 结构化断言, 见下 |
| `covers_action` | ✓ | 所保护的修复动作 `action_id` |
| `evidence` | — | 相对 `incident_dir` 的运行工件路径 (⑤ 拍离线回放源, 如 `runs/run4_postfix.json`) |

**断言条目** `{"metric", "op", "value"?}`:

- `metric`: 工件 JSON 内的点路径 (如 `max_pen_m`、`op_errors`);
  `evaluate_assert` 逐段下钻, 缺指标按失败处理 (宁严勿漏)。
- `op` 合法枚举: `eq ne lt le gt ge contains empty nonempty`, 以及
  符号别名 `== != < <= > >=` (求值时归一化为字词形)。带值算子
  (`eq/ne/lt/le/gt/ge/contains` 及其符号形) **必须随附 `value`**;
  `empty/nonempty` 不带。
- 断言由 `evaluate_assert(artifact, spec) -> (bool, detail)` 求值,
  **不 eval 任意表达式**; 类型不可比 (TypeError) 也按失败处理。
- `contains` 用 `value in val` 成员判断; `empty` = None 或 len==0,
  `nonempty` 反之。

## 第四件: evidence_contract_revision (证据契约修订)

| 字段 | 必填 | 约束 | 语义 |
|---|---|---|---|
| `revision_kind` | ✓ | enum `none \| amend \| new` | amend = 补既有契约 (DetFix: 加 LANE_INTERLOCK 事件类型); new = 新契约 (conveyor: CONVEYOR 节点 + in/out 事件对) |
| `summary` | — | string | 修订摘要 (契约保证的散文版) |
| `proposed_changes` | — | object[] | 每条: `path`* + `change`* + `reason`? + `register`? |
| `status` | ✓ | **const `draft`** | 见语义门 ④ |

`register` 指令 (④ 拍据此组装注册载荷, 经 core
`register_event_kind` / `register_node_source` 落册):

- `as`: `event_kind` (附 `kind`/`description`) 或 `node_source`
  (附 `fn` — `"pkg.module:attr"` 序列化引用, `name`?)。
- Loop ④ 拍自动取**第一条**带 `register` 的 change 落册; 多条注册
  指令需逐条补册 (见 `scripts/esr_conveyor_walkthrough.py` ④ 续段)。

## 语义门 (validate_semantics, schema 之上)

`validate_semantics(obj) -> list[str]` 是纯函数, **返回错误清单而非抛
异常** —— 就是为 LLM 草稿回路设计的。四条语义门:

1. **动作必有回归保护**: 每条 `fix_actions` 必须被 ≥1 条回归用例
   `covers_action` 指向, 缺一拒绝 (`修复动作 'FIX-n' 无回归用例保护`)。
2. **covers_action 必须指向存在的 action_id**: 悬空引用报错并列出
   现有动作 ID。
3. **断言算子合法且带值算子必须随附 value**。
4. **契约修订恒 draft**: `status != "draft"` 即拒绝 —— 修订是否入册
   由场景进化门 (`esr.gate`) 裁决, 结晶层只起草。

对残缺输入防御: 不抛异常, 只报错。

## 定档与求值 API

```python
from dc_twin.esr.crystallize import (
    SCHEMA_ID, ASSERT_OPS,            # "esr.incident_record/0.1" / 算子表
    load_schema,                      # 随包 JSON Schema (权威定义)
    validate_semantics,               # obj -> 错误清单 (纯函数, 草稿回路)
    crystallize,                      # incident_dir -> IncidentRecord
    evaluate_assert,                  # (artifact, spec) -> (ok, detail)
)

record = crystallize("tests/fixtures/esr/detfix_m2")   # schema/语义任一
#                                                      # 不过 -> ValueError
#                                                      # (清单随异常文本)
```

`crystallize()` 产出的 `IncidentRecord` (frozen dataclass) 含
`root_cause` / `fix_actions` / `regression_cases` / `contract` /
`window` / `runs` / `source`, `to_dict()` / `from_dict()` 往返 JSON
可持久化 —— ② 拍阶段记录存的就是 `record.to_dict()`。

## 草稿回路 (LLM 用法)

② 拍 LLM 草稿即一份候选 dict: 经 `validate_semantics` 迭代修复, 清单
清零后交 `crystallize()` 定档。Loop 的 `LLMFacade.draft_crystallization`
即此用法 (见 loop_beats.md): 草稿不过校验 → 自动回退确定性路径并记
warning。
