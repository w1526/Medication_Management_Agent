# Phase 4.1 修补实现报告

## 1. 结论

Phase 4.1 已完成。本次是对 Advanced Scheduling 的一致性和可审计性修补，不是新阶段，也没有引入 M5、FHIR、HIS、Redis、PostgreSQL、智能药盒、PRN 自动给药、全球时区/DST 或多 Medication Entry Plan。

当前实现具备以下保证：

1. 历史 occurrence 可以只依赖自身的 `schedule_snapshot_json` 解释“为什么在这个时间提醒”。
2. Meal/Bedtime relation occurrence 保存实际使用的 routine anchor 与 `routine_version`。
3. Routine 更新、M2 重检、未来 occurrence 取消/生成和相关事件在一个 SQLite 事务中提交或全部 rollback。
4. 所有自动 Schedule Type 的候选都经过统一的 Plan effective range 过滤。
5. Preview 与正式 generation 使用同一个 `ScheduleExpander` 结果契约。
6. 重复 routine PUT 和重复 schedule recalculation 不产生重复 active occurrence。

## 2. 修改文件

### 代码

- `src/medication_reminder/routine.py`
  - `ElderRoutine` 增加 `routine_version`，旧 positional 构造顺序保持兼容。
  - `from_mapping()` 对旧行缺失版本时默认 `1`。
- `src/medication_reminder/storage.py`
  - `elder_routine.routine_version INTEGER NOT NULL DEFAULT 1`。
  - 通过已有幂等 migration 为旧数据库补列，不删除数据库、不重建表。
- `src/medication_reminder/schedule.py`
  - `ScheduleExpander` 为每条候选生成 resolved schedule context。
  - 对所有 schedule type 统一执行 `[effective_from, effective_until)` 过滤。
- `src/medication_reminder/service.py`
  - routine 真实变化才递增版本、更新 `updated_at`、发事件并重算。
  - 相同 routine PUT 直接返回，不触发重算和 event storm。
  - routine 更新链路保持单事务。
  - 重算时可恢复同一 stable occurrence identity，不新增重复行。
  - occurrence 写入完整 resolved snapshot。

### 测试与文档

- `tests/test_phase4_1_patch.py`：新增 21 项 Phase 4.1 测试。
- `docs/phase4_advanced_scheduling.md`：补充 Phase 4.1 的模型、范围和事务约定。
- `docs/phase4_advanced_scheduling_implementation_report.md`：补充真实测试数字。

## 3. Routine Version 设计

`elder_routine.routine_version` 从 `1` 开始：

```text
首次保存 breakfast=08:00  -> version=1
相同值再次 PUT             -> version=1，updated_at 不变
08:00 修改为 08:30         -> version=2
```

比较的是规范化后的 `breakfast_time`、`lunch_time`、`dinner_time`、`bedtime` 和 `timezone`。客户端不能通过 PUT 修改 `routine_version` 或 `updated_at`。

没有建设 Routine History 表。Occurrence snapshot 记录每次实际展开使用的 routine version，承担本次审计所需的历史解释职责。

## 4. Occurrence Snapshot 变化

`medication_occurrence.schedule_snapshot_json` 仍保留 canonical schedule config，并增加本次 occurrence 的最小 resolved context。

### Meal/Bedtime relation

```json
{
  "type": "MEAL_RELATION",
  "schedule_type": "MEAL_RELATION",
  "meal": "BREAKFAST",
  "relation": "AFTER",
  "offset_minutes": 30,
  "resolved_anchor_type": "BREAKFAST",
  "resolved_anchor_time": "08:00",
  "resolved_anchor_local_datetime": "2026-09-20T08:00:00+08:00",
  "resolved_local_datetime": "2026-09-20T08:30:00+08:00",
  "routine_version": 1
}
```

### 其他自动 Schedule Type

- `FIXED_TIME`：`times`、`selected_time`。
- `INTERVAL`：`anchor_at`、`interval_hours`、`interval_index`。
- `WEEKLY`：`weekdays`、`times`、`selected_weekday`、`selected_time`。
- `CYCLE`：`cycle_start_date`、`days_on`、`days_off`、`cycle_day`、`cycle_day_index`、`selected_time`。

旧 occurrence 的 snapshot 不会因 routine 后续变化而回写；新 occurrence 使用新 routine version 和新 resolved anchor。

## 5. Routine 更新事务设计与故障恢复

当前采用优先方案：单 SQLite 事务。

```text
BEGIN IMMEDIATE
  routine value/version 写入
  M2 recheck
  cancel future unconfirmed occurrence
  regenerate/recover future occurrence
  medication.routine.updated
  medication.schedule.recalculated
COMMIT
```

任一步抛出异常，`Storage.transaction()` 执行 rollback。因此本次不需要 `routine_recalculation_pending` 表或后台恢复 job；“恢复”由事务 rollback 保证，服务重启后仍是旧 routine + 旧 occurrence 的一致状态。

M2 `BLOCK` 的行为是确定的：新 routine 可以保留，BLOCK 检查会在同一个事务内记录，相关未来 occurrence 被取消，不生成新的 active future occurrence。历史 `confirmed_taken`、`skipped`、`closed_unconfirmed` 不参与取消条件。

`medication.routine.updated` payload 增加 `old_routine_version`、`new_routine_version` 和 `affected_plan_ids`；没有复制额外 PHI。

## 6. Effective Range 规则

所有自动 Schedule Type：`FIXED_TIME`、`MEAL_RELATION`、`ROUTINE_RELATION`、`INTERVAL`、`WEEKLY`、`CYCLE`，都在 `ScheduleExpander` 最终过滤阶段使用：

```text
effective_from <= scheduled_at
scheduled_at < effective_until
```

具体约定：

- Active Plan 使用实际 `effective_from` 激活时间。
- 没有激活时间的直接 Plan/draft expansion 使用 `start_date 00:00` 作为下界。
- 直接传入 `effective_until` 时按 exclusive 处理。
- 持久化 Plan 当前只有 date-only `end_date`，保持既有 inclusive 日历日语义，转换为次日 00:00 exclusive upper bound。
- Requested horizon 与 Plan effective range 取交集。
- Meal 前移到生效日前会被过滤；候选等于 `effective_from` 会保留。
- Interval 的 anchor 可以早于 `effective_from`，只用于计算序列 index，输出仍必须在有效范围内。
- Cycle 的 `cycle_start_date` 可以早于 `effective_from`，仍用于计算 cycle day，输出仍必须在有效范围内。

## 7. Occurrence Identity

本次不重构 identity，继续使用：

```text
plan_id + plan_version + scheduled_at
```

stable hash 和数据库唯一约束继续共同防止重复。重复重算会恢复相同的、此前因 `routine_changed`/`schedule_recalculated` 取消的 occurrence identity，而不是插入第二行。

未来如果一个 Plan Version 包含多个 Medication Entry，需要升级为：

```text
plan_id + plan_version + medication_entry_id + scheduled_at
```

本次不实现。

## 8. 新增测试

`tests/test_phase4_1_patch.py` 共 21 项，覆盖：

- Meal resolved anchor、routine version、历史 snapshot 不变、新 occurrence 使用新版本；
- 相同 routine PUT 幂等、真实变化递增版本；
- regeneration failure injection、rollback、服务重启后一致；
- routine change + M2 BLOCK 不留下 active future occurrence；
- Meal 跨日前移、effective_from 等于候选、FIXED_TIME effective_until；
- INTERVAL anchor 早于生效时间、WEEKLY horizon 截断、CYCLE anchor/cycle day；
- Preview 与正式 generation 的 effective range 一致；
- FIXED/INTERVAL/WEEKLY/CYCLE snapshot context；
- 历史 intake status 保护；
- 重复 recalculation 不产生 duplicate occurrence；
- 旧 `elder_routine` schema 自动迁移为 `routine_version=1`。

## 9. 完整真实测试结果

测试在 2026-09-20 完成，结果不是占位命令：

| 命令/范围 | 真实结果 |
|---|---:|
| `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v` | **120 tests, 120 OK, 0 failures, 0 errors** |
| `tests/test_phase4_scheduling.py` | **18 tests, 18 OK** |
| `tests/test_phase4_1_patch.py` | **21 tests, 21 OK** |
| `PYTHONPATH=. .venv/bin/python -m unittest discover -s text_bridge/tests -v` | **10 tests, 10 OK, 0 failures, 0 errors** |
| `node --check web/assets/app.js` | **OK** |

Phase 4.1 前 baseline 为 `tests/` 99 tests 全部 OK；修补后新增 21 项，最终为 120 tests 全部 OK。

## 10. HTTP Smoke Test

使用临时 SQLite 数据库启动真实 `ThreadingHTTPServer`，按 HTTP 请求验证：

```text
health=200
routine PUT=200
draft=201
approve=200
preview=200
scheduler=200
HTTP_SMOKE PASS
```

## 11. 当前仍未实现内容

本次明确不做、当前仍不具备生产能力的部分包括：

- M5 Evidence Fusion；
- FHIR/HIS/DrugBank/OpenFDA 等外部临床数据接入；
- 真实 DDI、过敏、禁忌症和临床剂量知识覆盖；
- PRN 自动 trigger/自动给药；
- 智能药盒、重量传感器和真实设备闭环；
- 复杂多阶段 Cycle；
- Redis/PostgreSQL、HA 部署和分布式 scheduler；
- 全球时区、DST；
- RBAC、医生/家属生产级工作台和真实通知通道；
- 多 Medication Entry Plan。

当前仍是单机 SQLite/本地 adapter 的工程 MVP；M2 的结构检查和 fixture provider 不能等同于临床安全覆盖。

## 12. 下一阶段建议

下一阶段应先选择基础设施方向（PostgreSQL/可靠 worker/权限与真实通知）和临床数据方向（受控规则源、证据链、人工复核）中的一个，在保持当前 stable Schedule/Occurrence contract 的前提下逐步推进。PRN、FHIR 和多药品 Plan 不应与本次 Phase 4.1 修补混入同一变更。
