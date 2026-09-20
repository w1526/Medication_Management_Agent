# Phase 2：M6 异常升级与人工处置闭环

## 目标与边界

M6 只处理已经发生、可审计的软件事实，不判断药物风险，也不根据药名推断优先级。当前自动触发事实是 `occurrence.intake_status=closed_unconfirmed`；单次 `SKIP` 和 reminder delivery failure 不会被当作漏服或紧急事件。

完整链路为：

```text
confirmation deadline
→ closed_unconfirmed
→ medication.escalation.opened
→ caregiver.task.assign
→ timeout
→ family_notify.request
→ timeout
→ manual_review.request
→ acknowledge
→ resolution deadline
→ resolve 或继续升级
```

`completed` 仍然只是 reminder delivery 事实，`resolve` 也不会把 occurrence 改成 `confirmed_taken`。现场确认服药要调用独立的人工确认 domain/API，然后再 resolve escalation。

## M6 状态与升级等级

等级按行为事实确定，不含医疗风险评分：

| 等级 | 自动动作 |
| --- | --- |
| `CAREGIVER` | 发布 `caregiver.task.assign` |
| `FAMILY` | 发布 `family_notify.request` |
| `MANUAL_REVIEW` | 设置 `needs_manual_review`，发布高优先级 `manual_review.request` |

常规流程不会自动产生 `P0`、`EMERGENCY` 或 `emergency_alert.trigger`。相关常量仅为未来可信内部事件契约预留。

升级主表状态为 `OPEN`、`ACKNOWLEDGED`、`RESOLVED`、`CANCELLED`、`EXHAUSTED`。当前自动链在 `MANUAL_REVIEW` 停止，状态保持 `OPEN`，因此人工仍可 ACK/RESOLVE；`next_escalation_at` 置空。

```text
OPEN ── acknowledge ──> ACKNOWLEDGED ── resolve ──> RESOLVED
  │
  ├── timeout: CAREGIVER → FAMILY → MANUAL_REVIEW
  ├── acknowledge: 设置 resolution_deadline_at
  ├── resolution timeout: 继续 CAREGIVER → FAMILY → MANUAL_REVIEW
  └── explicit cancel ──> CANCELLED
```

ACK 代表“有人看到并接手”，不会停止监督：它清除原 response deadline，设置新的 `resolution_deadline_at`。处理时限内 RESOLVE 才结束；ACK 超时会恢复为 `OPEN` 并升级，历史 ACK actor 保留。`MANUAL_REVIEW` 没有后续自动等级，等待人工处理。RESOLVE 必须有 resolution code，不修改 occurrence。

## Trigger policy

- `closed_unconfirmed`：默认创建一个 escalation；同一 occurrence 由 `UNIQUE(occurrence_id)` 保证只有一条链。
- 最近配置窗口内，同一老人、同一计划和药品快照出现达到 `ESCALATION_REPEAT_MISSED_COUNT` 的 `closed_unconfirmed` 时，初始等级直接为 `FAMILY`。
- 单次 `skipped`：只记录，不自动创建 escalation。
- `failed`/`interrupted`/`expired`：只保留 delivery 事实和事件日志，不等同于漏服。
- 计划暂停/修订只处理未来 occurrence；已打开 escalation 不会被静默删除。

## 事件契约

所有事件同时写入 Event Log 和 Transactional Outbox。通知事件 payload 包含：

```json
{
  "escalation_id": "escalation_...",
  "elder_id": "E001",
  "occurrence_id": "occ_...",
  "plan_id": "plan_...",
  "level": "CAREGIVER",
  "reason": "closed_unconfirmed",
  "scheduled_at": "2026-09-20T00:00:00+00:00",
  "opened_at": "2026-09-20T02:01:00+00:00",
  "respond_before": "2026-09-20T02:31:00+00:00",
  "medication": {"name": "药品快照", "dose": "剂量快照"}
}
```

已实现的事件：

- `medication.escalation.opened`
- `medication.escalation.escalated`
- `caregiver.task.assign`
- `family_notify.request`
- `manual_review.request`
- `medication.escalation.acknowledged`
- `medication.escalation.resolved`
- `medication.escalation.cancelled`
- `medication.intake.late_verified`

当前通知 publisher 是本地可审计 sink：Outbox 消费成功后把 escalation step 标记为 `simulated`，不声称护工或家属已经收到；真实通道回执未来才可使用 `delivered`。

## API

```text
GET  /api/v1/medication/escalations?elder_id=E001&status=OPEN&level=CAREGIVER
GET  /api/v1/medication/escalations/{id}
GET  /api/v1/medication/escalations/summary?elder_id=E001
POST /api/v1/medication/escalations/run
POST /api/v1/medication/escalations/{id}/acknowledge
POST /api/v1/medication/escalations/{id}/resolve
POST /api/v1/medication/escalations/{id}/cancel
POST /api/v1/medication/occurrences/{id}/confirm
```

ACK 请求：

```json
{"actor_id":"caregiver-001","actor_role":"caregiver","event_id":"ack-001"}
```

RESOLVE 请求：

```json
{
  "actor_id": "caregiver-001",
  "actor_role": "caregiver",
  "resolution_code": "NOT_TAKEN",
  "resolution_note": "老人拒绝服药",
  "event_id": "resolve-001"
}
```

`actor_id`/`actor_role` 当前只是审计字段，不代表已经完成认证或 RBAC。正常确认窗口内调用 occurrence confirm 会写 `confirmed_taken`；如果 occurrence 已是 `closed_unconfirmed`，同一接口只写迟到核实字段和 `medication.intake.late_verified`，保留原超时事实。`TAKEN_VERIFIED` 只描述人工处置结果；需要更新服药事实时，再独立 resolve escalation。

## 重启、幂等和事务

- occurrence 关闭、escalation 创建、step、Event Log 和 Outbox 在 `_close_expired()` 的同一 SQLite 事务内提交。
- CAREGIVER/FAMILY/MANUAL_REVIEW 自动升级，以及 ACK/RESOLVE/CANCEL 的领域状态、step、Event Log 和 Outbox 在同一事务内提交；任一步失败都会回滚。
- `medication_escalation.occurrence_id` 唯一，Outbox `dedup_key` 按 escalation/level 唯一。
- ACK/RESOLVE/CANCEL 使用调用方 `event_id` 写入 Event Log；重复 event_id 返回同一结果，不重复状态变化或通知。
- `next_escalation_at` 与 ACK 后的 `resolution_deadline_at` 都持久化在数据库；scheduler 每轮调用 `process_due_escalations()`，进程重启后可以继续。
- 每个 escalation level 只能有一个 `medication_escalation_step`。

## 配置

`.env.example` 中的 timeout、重复异常阈值和 lookback 是工程测试默认值，不是临床指南，也不能作为医疗标准解释：

```dotenv
ESCALATION_ENABLED=1
ESCALATION_CAREGIVER_TIMEOUT_MINUTES=30
ESCALATION_FAMILY_TIMEOUT_MINUTES=60
ESCALATION_ACK_RESOLUTION_TIMEOUT_MINUTES=30
ESCALATION_REPEAT_MISSED_COUNT=2
ESCALATION_REPEAT_MISSED_LOOKBACK_HOURS=24
ESCALATION_SKIP_TRIGGER_COUNT=3
ESCALATION_SKIP_LOOKBACK_HOURS=24
ESCALATION_SILENCE_WINDOW_MINUTES=0
```

## 当前未实现与后续

当前未接入真实通知通道、认证/RBAC、短信/电话、急救、LLM 风险评级、DDI、药物关键性数据库和跨 occurrence 聚合。Phase 3 可先补认证后的 actor identity、真实通知 adapter、显式取消/人工复核队列，再由 M2/M5 提供可信的 P0/P1 内部事件。
