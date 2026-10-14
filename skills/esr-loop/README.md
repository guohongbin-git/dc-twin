# ESR-Loop Agent Skill

[中文](#中文) · [English](#english)

## 中文

这个目录是一个 Agent Skill。[`SKILL.md`](SKILL.md) 是可被发现的 skill
入口; `references/` 提供技能结晶格式、场景进化门、五拍状态机与数值判据
四份参考文档, `scripts/` 提供确定性校验器与回归测试。

**定位 (蓝图 v0.5.1 §17-§18)**: ESR — Evidence-Simulation-Ready, 是
NVIDIA SimReady (OpenUSD 资产规范: 物理属性/语义标签/材质/dense
captions/多运行时) 的**超集**。SimReady 解决"资产丢进仿真就能用"; ESR
在其上追加三层 SimReady 没有的东西:

| 层 | SimReady | ESR |
|---|---|---|
| L+1 证据契约 | 无 | 资产自带 evidence manifest: 哪些事件组合证明哪些状态转移、提交语义、恢复行为 |
| L+2 技能接口 | 无 | MCP tool 面: 每工位暴露 agent 可调用技能 (前置→执行→证据→后置), Voyager 式技能库 |
| L+3 可验证性 | 无 | 随附消融-评估 harness (B0-B4 基线), 指标预注册 |

一句话: **SimReady 让资产"能仿真", ESR 让资产"能被信任地执行、被
agent 操作、被独立验证"。** 而本质区别是自进化闭环 (SimReady 没有智
商): 场景/资产随运行经验生长, 每一次进化都要过 ⑤ 的消融门。

五拍闭环 (§18.①):

```
① 运行   ② 经验结晶   ③ 知识沉淀   ④ 场景进化   ⑤ 消融验证
sim 连续 → 事件/故障/   → 技能库+证据  → 注册表新类型/  → 契约回归门
运转     停机/对账分歧     契约自动起草   证据契约自动生成
   ↑______________________________________________________|
```

实现为仓库内 `dc_twin/esr/` 包 (`crystallize.py` 四件套格式 / `gate.py`
进化门 / `loop.py` 五拍编排器); 样本 fixture 在 `tests/fixtures/esr/`
(DetFix 16 runs 炼三修复 + 产线输送缺失), 走通脚本在仓库
`scripts/esr_detfix_walkthrough.py` 与 `scripts/esr_conveyor_walkthrough.py`。

## English

This directory is an Agent Skill. [`SKILL.md`](SKILL.md) is the
discoverable skill entrypoint; `references/` carries the four-piece
crystallization format, the evolution gate, the five-beat state machine,
and the numeric validation thresholds; `scripts/` provides a deterministic
validator plus its regression suite.

**Positioning (blueprint v0.5.1 §17-§18)**: ESR — Evidence-Simulation-
Ready — is a **superset** of NVIDIA SimReady (OpenUSD asset spec:
physics / semantic labels / materials / dense captions / multi-runtime).
SimReady makes assets "drop-in simulatable"; ESR adds three layers
SimReady does not have: evidence contracts (which event combinations
prove which state transitions, commit semantics, recovery behavior),
skill interfaces (per-station agent-callable skills exposed as MCP
tools, curated in a Voyager-style skill library), and verifiability
(ablation-evaluation harness over B0-B4 baselines with pre-registered
metrics). In one sentence: **SimReady makes assets simulatable; ESR makes
assets trustworthily executable, agent-operable, and independently
verifiable.** The essential difference is the self-evolution loop
(SimReady has no intelligence): scenes and assets grow from runtime
experience, and every evolution must pass the ablation gate.

## 安装 / Install

```bash
mkdir -p ~/.claude/skills
cp -R skills/esr-loop ~/.claude/skills/esr-loop
```

## 校验 / Validate

校验器独立于 agent 运行 (纯函数, 只要求 `dc_twin` 包可导入):

```bash
# 仓库内 (自动发现 dc_twin):
python3 skills/esr-loop/scripts/validate_incident.py tests/fixtures/esr/detfix_m2

# 任意位置: 用 DC_TWIN_HOME 或 PYTHONPATH 指向 dc-twin 仓库根
DC_TWIN_HOME=~/1-Projects/dc-twin \
  python3 validate_incident.py /path/to/incident_dir --json
```

回归: `python3 skills/esr-loop/scripts/test_validate_incident.py`。
