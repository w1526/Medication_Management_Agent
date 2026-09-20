# 养老智能体 OS：用药提醒智能体 MVP 实施方案 V2.1

> 基于《养老智能体 OS：用药提醒智能体及其技能完整设计方案 V2.0》与第一版 `medication_scheduling_agent_implementation.md` 收敛  
> 目标：只实现第一阶段 MVP，优先打通“录入 → 定时提醒 → 确认 → 记录 → 家属可见”主线  
> 版本日期：2026-09-10

---

## 1. 本版结论

第一阶段不实现老师 V2.0 中的全部能力，而是保留总体架构思想，先完成一个**可可靠运行、可恢复、可审计的用药提醒闭环 MVP**。

MVP 继续遵循以下原则：

- **LiveKit Chat Agent 仍是老人侧唯一自然语言交互入口**，不增加 Supervisor Agent。
- **Medication Reminder Skill / Medication Scheduling Agent 独立运行**，不直接进入 LiveKit Room，不直接调用 TTS，不共享 Chat Agent 完整对话历史。
- Chat Agent 与 Medication Skill **只通过结构化 Event/API 交互**。
- 老师 V2.0 第一阶段的核心模块仍以 **M1 + M3 + M4 + M5** 为主：
  - M1：用药计划管理
  - M3：提醒调度
  - M4：提醒触达请求
  - M5：服药确认与记录
- **定时、超时、状态流转、任务恢复由确定性代码完成**；LLM/DeepSeek Harness 只处理自然语言理解、歧义消解和结构化参数提取。
- 第一阶段暂不实现 M2 药物相互作用、完整 M6 异常升级、M7 反思、自进化、FHIR、智能药盒和向量记忆。
- 为避免 MVP 后续返工，第一阶段必须同时补上最小可靠性能力：**计划版本、任务绑定、事件幂等、任务恢复、审计日志**。

一句话概括：

> **第一阶段先做“可靠的智能用药提醒闭环”，而不是一次性做完整的智能用药守护体系。**

---

## 2. MVP 要实现什么

### 2.1 核心用户流程

```text
家属/医护录入用药计划
        ↓
生成 Plan Draft
        ↓
最小审批/确认
        ↓
Plan Active
        ↓
生成 MedicationOccurrence
        ↓
到点 Scheduler 触发
        ↓
发送 medication.reminder_due
        ↓
LiveKit 音箱主动播报
        ↓
老人：吃了 / 晚点 / 跳过
        ↓
更新 Occurrence + 记录 Intake Record
        ↓
家属/平台可查询
```

第一阶段完成后，系统至少能回答下面五个问题：

1. **这个老人今天几点该吃什么药？**
2. **到点以后音箱有没有正常提醒？**
3. **老人回复了“吃了”“晚点”“跳过”中的哪一种？**
4. **这一剂药目前是什么状态？**
5. **系统重启后，未完成的任务还能不能恢复？**

---

## 3. 第一阶段范围

### 3.1 本阶段必须实现

| 能力 | 对应 V2.0 模块 | MVP 是否实现 |
|---|---|---:|
| 用药计划创建/查看/暂停 | M1 | ✅ |
| Plan Draft + 最小确认 | M1 + HITL 最小子集 | ✅ |
| 固定时间每日调度 | M3 | ✅ |
| MedicationOccurrence | M3 / 数据层 | ✅ |
| 到点事件 | M3 | ✅ |
| LiveKit 主动播报 | M4 | ✅ |
| 吃了 | M5 | ✅ |
| 晚点提醒 | M5 + M3 | ✅ |
| 跳过 | M5 | ✅ |
| 未确认关闭 | M3/M5 | ✅ |
| 家属/平台查询今日用药状态 | M5 / Status API | ✅ |
| 审计 Event Log | 基础设施 | ✅ |
| 服务重启任务恢复 | 基础设施 | ✅ |
| 重复事件幂等 | 基础设施 | ✅ |
| 计划版本与旧任务失效 | 基础设施 | ✅ |

### 3.2 本阶段明确不做

- ❌ M2 Drug-Drug Interaction
- ❌ DrugBank / OpenFDA
- ❌ FHIR / EHR 同步
- ❌ 智能药盒
- ❌ M7 Reflection Sub-agent
- ❌ 向量数据库 / Semantic Memory
- ❌ Prompt Evolution
- ❌ 自动调整用药时间
- ❌ 完整 L3/L4 家属/护士站升级
- ❌ 动态餐时调度
- ❌ 复杂周期规则
- ❌ 多智能体内部协商

---

## 4. 总体架构

```text
老人
 │
 ▼
┌──────────────────────────────────────────────┐
│ LiveKit Chat Runtime                         │
│ VAD → ASR → Chat Agent / Fast Path → TTS     │
│                                              │
│ - 老人侧唯一交互入口                         │
│ - 接收 Medication Reminder 事件并播报        │
│ - 将老人回复转为结构化 response              │
└───────────────────┬──────────────────────────┘
                    │
             structured event
                    │
                    ▼
┌──────────────────────────────────────────────┐
│ Redis Streams / Agent Event Bus              │
│                                              │
│ medication.commands                          │
│ medication.events                            │
│ device.events                                │
└───────────────────┬──────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────────┐
│ Medication Reminder Skill                    │
│                                              │
│ Plan Service                                 │
│ Occurrence Engine                            │
│ Scheduler                                    │
│ Intake Service                               │
│ Reminder Request Builder                     │
│ Repository                                   │
│ Audit + Outbox                               │
│                                              │
│ Optional Semantic Adapter                    │
│ └─ DeepSeek Harness / LLM                    │
└───────────────────┬──────────────────────────┘
                    │
                    ▼
               PostgreSQL
```

### 4.1 核心边界

**Chat Agent 是唯一对话入口，但不是用药业务真值。**

**Medication Reminder Skill 是唯一用药业务真值，但不是对话入口。**

---

## 5. LLM / Harness 在 MVP 中的职责

### 5.1 可以做

- 从自然语言中提取：
  - 药名候选
  - 剂量文本
  - 时间
  - 餐前/餐后描述
- 理解：
  - “吃了”
  - “等一哈”
  - “晚半个小时”
  - “今天不吃”
- 输出结构化结果。

示例：

```json
{
  "action": "delay",
  "delay_minutes": 30,
  "confidence": 0.96
}
```

### 5.2 不可以做

- ❌ 判断“现在是不是到点”
- ❌ 决定任务是否超时
- ❌ 自己修改 Plan 状态
- ❌ 自己修改 Occurrence
- ❌ 自己决定老人是否漏服
- ❌ 自己改变服药时间
- ❌ 自己生成相互作用结论
- ❌ 自己修改升级阈值

### 5.3 快路径

当前处于 `medication_interaction` 时：

```text
“吃了”     → CONFIRM_TAKEN
“晚点”     → DELAY
“半小时后” → DELAY(30)
“跳过”     → SKIP
“再说一遍” → REPEAT
```

可以优先走本地 fast path；只有无法确定时再调用 LLM。

---

## 6. 核心数据模型

### 6.1 medication_plan

```text
id
elder_id
version
drug_name
dosage_text
route
schedule_type
schedule_time
timezone
relation_to_meal
start_date
end_date
status
source
created_by
approved_by
effective_from
created_at
updated_at
```

MVP `status`：

```text
draft
pending_confirmation
active
paused
completed
```

### 6.2 计划版本原则

关键字段修改时不直接覆盖历史版本。

```text
Plan v1
  ↓ 修改
Plan v2
```

`MedicationOccurrence` 必须保存：

```text
plan_id
plan_version
```

已完成的历史记录继续绑定旧版本。

### 6.3 medication_occurrence

每一次具体服药任务。

```text
id
plan_id
plan_version
elder_id

scheduled_at
confirmation_deadline_at
next_reminder_at

intake_status

actual_time
confirmation_method

reminder_count
snooze_count

drug_name_snapshot
dosage_snapshot
relation_to_meal_snapshot

created_at
updated_at
```

MVP `intake_status`：

```text
unconfirmed
confirmed_taken
skipped
closed_unconfirmed
```

注意：

`delayed` 不作为最终服药事实，而通过 `next_reminder_at` + `snooze_count` 表达。

### 6.4 reminder_attempt

单独记录一次提醒是否真正完成投递。

```text
id
occurrence_id
level
scheduled_at
delivery_status
started_at
completed_at
failure_reason
created_at
```

`delivery_status`：

```text
queued
dispatched
started
completed
failed
interrupted
expired
```

这样可以区分：

```text
事件已发送
≠ 音箱收到
≠ TTS 播放完成
≠ 老人服药
```

### 6.5 medication_interaction

用于将“老人回复”可靠绑定到具体任务。

```text
id
elder_id
device_sn
occurrence_id
opened_at
expires_at
status
created_at
```

MVP 规定：

> 一个 interaction 默认只绑定一个 occurrence。

同一时间多种药的批量确认放到后续版本。

### 6.6 medication_event_log

```text
id
event_id
elder_id
plan_id
occurrence_id
event_type
source
payload_json
occurred_at
received_at
processed_at
created_at
```

### 6.7 domain_outbox

```text
id
event_id
event_type
payload_json
status
retry_count
created_at
published_at
```

用于保证：

```text
数据库状态更新
+
待发布事件
```

在同一个数据库事务里提交。

---

## 7. 调度规则

### 7.1 MVP 只支持

```text
timezone = Asia/Shanghai
schedule_type = daily
固定时间 HH:MM
start_date 必填
end_date 可选
```

示例：

```json
{
  "schedule_type": "daily",
  "schedule_time": "08:00",
  "timezone": "Asia/Shanghai"
}
```

### 7.2 暂不支持

- 餐后动态时间
- 起床后自动计算
- 隔日/复杂 cron
- PRN 按需用药
- 动态作息自适应

`relation_to_meal` 在 MVP 中仅作为提醒展示信息，不参与动态调度。

### 7.3 occurrence 生成

推荐滚动生成未来 7 天任务。

业务唯一键：

```text
(plan_id, plan_version, scheduled_at)
```

每天补充未来第 8 天，重启时重新检查并补齐。

---

## 8. 三个时间必须分开

每个 occurrence 至少保留：

```text
scheduled_at
confirmation_deadline_at
next_reminder_at
```

含义：

- `scheduled_at`：原定服药时间，不因“晚点提醒”而修改。
- `confirmation_deadline_at`：本次任务允许等待确认的业务截止时间。
- `next_reminder_at`：下一次提醒时间，可以因“晚点提醒”修改。

老人说：

> “半小时后再喊我。”

只修改：

```text
next_reminder_at
```

不修改：

```text
scheduled_at
confirmation_deadline_at
```

并增加：

```text
snooze_count
max_snooze_count
max_snooze_until
```

MVP 具体数值通过配置设定，不由 LLM 决定。

---

## 9. MVP 状态转换

### 9.1 业务状态

```text
unconfirmed
    │
    ├── CONFIRM_TAKEN
    │       ↓
    │ confirmed_taken
    │
    ├── SKIP
    │       ↓
    │    skipped
    │
    └── deadline 到期
            ↓
      closed_unconfirmed
```

“晚点”不改变最终业务状态：

```text
unconfirmed
   │
   └── DELAY
         ↓
修改 next_reminder_at
         ↓
仍然 unconfirmed
```

### 9.2 并发保护

状态更新采用条件更新，例如：

```sql
UPDATE medication_occurrence
SET intake_status = 'confirmed_taken'
WHERE id = :occurrence_id
  AND intake_status = 'unconfirmed';
```

超时关闭同理。

确保“老人确认”和“超时扫描”同时到达时，只有一个状态变更成功。

---

## 10. 事件协议

统一事件头：

```json
{
  "event_id": "uuid",
  "event_type": "...",
  "occurred_at": "2026-09-10T08:00:00+08:00",
  "source": "...",
  "elder_id": "E001",
  "payload": {}
}
```

### 10.1 创建计划

```json
{
  "event_type": "medication.plan.create_request",
  "elder_id": "E001",
  "source": "chat_agent",
  "payload": {
    "text": "每天早上八点提醒我吃氨氯地平5毫克"
  }
}
```

处理结果：

```text
LLM/Harness 解析
    ↓
MedicationPlan Draft
    ↓
pending_confirmation
```

未经确认不能直接变为 active。

### 10.2 到点提醒

```json
{
  "event_type": "medication.reminder_due",
  "elder_id": "E001",
  "payload": {
    "occurrence_id": "O001",
    "plan_id": "P001",
    "plan_version": 1,
    "drug_name": "氨氯地平",
    "dosage": "5mg",
    "relation_to_meal": "餐后"
  }
}
```

### 10.3 Device Interaction Request

Medication Skill 不直接 TTS，而是发布：

```json
{
  "event_type": "device.interaction.request",
  "elder_id": "E001",
  "payload": {
    "interaction_type": "medication_reminder",
    "interaction_id": "I001",
    "occurrence_id": "O001",
    "text": "王大爷，到吃药时间了哈。氨氯地平5毫克，餐后服用。吃了跟我说一声。"
  }
}
```

### 10.4 老人回复

```json
{
  "event_type": "medication.user_response",
  "elder_id": "E001",
  "payload": {
    "interaction_id": "I001",
    "occurrence_id": "O001",
    "text": "我吃了",
    "action": "CONFIRM_TAKEN"
  }
}
```

`occurrence_id` 必须由可信 interaction 上下文带入，不能让 LLM 自己猜。

### 10.5 Intake 更新

```json
{
  "event_type": "medication.intake.updated",
  "elder_id": "E001",
  "payload": {
    "occurrence_id": "O001",
    "status": "confirmed_taken",
    "actual_time": "2026-09-10T08:06:00+08:00"
  }
}
```

---

## 11. 到点提醒完整流程

```text
Scheduler 扫描
    ↓
找到 next_reminder_at <= now()
    ↓
锁定 / 原子领取 occurrence
    ↓
创建 ReminderAttempt
    ↓
DB Transaction:
  写 reminder attempt
  写 outbox event
    ↓
COMMIT
    ↓
Outbox Worker
    ↓
Redis Streams
    ↓
Device Event Adapter
    ↓
elder_id -> deviceSn
    ↓
LiveKit Runtime
    ↓
TTS 播报
    ↓
Delivery ACK
    ↓
更新 ReminderAttempt
```

---

## 12. 老人确认流程

```text
老人：“吃了”
    ↓
LiveKit ASR
    ↓
当前 interaction = medication_reminder
    ↓
Fast Path / LLM
    ↓
CONFIRM_TAKEN
    ↓
medication.user_response
    ↓
Medication Service
    ↓
校验：
- elder_id
- occurrence_id
- interaction_id
- interaction 未过期
- occurrence 仍 unconfirmed
    ↓
状态更新
    ↓
confirmed_taken
    ↓
写 Event Log
```

---

## 13. “晚点提醒”流程

```text
老人：“半个小时后再喊我”
    ↓
解析 delay=30
    ↓
检查：
- occurrence 仍 unconfirmed
- 未超过 max_snooze_count
- 新 next_reminder_at 未超过 max_snooze_until
    ↓
更新 next_reminder_at
    ↓
snooze_count + 1
    ↓
保持 intake_status = unconfirmed
```

明确：

> “晚点提醒”只改变提醒时间，不表示系统允许修改医嘱服药时间。

---

## 14. 未响应流程

MVP 不实现完整 M6 L3/L4 升级，但不能无限提醒。

```text
confirmation_deadline_at 到期
    ↓
occurrence 仍 unconfirmed
    ↓
closed_unconfirmed
    ↓
记录 medication.intake.unconfirmed
    ↓
家属/平台状态页显示“未确认”
```

第二阶段再接：

```text
family_notify.request
emergency_alert.request
```

---

## 15. 最小 HITL

老师 V2.0 要求计划录入确认。MVP 保留最小版本。

### 15.1 规则

老人语音创建计划：

```text
只能生成 Draft
```

家属/医护或测试管理端确认：

```text
Draft
  ↓
pending_confirmation
  ↓
approve
  ↓
active
```

审批需要记录：

```text
approved_by
approved_at
plan_version
```

关键字段被修改后：

```text
重新进入 pending_confirmation
```

### 15.2 MVP UI

不要求完整家属 APP。

第一阶段允许使用：

- 简单 Web 管理页
- Swagger/API
- 内部管理接口

完成最小审批闭环即可。

---

## 16. 事件可靠性

### 16.1 Transactional Outbox

所有“状态更新 + 发事件”操作：

```text
同一 DB transaction
```

示例：

```text
BEGIN

UPDATE medication_occurrence ...

INSERT domain_outbox ...

INSERT medication_event_log ...

COMMIT
```

避免：

```text
DB 已更新
但 Redis 事件没发出去
```

### 16.2 Redis Streams

采用 Consumer Group：

```text
XREADGROUP
→ process
→ DB commit
→ XACK
```

消费者故障后：

```text
PEL
→ 超时 reclaim
→ retry
```

### 16.3 幂等

每个事件必须有唯一：

```text
event_id
```

消费者维护已处理事件或使用业务唯一键保证幂等。

不能把整个 occurrence 只用一个幂等键，因为同一 occurrence 合法存在多次提醒。

---

## 17. 计划暂停/修改

### 17.1 暂停

Plan `active -> paused` 后：

- 不再生成新 occurrence；
- 所有尚未开始的未来 occurrence 标记失效；
- 已入队但尚未播报的事件，在消费前做有效性校验；
- 已完成历史记录不修改。

### 17.2 修改

关键字段修改：

```text
Plan v1
  ↓
创建 Plan v2
  ↓
v2 审批
  ↓
effective_from 生效
```

v2 生效以后：

- 取消 v1 的未来未开始任务；
- 生成 v2 新任务；
- 历史记录保留 v1 快照。

---

## 18. 主动播报失败处理

MVP 至少区分：

```text
reminder event 已发布
device event 已接收
TTS 开始
TTS 完成
TTS 失败
TTS 被打断
```

过期提醒禁止在设备恢复后无条件集中重播。

收到待播报事件时检查：

```text
occurrence 仍有效？
plan_version 仍有效？
event 是否过期？
```

无效则：

```text
discard + audit
```

---

## 19. 进程与故障隔离

建议至少逻辑上拆成：

```text
Medication Domain Process
├── API
├── Scheduler
├── State / Intake
├── Repository
└── Outbox

Medication Semantic Process
└── DeepSeek Harness / LLM
```

必须满足：

```text
Semantic/Harness 挂掉
↓
已存在的 active plan
↓
Scheduler 仍然能够正常触发 reminder_due
```

也就是说：

> LLM 故障不能导致已建立的用药提醒消失。

---

## 20. MVP 开发阶段

### Phase 0：主动提醒链路验证

目标：

```text
后台事件
→ elder_id/person_id
→ deviceSn
→ LiveKit
→ 无活跃聊天时主动播报
```

验收：

- 建立 5 分钟后测试任务；
- 当前聊天结束；
- 到点音箱仍能主动播报；
- 能收到播报完成/失败结果。

---

### Phase 1：Plan + Occurrence + Scheduler

实现：

- medication_plan
- plan_version
- medication_occurrence
- 固定时间规则
- 未来 7 天 occurrence 滚动生成
- due 扫描器
- PostgreSQL
- 基础 API

验收：

```text
创建 approved 测试计划
→ 自动产生 occurrence
→ 到点被扫描
```

---

### Phase 2：LiveKit 提醒闭环

实现：

- reminder_due
- device.interaction.request
- Device Event Adapter
- medication_interaction
- 吃了
- 晚点
- 跳过
- reminder_attempt

验收：

```text
创建计划
→ 到点音箱提醒
→ 老人说“吃了”
→ 只关闭正确 occurrence
```

---

### Phase 3：可靠性

实现：

- event_log
- outbox
- Redis Consumer Group
- 幂等
- 并发状态保护
- 重启恢复
- 旧事件失效

验收：

- DB commit 后 Redis 发布前强制崩溃；
- Redis 发布后 consumer commit 前强制崩溃；
- 重复消息；
- 两个 scheduler 同时扫描；
- 服务重启；
- 计划暂停后旧事件到达。

均不得导致重复业务状态或错误提醒。

---

### Phase 4：最小 HITL + 家属可见

实现：

- Draft / Pending Confirmation / Active
- 最小审批 API/UI
- 今日用药状态查询
- 已服 / 跳过 / 未确认
- 历史查询

完成后，即达到老师 V2.0 第一阶段：

```text
录入
→ 定时提醒
→ 确认
→ 记录
→ 家属可见
```

---

## 21. API 最小集合

```text
POST /api/v1/medication/plans/draft
POST /api/v1/medication/plans/{id}/approve
POST /api/v1/medication/plans/{id}/pause

GET  /api/v1/medication/plans
GET  /api/v1/medication/today
GET  /api/v1/medication/occurrences/{id}

POST /api/v1/medication/responses
POST /api/v1/medication/device-events

GET  /health/live
GET  /health/ready
```

---

## 22. 推荐目录结构

```text
medication-reminder/
├── src/
│   ├── api/
│   │   ├── plans.py
│   │   ├── responses.py
│   │   ├── device_events.py
│   │   └── status.py
│   │
│   ├── domain/
│   │   ├── plan.py
│   │   ├── occurrence.py
│   │   ├── reminder_attempt.py
│   │   ├── interaction.py
│   │   └── events.py
│   │
│   ├── services/
│   │   ├── plan_service.py
│   │   ├── scheduler_service.py
│   │   ├── intake_service.py
│   │   ├── reminder_service.py
│   │   └── occurrence_service.py
│   │
│   ├── infra/
│   │   ├── database.py
│   │   ├── repositories/
│   │   ├── redis_bus.py
│   │   ├── outbox.py
│   │   └── migrations/
│   │
│   ├── semantic/
│   │   ├── fast_path.py
│   │   ├── harness_adapter.py
│   │   └── schemas.py
│   │
│   ├── workers/
│   │   ├── scheduler_worker.py
│   │   └── outbox_worker.py
│   │
│   └── main.py
│
├── tests/
│   ├── unit/
│   ├── state/
│   ├── reliability/
│   └── integration/
│
├── config/
│   └── medication.yaml
│
└── README.md
```

---

## 23. Codex 第一阶段实施约束

1. 不修改现有 LiveKit 主对话总体结构。
2. 不引入 Supervisor Agent。
3. Medication Skill 不直接调用 LiveKit SDK/TTS。
4. Chat Agent 不直接访问 Medication DB。
5. Scheduler 和状态流转必须是 deterministic code。
6. Harness/LLM 只负责语义解析。
7. MedicationOccurrence 是调度业务真值。
8. 关键 Plan 变更必须版本化。
9. 所有外部事件必须包含 `event_id`。
10. 所有消费操作必须幂等。
11. 状态更新必须使用条件更新或 version guard。
12. DB 状态更新和待发布事件使用 transactional outbox。
13. interaction 与 occurrence 的绑定由可信运行时建立，不能让 LLM 猜 ID。
14. “晚点提醒”不得修改 `scheduled_at`。
15. 未确认不能自动记作 confirmed_taken。
16. 第一阶段不实现 DDI、FHIR、智能药盒、M7、自进化。
17. 先写测试，再完成关键状态和可靠性逻辑。

---

## 24. MVP 验收清单

### 主流程

- [ ] 能创建 Plan Draft。
- [ ] 未审批计划不会被调度。
- [ ] 审批后自动生成 occurrence。
- [ ] 到点能够通过 deviceSn 路由到正确音箱。
- [ ] 无活跃聊天时仍可主动播报。
- [ ] “吃了”只确认当前 occurrence。
- [ ] “晚半小时”只改变 next_reminder_at。
- [ ] “跳过”正确记录 skipped。
- [ ] 截止时间到达后进入 closed_unconfirmed。
- [ ] 家属/平台能够查询今日状态。

### 可靠性

- [ ] Medication Semantic/Harness 停止后，已存在提醒仍正常触发。
- [ ] Medication Domain 重启后，待执行任务能够恢复。
- [ ] 两个 Scheduler 同时运行不会重复领取同一个任务。
- [ ] 重复 Event 不会重复确认。
- [ ] Outbox 发布失败后能够重试。
- [ ] Consumer 崩溃后 pending message 能够恢复。
- [ ] 计划暂停后旧事件不会继续播报。
- [ ] 计划修改后历史记录仍显示旧版本快照。
- [ ] 确认与超时同时发生时只有一个状态转换成功。

### 架构边界

- [ ] Chat Agent 无 Medication DB 凭据。
- [ ] Medication Skill 无 LiveKit Room 控制权。
- [ ] 两侧只通过 Event/API 数据协同。
- [ ] 所有状态变化均可从 Event Log 审计。

---

## 25. MVP 完成后的下一阶段

当上述 MVP 稳定后，再按照老师 V2.0 逐步增加：

### V1.1
- M6 家属通知
- L3/L4 异常升级
- family_notify / emergency_alert

### V1.2
- Drug Normalization
- M2 DDI
- 权威药学知识库

### V1.3
- FHIR / EHR
- 智能药盒

### V1.4
- ElderProfile
- 个性化提醒

### V1.5
- M7 Reflection
- 长期语义记忆
- 受控自进化

---

## 26. 最终实施原则

老师 V2.0 描述的是完整目标形态；本 V2.1 负责把第一阶段收敛成可开发、可测试、可恢复的工程 MVP。

最终关系为：

```text
LiveKit Chat Agent
= 老人侧统一交互入口

Medication Reminder Skill
= 用药业务主系统

Redis Streams + Event Contract
= 两者协同边界

Scheduler + Occurrence + Database
= 用药任务可靠性核心

LLM / DeepSeek Harness
= 自然语言语义能力

M2 / M6 / M7 / FHIR / Pillbox
= 后续增量能力
```

因此第一阶段的开发重点不是“把老师 82 页设计全部实现”，而是先确保：

> **计划不丢、提醒不丢、回复不绑错、状态不乱、重启可恢复、过程可审计。**
