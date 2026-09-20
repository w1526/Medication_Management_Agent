# Phase 2 M6 实现报告

## 1. 实现结论

本阶段已在现有 Phase 1 SQLite、Event Log、Transactional Outbox 和 Scheduler 之上完成确定性的异常升级与人工处置闭环：

```text
Plan → Occurrence → Reminder → completed
→ no user response → closed_unconfirmed
→ CAREGIVER → FAMILY → MANUAL_REVIEW
→ acknowledge → resolve
```

M6 不调用 LLM，不根据药名推断风险，不修改处方，也不自动拨打电话、发送短信或呼叫急救。

## 2. M6 架构

- `escalation.py`：集中定义等级、状态、升级顺序、通知事件类型和 resolution code。
- `MedicationService._close_expired()`：在现有 occurrence 关闭事务中创建 escalation、首个 step、Event Log 和 Outbox。
- `MedicationService.process_due_escalations()`：读取持久化的 `next_escalation_at` 或 ACK 后的 `resolution_deadline_at`，推进 `CAREGIVER → FAMILY → MANUAL_REVIEW`。
- `domain_outbox`：继续作为所有外部通知的可靠边界。
- 本地 escalation publisher 只把 step 记录为 `simulated`；这表示本地 consumer 已处理，不表示护工或家属收到，为未来真实通知 adapter 保留 `delivered` seam。
- `SchedulerThread` 复用现有 `run_scheduler_cycle()`；不引入 Celery、Redis 或内存 timer。

## 3. 数据库变更

新增幂等 schema：

`medication_escalation`：保存 elder、occurrence、plan snapshot 关系、reason/source event、current level、状态、deadline、人工 actor 和 resolution。

`medication_escalation_step`：保存每个等级的 target role、通知动作、step 状态、计划时间、执行时间和通知 event_id。`UNIQUE(escalation_id, level)` 保证同一级只产生一次通知。

`UNIQUE(medication_escalation.occurrence_id)` 保证一个 occurrence 只有一条升级链。schema 继续使用现有启动时 `CREATE TABLE IF NOT EXISTS` 方式，并对 Phase 2.1 字段执行 nullable `ALTER TABLE` 增量迁移，不删除或重建已有数据库。

## 4. Trigger policy

- `closed_unconfirmed`：自动创建，默认从 `CAREGIVER` 开始。
- 同一老人、同一 plan_id/药品快照在 lookback 内达到 `ESCALATION_REPEAT_MISSED_COUNT`：初始等级为 `FAMILY`。
- 单次 `SKIP`：只保留原有 `skipped` 事实，不创建 escalation。
- reminder `failed`、`interrupted`、`expired`：只记录 delivery 问题，不修改 occurrence 服药状态。
- 已打开 escalation 不因 plan pause/revise 静默删除。

## 5. 状态机

```text
OPEN ── acknowledge ──> ACKNOWLEDGED ── resolve ──> RESOLVED
  │                              │
  ├── timeout: CAREGIVER → FAMILY → MANUAL_REVIEW
  └── explicit cancel ──> CANCELLED
                                 └── resolution timeout → next level
```

ACK 设置新的 `resolution_deadline_at`，不再沿用原 response deadline。ACK 超时会恢复为 `OPEN` 并升级，同时保留 ACK 历史；`MANUAL_REVIEW` 没有后续自动等级。RESOLVE 必须携带 `TAKEN_VERIFIED`、`NOT_TAKEN`、`REFUSED`、`NOT_FOUND`、`DEVICE_ERROR` 或 `OTHER` 之一及可选 note。`RESOLVED`、`CANCELLED` 不允许重新打开或继续升级。

## 6. 事件与 Outbox

已加入：

- `medication.escalation.opened`
- `medication.escalation.escalated`
- `caregiver.task.assign`
- `family_notify.request`
- `manual_review.request`
- `medication.escalation.acknowledged`
- `medication.escalation.resolved`
- `medication.escalation.cancelled`
- `medication.intake.late_verified`

每个事件含有 escalation、elder、occurrence、plan、reason、level、timestamp 和 trace 关联；通知 payload 使用 occurrence 的药名/剂量 snapshot，不让 LLM 生成事实。

## 7. API

```text
GET  /api/v1/medication/escalations
GET  /api/v1/medication/escalations/{id}
GET  /api/v1/medication/escalations/summary
POST /api/v1/medication/escalations/run
POST /api/v1/medication/escalations/{id}/acknowledge
POST /api/v1/medication/escalations/{id}/resolve
POST /api/v1/medication/escalations/{id}/cancel
POST /api/v1/medication/occurrences/{id}/confirm
```

`actor_id`/`actor_role` 目前只是审计字段；认证与 RBAC 留给后续阶段。正常窗口 confirm 写 `confirmed_taken`；超时关闭后 confirm 写独立的 late verification，不覆盖 `closed_unconfirmed`。

## 8. 幂等与重启恢复

- occurrence 唯一约束、step 唯一约束、Outbox dedup key 和 event_id 四层去重。
- 重复 scheduler、进程重启、Outbox retry 都不会产生第二条 escalation 或同级通知。
- `next_escalation_at` 和 ACK 后的 `resolution_deadline_at` 都持久化，重启后 scheduler 可继续处理。
- ACK/RESOLVE/CANCEL 重复 event_id 返回 `duplicate=true`，不会二次写状态或事件。

## 9. 与 occurrence 的边界

`completed != confirmed_taken`、`delivery failed != missed` 继续成立。`resolve(resolution_code=TAKEN_VERIFIED)` 不修改 occurrence。正常窗口内的显式 `POST /api/v1/medication/occurrences/{id}/confirm` 写 `medication.intake.updated` 并进入 `confirmed_taken`；如果 occurrence 已是 `closed_unconfirmed`，同一接口写 `medication.intake.late_verified` 和独立核实字段，保留原超时事实；之后再独立 ACK/RESOLVE escalation。

## 10. Web 测试台

现有三角色页面新增 M6 卡片，展示药物 snapshot、reason、level、status、opened_at，并在 OPEN/ACKNOWLEDGED 分别展示升级截止或处理截止；本地通知使用“模拟派发”文案，不显示已送达。页面继续使用原生 JS/现有 Tabler 样式，没有引入前端框架。

## 11. 配置说明

`ESCALATION_ENABLED`、护工/家属 timeout、ACK resolution timeout、重复 missed count/lookback、skip 配置和 silence window 已加入 `.env.example`。这些值是工程测试默认值，不是临床指南；本阶段只实现 closed_unconfirmed 的自动触发，skip 统计配置为未来 policy 预留。

## 12. 测试结果

本次 `tests/test_phase2_escalation.py` 共 24 个测试，除原有自动创建、唯一去重、重启、CAREGIVER/FAMILY/MANUAL_REVIEW、ACK/RESOLVE 幂等、单次 SKIP、重复 missed、completed 边界、delivery failure、人工确认、计划暂停、HTTP API、CANCELLED 和终态拒绝外，还覆盖 ACK resolution deadline、两级 ACK 超时升级、late verification、正常窗口 confirm、本地 simulated 派发，以及自动升级/ACK/RESOLVE 失败注入回滚。

最终验证命令及结果：

```text
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
→ 52 tests, OK

PYTHONPATH=. .venv/bin/python -m unittest discover -s text_bridge/tests -v
→ 10 tests, OK

node --check web/assets/app.js
→ OK
```

## 13. HTTP smoke test

已通过进程内 `Application` smoke 验证计划审批、scheduler、deadline close、escalation list/detail/summary、acknowledge 和 resolve。待部署服务可使用：

```text
POST /api/v1/medication/scheduler/run
POST /api/v1/medication/escalations/run
GET  /api/v1/medication/escalations?elder_id=E001
```

## 14. 尚未完成能力与 Phase 3 建议

当前未实现真实家属 App、短信、电话、认证/RBAC、跨 occurrence 聚合、M2 药物安全/ DDI、真实 overdose detection、自动急救、LLM 风险评级和 M7 行为分析。

建议 Phase 3 先补可信身份和真实 Notification Adapter，再定义经过 M2/M5 证明的 P0/P1 内部事件；不要从药名或自然语言推断紧急程度。
