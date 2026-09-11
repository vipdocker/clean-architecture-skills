# Draw.io 交付说明 — autopilot-flow

- **正典源**（source of truth）: `pipeline/autopilot-flow.dot`（Graphviz DOT，12 节点）
- **可编辑交付物**: `pipeline/autopilot-flow.drawio`（diagrams.net XML，由 DOT 人工转换，稳定 ID 一致：orch/P0/P1/P2/G3/P4/G5/P6/CHK/LOG/USER/DONE）
- **生成日期**: 2026-09-11 · 系统版本 v1.10.0

## 证据溯源（全部 high 置信度）

| 图中元素 | 证据 |
|---|---|
| 阶段序列 P0→P1→P2→G3→P4→G5→P6 | `skills/clean-architecture-autopilot/SKILL.md` 状态机章节 |
| P0 可选（存量库时） | SKILL.md `### P0 — Research (optional...)` |
| 每阶段 Agent 与本地 skill 名 | SKILL.md Phase Dispatch Table |
| G3 工具 dep_graph.py (AST+Tarjan) | SKILL.md G3 章节 |
| 进入门闩 P4←g3 / P6←g5 | `cc_log.py:87` `PHASE_GATE_PREREQ` |
| 返工回路 P2 重入 void g3、P4 重入 void g5 + 精准 scope 路由 | `cc_log.py:97` `PHASE_INVALIDATES` + SKILL.md G5 verdict 路由 |
| 迭代上限 ≤2 → 升级用户 | `cc_log.py:109` `MAX_GATE_ITERATIONS` |
| P2 退出门闩（design_coverage + plan_graph + 产物位置） | `cc_log.py` P2 exit latch 块 |
| P4 配对计时 agent_dispatch ⇄ component_done | SKILL.md "Log the dispatch" 段 |
| P6 report.md 门闩 | `cc_log.py` P6 exit latch + SKILL.md P6 章节 |
| USER LOOP 四触发器与批量契约 | `pipeline/orchestration.md` "When to Enter the User Loop" |
| 日志三件套 run.jsonl/state.json/manifest | SKILL.md Directory layout |

## 再生成

```bash
# DOT 渲染（正典预览）
dot -Tsvg pipeline/autopilot-flow.dot -o pipeline/autopilot-flow.svg
# drawio 为手工排版产物; 若流程变化, 先改 .dot, 再同步 .drawio 的节点/边与标签
```

## 维护规则

- 流程事实变化时**先改 DOT**，再同步 drawio——drawio 是派生交付物，不是正典。
- 不要在 drawio 里手工改架构事实而不更新 DOT（架构契约：evidence first）。
