# Phase 5.1 闭环修补 Implementation Report

日期：2026-09-22

## 1. 修复内容

本阶段完成了 M5 → M6 人工复核闭环修补：

- M5 assessment 在 `review_required=true` 时，事务内直接调用 `_open_manual_review_escalation_in_transaction()`。
- 新任务直接创建为：
  - `current_level = MANUAL_REVIEW`
  - `needs_manual_review = true`
  - `status = OPEN`
- `reason` 按来源区分：
  - `EVIDENCE_CONFLICT`
  - `EXCESS_REMOVAL_SUSPECTED`
- 复用了既有 `medication_escalation` 与 `medication_escalation_step`，没有新增平行 Review 表。
- M5 的 `manual_review.request` 被复用为 M6 MANUAL_REVIEW step 的通知事件，避免产生重复的人工复核请求。
- `get_plan(plan_id, version=None)` 现在按 `version DESC LIMIT 1` 返回最新版；指定版本仍返回指定版本。

## 2. M5 → M6 闭环

现在的执行路径为：

~~~text
Evidence
  → M5 assessment
  → conflict / review_required event
  → manual_review.request
  → 同一 SQLite transaction 内创建 medication_escalation
  → 创建 MANUAL_REVIEW escalation step
  → Event Log + Outbox
  → GET escalations / GET escalation detail
  → ACKNOWLEDGED
  → RESOLVED
  → 审计事件
~~~

核心任务创建发生在 M5 domain transaction 内，不依赖 Outbox publisher 或未来的 Redis consumer。

Outbox publisher 继续负责本地通知/模拟派发；当 `manual_review.request` 被本地 publisher 处理后，关联 step 会标记为 `simulated`。

## 3. 现有 M6 模型复用情况

没有新增：

- `manual_review_task`
- `review_job`
- `confirmation_review`

现有接口直接可用：

~~~text
GET  /api/v1/medication/escalations
GET  /api/v1/medication/escalations/{id}
POST /api/v1/medication/escalations/{id}/acknowledge
POST /api/v1/medication/escalations/{id}/resolve
~~~

M5 产生的 escalation 与 Phase 2 missed-dose escalation 共用同一查询、step、ACK、RESOLVE 和审计状态机。

## 4. 状态变化

### 新建 M5 人工复核任务

~~~text
status = OPEN
current_level = MANUAL_REVIEW
needs_manual_review = true
~~~

### ACK

~~~text
OPEN → ACKNOWLEDGED
acknowledged_by / acknowledged_at 写入
~~~

### RESOLVE

~~~text
ACKNOWLEDGED → RESOLVED
resolved_by / resolved_at / resolution_code / resolution_note 写入
~~~

RESOLVE 不修改 occurrence 的服药事实。例如：

~~~text
confirmed_taken 不会被改回 unconfirmed
closed_unconfirmed 不会被改成 confirmed_taken
~~~

人工复核任务只负责异常处置和留痕；人工修改服药事实仍需单独业务设计。

## 5. 幂等策略

- 同一个 `event_id` 重复提交 evidence 时，直接返回已存在 evidence 和 assessment，不重复写入。
- 现有 `medication_escalation.occurrence_id UNIQUE` 保证同一 occurrence 不产生第二条 escalation。
- 现有 `medication_escalation_step (escalation_id, level) UNIQUE` 保证同一 escalation 不重复创建 MANUAL_REVIEW step。
- 已有 `OPEN` 或 `ACKNOWLEDGED` escalation 遇到新的 M5 review：
  - 原地设置 `needs_manual_review=true`
  - 直接提升到 `MANUAL_REVIEW`
  - 保留原 escalation_id
  - 必要时添加 MANUAL_REVIEW step
- 已有 `RESOLVED`、`CANCELLED` 或其他终结状态时，不静默重开或覆盖历史；M5 review event 仍会保留审计记录。

## 6. 事务边界

以下写入在同一个 SQLite transaction 中完成：

~~~text
Evidence
Assessment
M5 assessed/conflict/review events
M5 manual_review.request
M6 medication_escalation
M6 medication_escalation_step
相关 Event Log
相关 Outbox
Occurrence 状态应用
~~~

如果 escalation 创建或 step 创建失败，整个 transaction rollback，避免出现：

~~~text
review_required=true
但没有可查询的 M6 task
~~~

本阶段没有修改 schema、没有删除表、没有重建数据库，也没有改变 Evidence / Assessment immutable 约束。

## 7. 新增测试

新增：

~~~text
tests/test_phase5_1_closure.py
~~~

覆盖 9 个场景：

1. Evidence conflict 直接创建 MANUAL_REVIEW escalation；
2. `EXCESS_REMOVAL_SUSPECTED` 创建人工复核任务；
3. 重复 evidence event 不重复创建 evidence、assessment、escalation；
4. 同一 occurrence 多次 conflict 仍只有一条 escalation；
5. M5 escalation 可 ACK；
6. M5 escalation 可 RESOLVE 且不修改 occurrence 历史；
7. escalation 创建失败时 Evidence、Assessment、review event、escalation 全部 rollback；
8. 已有 missed-dose escalation 时原地提升，不插入第二条；
9. `get_plan()` 默认返回最新版，同时支持指定旧版本。

另外验证了 manual-review outbox 发布后关联 step 会进入本地 `simulated` 状态。

## 8. 完整测试结果

按任务要求执行：

~~~text
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
~~~

结果：

~~~text
Ran 155 tests
OK
~~~

~~~text
PYTHONPATH=. .venv/bin/python -m unittest discover -s text_bridge/tests -v
~~~

结果：

~~~text
Ran 10 tests
OK
~~~

~~~text
node --check web/assets/app.js
~~~

结果：通过，无输出。

## 9. 当前仍未实现的内容

按 Phase 5.1 范围，本阶段没有实现：

- 认证、RBAC、JWT、deviceSn 鉴权；
- `identity_trusted` / `source_trusted` 强校验策略；
- LLM confidence threshold；
- UNKNOWN / UNCERTAIN 意图；
- 临床药物数据库、真实 DDI、allergy、contraindication；
- Phase 4 每日剂量安全推导；
- Outbox 指数退避；
- Redis、PostgreSQL、LiveKit、真实音箱和真实传感器；
- 人工复核后修改服药事实的独立业务流程。

## 10. 人工 E2E 验收步骤

使用独立测试数据库启动服务，避免污染已有数据：

~~~bash
PYTHONPATH=src .venv/bin/python -m medication_reminder \
  --db /tmp/medication-phase51.db --port 18080
~~~

然后：

1. 创建一个当天计划，填写 `elder_id=E001`、药品、剂量和当前时间附近的 `schedule_time`。
2. 提交计划并审批，使计划进入 `active`。
3. 执行 scheduler，确认 occurrence 生成 reminder/interaction。
4. 在老人端点击“吃了”，或调用：

   ~~~text
   POST /api/v1/medication/responses
   ~~~

   使用 occurrence 的 `interaction_id` 和 `action=CONFIRM_TAKEN`。
5. 模拟传感器冲突：

   ~~~text
   POST /api/v1/medication/occurrences/{occurrence_id}/evidence
   ~~~

   使用：

   ~~~json
   {
     "event_id": "manual-phase51-no-weight-1",
     "elder_id": "E001",
     "source_type": "SENSOR",
     "evidence_type": "NO_WEIGHT_CHANGE",
     "value": {"simulated": true}
   }
   ~~~

6. 查询：

   ~~~text
   GET /api/v1/medication/escalations?elder_id=E001
   ~~~

   应看到：

   ~~~text
   current_level = MANUAL_REVIEW
   needs_manual_review = true
   reason = EVIDENCE_CONFLICT
   status = OPEN
   ~~~

7. 使用 escalation_id 调用：

   ~~~text
   POST /api/v1/medication/escalations/{id}/acknowledge
   ~~~

   请求体至少包含：

   ~~~json
   {
     "actor_id": "caregiver-001",
     "actor_role": "caregiver"
   }
   ~~~

8. 再调用：

   ~~~text
   POST /api/v1/medication/escalations/{id}/resolve
   ~~~

   请求体至少包含：

   ~~~json
   {
     "actor_id": "doctor-001",
     "actor_role": "doctor",
     "resolution_code": "TAKEN_VERIFIED",
     "resolution_note": "人工复核完成"
   }
   ~~~

9. 最后检查：

   ~~~text
   GET /api/v1/medication/events?occurrence_id={occurrence_id}
   ~~~

   应能看到 Evidence、Assessment、Conflict、Review、M6 opened、ACK 和 RESOLVE 的完整审计链路。

本阶段未创建 git commit，也未 push。
