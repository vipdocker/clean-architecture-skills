# 流水线证据完整性与可归因性设计

日期：2026-09-25

## 目标

让 clean-architecture-autopilot 的运行日志能够可靠回答四个问题：设计是否真的按计划落地、耗时是否可归因、用户授权是否真实且未重复记账、验证结论是否与原始工具输出一致。该设计只修改流水线工具与协议，不修改业务项目的功能代码。

## 范围

本次覆盖六项机制：P2/P4/diff 追溯、worker 与门闩活动计时、问题与债务账本、验证汇总、组件归属图、初始脏工作区 provenance。旧日志和旧 P2 artifact 保持可读取，但报告必须标注其证据粒度不足，不能补造精确指标。

不迁移既有 `.cc-skill` 历史日志，不重写事件存储格式，也不要求业务项目采用特定目录布局。

## 架构

### 1. 设计实现追溯门

新增 `scripts/design_trace.py`，输入为 P2 artifact、P4 component artifact、运行 manifest、`run.jsonl` 与基线 diff。

P2 的 `dag_tasks[]` 是每项工作的计划文件集合；P4 artifact 的 `task_id` 与 `files[]` 是实际落点。任务实际文件与计划不一致时，编排器必须先记录：

```json
{
  "event": "design_amendment",
  "detail": {
    "task_id": "T-Q2",
    "planned_files": ["modules/services/scenario_pnl_analyzer.py"],
    "actual_files": ["modules/services/option_scenario_rules.py"],
    "reason": "任务消息冻结了更准确的领域命名"
  }
}
```

`design_trace.py` 输出 `PASS`、`FAIL` 或 `UNCHECKED`，并列出：未落实的计划文件、未登记的实际文件、未归属的基线 diff、已批准的 amendment。G5 的 passing verdict 只有在 trace 为 `PASS` 或明确的 `UNCHECKED` 降级说明存在时才能记录；`FAIL` 不允许以正常 passing verdict 收尾。

### 2. 可信时间语义

保留现有 `agent_dispatch` 与 `component_done`，但它们只表示编排器调度和接收结果。新增通用事件：

```json
{"event":"work_started","phase":"P4","detail":{"activity_id":"T-B7","component":"T-B7","logical_wave_id":"wave3","execution_batch_id":"wave3-b1"}}
{"event":"work_finished","phase":"P4","detail":{"activity_id":"T-B7","component":"T-B7","execution_batch_id":"wave3-b1"}}
```

G3/G5 同样用 `work_started/work_finished`，但只填 `activity_id`，例如 `g5-full-review`。`logical_wave_id` 描述 P2 DAG 波次，`execution_batch_id` 描述受 `max_parallel` 约束后的真实批次，二者不得混用。

报告只在每个已完成 P4 组件均具有合法 lifecycle 对时计算单组件耗时、P4 并行度与模型 ROI。缺失 lifecycle 时，报告输出批窗口和 `timestamp_granularity=batched`，不输出精确并行度。暂停归类将查看所有 open activities：门闩审查在运行时不能被自动归为 idle。

### 3. 问题与债务授权账本

`question_ledger` 扩展为：

```json
{
  "asked": 0,
  "self_resolved": 0,
  "self_resolved_records": [
    {"seq": 7, "question": "...", "basis": "P0 note / SDD section"}
  ]
}
```

每个 `adopted_without_asking` 生成独立记录，计数仅从记录派生。新增 `debt_signoff_reused`：仅当当前债务列表逐字等于已有签认列表，且引用原始 `user_loop_seq`、`debt_signoff_seq` 和用户原话时可用。复用事件不增加 `asked`、不生成 `pending_user_question`、不重复写入 `debts_signed_off`。

### 4. 验证证据账本

新增 `verification_recorded` 事件，保存工具名、状态、原始输出 artifact、error/warning/info 计数与 accepted warnings：

```json
{
  "event":"verification_recorded",
  "detail":{
    "tool":"ast-grep",
    "status":"PASS_WITH_ACCEPTED_WARNINGS",
    "artifact":"artifacts/ast-grep.json",
    "counts":{"error":0,"warning":6,"info":0},
    "accepted_warnings":[{"rule":"python-broad-exception","path":"...","reason":"外部库边界异常翻译"}]
  }
}
```

机械报告按事件渲染计数，禁止推导“零告警”。SKILL 约束 prose summary 必须引用同一计数：只有 error、warning、info 全为零时才可写“零告警”。

### 5. 组件归属与依赖图

`dep_graph.py` 新增 `--component-map <p2-artifact>`。P2 `component_map` 扩展为显式 `ownership[]`，每条声明 path/module、component 和 `kind`（`domain`、`adapter`、`shared_adapter`、`framework`）。共享实现必须作为独立 `shared_adapter` 组件登记，不能隐含归属于业务组件。

工具输出模块边、组件边、未归属模块、共享组件依赖和组件 SCC。G3 审计 artifact 必须包含该结果或对不可扫描项目的明确降级理由。这样，类似“home 组件内的 store 运行期导入 valuation DTO”会成为可见组件边，而不会被 focus-prefix 扫描静默忽略。

### 6. 初始脏工作区 provenance

`init` 记录每个初始脏文件的 path、git status、SHA-256 与首次观察时间。新增 `baseline_accept` 命令或等价事件，只能接受 init 时记录的路径，并要求 purpose 与 `commit_allowed` 布尔值。

报告将初始文件分为：已接受输入、未接受初始改动、运行中新改动。初始文件永远不计入“本轮新增”；未接受初始改动会在 P6 前成为显式 warning，禁止以本轮成果归因。

## 兼容性与错误处理

- 旧日志没有 lifecycle、trace、verification 或 provenance 事件时保持可报告，但对应指标显示 `unsupported` 或 `UNCHECKED`。
- 新事件继续 append-only；`state.json` 允许缺少新增字段并在首次事件时初始化。
- 显式传入但不可读取或无效的 `--p2`、`--run-dir`、`--root`、`--baseline` 等 CLI 路径是调用错误：`design_trace.py` 以 CLI error exit 2 终止，不降级为 `UNCHECKED`。
- 只有已定位的历史运行缺少 P4 artifact、基线 diff 或旧版事件等证据时，才返回 `UNCHECKED` 并说明缺口；已提供的 P4 artifact、amendment、baseline 或 accepted-preexisting path 任一结构或路径畸形时返回 `FAIL`，且必须在判断 P2 DAG/P4 是否缺失前完成校验。
- `debt_signoff_reused` 的引用链、债务文本和原用户答案任一不匹配时拒绝记录。

## 修改面

- `skills/clean-architecture-autopilot/scripts/cc_log.py`：事件校验、state fold、报告、baseline accept CLI。
- `skills/clean-architecture-autopilot/scripts/design_trace.py`：新追溯检查器。
- `skills/clean-architecture-autopilot/scripts/dep_graph.py`：组件归属输入与组件图输出。
- `skills/clean-architecture-autopilot/SKILL.md`：事件协议、P2/G3/G5 门闩与报告口径。
- 各 agent frontmatter / prompt：版本一致性与 lifecycle、amendment、verification 记录责任。
- `install.sh`：校验并安装 `design_trace.py`。
- 新增 Python 回归测试：覆盖 trace 拦截、批窗口降级、签认复用、验证计数、组件图和脏基线分类。

## 验收标准

1. P4 artifact 文件偏离 P2 且无 amendment 时，G5 passing verdict 被拒绝；登记 amendment 后 trace PASS。
2. 缺 `work_started/work_finished` 的并发任务不会生成 per-component duration、parallelism 或 model ROI；完整 lifecycle 才生成这些指标。
3. G3/G5 的 open activity 使长审查间隔不被误判为空转。
4. 18 条 `adopted_without_asking` 使 `self_resolved=18` 并保留 18 条有依据记录。
5. 对完全相同债务的 delta review 使用 `debt_signoff_reused` 后，问询数和签认债务数不增长。
6. 含 0 error、6 warning 的 AST 扫描在报告中显示相同计数，且未列 accepted reason 时状态不为通过。
7. 共享 adapter 出现在组件图，未登记的模块或组件环可被 G3 artifact 读取。
8. 初始脏文件即使随后提交，也在报告中单列为 accepted preexisting input，不计为本轮新增。
