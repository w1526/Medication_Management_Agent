# Phase 4 Advanced Scheduling Implementation Report

## 1. 结论

Phase 4 已在现有 Phase 1–3 服务上增量完成。Plan Draft、Schedule Validation、M2 Safety、Plan Version、Occurrence Expansion、Scheduler 和 Reminder 仍使用原有事务/Outbox 链路。

## 2. 修改文件

- `src/medication_reminder/schedule.py`：ScheduleType、Validator、Expander、OccurrenceSpec。
- `src/medication_reminder/routine.py`：ElderRoutine 与显式 anchor 校验。
- `src/medication_reminder/storage.py`：幂等 migration、新字段和 `elder_routine`。
- `src/medication_reminder/service.py`：canonical schedule、审批门禁、展开、稳定 identity、preview、routine 重算。
- `src/medication_reminder/safety.py`：高级 schedule 不再被 legacy `schedule_time` 误判为 M2 BLOCK。
- `src/medication_reminder/http.py`：routine、preview、recalculate 和 PUT。
- `src/medication_reminder/semantic/agent.py`、`harness_adapter.py`：新 Draft contract 与澄清边界。
- `web/assets/app.js`：最小 schedule 创建和计划摘要显示。
- `tests/test_phase4_scheduling.py`：Phase 4 单元/服务/API/Harness 测试。

## 3. 模型与类型

实现 `FIXED_TIME`、`MEAL_RELATION`、`INTERVAL`、`WEEKLY`、`CYCLE` 和仅 schema 的 `PRN`；额外支持 `ROUTINE_RELATION(BEDTIME)`。所有 canonical config 会写入 Plan，Occurrence 保存 snapshot/source。

## 4. 校验与错误

`SCHEDULE_INVALID` 用于时间、weekday、周期、interval anchor 等结构错误；`SCHEDULE_CONTEXT_MISSING` 用于 routine anchor 缺失；`SCHEDULE_UNSUPPORTED` 用于未知类型。Schedule 错误发生在 M2 之前，Safety BLOCK 仍只表示 M2 结论。

## 5. 确定性展开

正式 generation 与 preview 都调用 `ScheduleExpander`。固定时间排序去重；meal/routine 使用 explicit anchor 加减 offset；interval 按 anchor + N×interval；weekly 使用 ISO 1–7；cycle 使用 on/off 模运算。内部保持 aware datetime，数据库保存 UTC。

## 6. 幂等、版本与恢复

Occurrence identity 是 `plan_id|plan_version|scheduled_at` 的稳定 hash，并由数据库唯一键兜底。重复 approve、重复 horizon refill 和服务重启不会 duplicate；删除测试中的缺失 future row 后，重新启动服务可以补齐。

## 7. Routine Change

routine 更新只影响引用该 anchor 的 active version，历史 occurrence 不变。未来未确认 occurrence 取消后重新生成；同一 routine 值重复提交不会产生第二套 occurrence。重算会重新保存当前 Plan Version 的 M2 Safety Check。

## 8. Safety

新版本不会继承旧 Safety Check；合法 Schedule 仍须 PASS/WARN 才能 Active，BLOCK 时不生成 occurrence。Phase 3 的 ruleset fingerprint、freeze、future cancellation 和历史保护逻辑保持不变。

## 9. API/Event/Schema/Web

见 `docs/phase4_advanced_scheduling.md`。Preview 明确返回 `persisted=false`，routine API 只保存最小作息字段。

## 10. 测试结果

Phase 4.1 最终真实执行结果如下：

| 检查项 | 结果 |
|---|---|
| `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v` | **120 tests, 120 OK, 0 failures, 0 errors** |
| `tests/test_phase4_scheduling.py` | **18 tests, 18 OK** |
| `tests/test_phase4_1_patch.py` | **21 tests, 21 OK** |
| `PYTHONPATH=. .venv/bin/python -m unittest discover -s text_bridge/tests -v` | **10 tests, 10 OK, 0 failures, 0 errors** |
| `node --check web/assets/app.js` | **OK** |
| HTTP smoke | **PASS**：health=200、routine PUT=200、draft=201、approve=200、preview=200、scheduler=200 |

Phase 4.1 新增测试包含 resolved anchor/routine version、历史 snapshot 不变、相同 routine PUT 幂等、routine 版本递增、单事务故障注入与重启一致性、M2 BLOCK、所有 Schedule Type 的 effective range、跨日/interval/cycle 边界、preview/正式 generation 一致、重复重算 stable identity、不重复以及旧 schema migration。

完整修补说明见 [`docs/phase4_1_patch_report.md`](phase4_1_patch_report.md)。

## 11. 当前限制与下一步

当前不做自动 PRN trigger、复杂临床 cycle、DST/global timezone、真实药学数据和剂量调整。下一阶段可在不改变 ScheduleExpander contract 的前提下增加受控 PRN trigger、更多 routine anchors 和 provider-backed clinical rules。
