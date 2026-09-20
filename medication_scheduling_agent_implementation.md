# 养老智能体 OS：用药调度智能体轻量化架构与实施方案

> 面向 LiveKit 音箱对话主链路 + DeepSeek Harness 领域智能体  
> 日期：2026-09-05

## 1. 方案结论

本方案基于现有 LiveKit 老人陪伴机器人进行扩展。核心原则是：**不把 Chat Agent 放在 Supervisor Agent 之后，也不让 Medication Agent 接管对话。**

系统采用“**对话主平面 + 独立领域智能体 + 数据事件协同**”模式：

- **Chat Agent**：继续作为音箱上的唯一自然语言交互入口，保留现有 VAD、ASR、四川话对话、TTS、数字人、打断和记忆能力。
- **Medication Scheduling Agent**：作为独立后台领域智能体，负责用药计划、调度、服药状态、异常升级与依从性数据；不持有 LiveKit 会话，不直接 TTS，不共享 Chat Agent Prompt。
- **两者仅通过结构化数据/Event 交互**，不进行 Agent handoff，不共享完整 Conversation History。
- **定时提醒必须由确定性 Scheduler 触发，不依赖 LLM。** DeepSeek/Harness 故障时，已经创建的用药任务仍应能够正常触发。

老师原方案中“计划管理、相互作用检查、提醒调度、多模态提醒、服药确认、异常升级、依从性分析”等 7 项能力继续保留，但第一阶段不拆成 7 个 Sub-Agent，而是作为一个 Medication Agent 下的服务/工具模块实现。

---

## 2. 总体架构

```text
老人
  │ 语音
  ▼
┌─────────────────────────────────────────────┐
│       音箱 / LiveKit 对话交互层              │
│ VAD → ASR → Chat Agent → TTS → 数字人/音箱  │
│              （始终是唯一对话入口）           │
└──────────────────┬──────────────────────────┘
                   │ 结构化数据 / Event
                   ▼
┌─────────────────────────────────────────────┐
│ Agent 数据协同层：Redis Streams / Event Bus  │
│ medication.command / reminder_due / result  │
└──────────────────┬──────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────┐
│ Medication Scheduling Agent                 │
│ （独立 Harness 领域智能体；不接管对话）       │
│                                             │
│ Harness 语义层 + Plan Service               │
│ Scheduler + Occurrence State Machine        │
│ Safety Rules + Repository                   │
└──────────────────┬──────────────────────────┘
                   │
                   ▼
          Medication DB / 审计记录
```

### 2.1 关键设计原则

1. **对话链路不改造**：老人所有语音仍先进入现有 LiveKit Chat Agent。
2. **领域智能体隔离**：Medication Agent 不属于 Chat Agent，也不位于 Supervisor 之下。
3. **只传数据，不转会话**：跨 Agent 协作通过结构化事件进行。
4. **业务状态确定化**：用药计划、到点触发、服药状态、超时与升级均由确定性代码/状态机控制。
5. **LLM 只做擅长的部分**：自然语言理解、歧义澄清、文本解释、低风险策略分析；不承担定时可靠性和药学硬判断。
6. **轻量优先**：第一版不引入 Kafka、RabbitMQ、Temporal、Celery 等重型基础设施。

---

## 3. 各组件职责

### 3.1 LiveKit Chat Agent

职责：

- 接收老人自然语音；
- VAD / ASR / Dialogue Policy / TTS / 数字人；
- 处理四川方言、打断、Turn-taking；
- 将涉及用药的自然语言转为结构化 `medication.command`；
- 接收 `medication.reminder_due` 等事件并以“小伴”的身份对老人播报；
- 将老人对提醒的回复转为结构化 `medication.user_response`。

**不允许：**

- 直接修改 Medication DB；
- 自己计算漏服状态；
- 自己决定升级级别；
- 自己生成药物相互作用结论。

### 3.2 Medication Scheduling Agent

职责：

- 用药计划草稿/确认/更新；
- 将计划展开为具体 `MedicationOccurrence`；
- 维护每次服药任务状态；
- 接收老人结构化响应并更新状态；
- 产生 `reminder_due`、`missed_dose`、`adherence_updated` 等领域事件；
- 后续接入药物规范化、DDI、EHR/FHIR、智能药盒。

**不允许：**

- 直接进入 LiveKit Room；
- 直接调用 TTS；
- 拥有 Chat Agent 的对话历史；
- 自行改药、加药、推荐药或调整剂量。

### 3.3 Agent Data Bus

MVP 建议直接使用 **Redis Streams**，因为现有项目已有 Redis，部署和维护成本最低。

建议至少定义：

```text
agent:medication:commands   # Chat/OS -> Medication
agent:medication:events     # Medication -> OS/平台
agent:device:events         # Medication/OS -> 音箱交互层
```

### 3.4 Device Event Adapter

一个很薄的非 LLM 组件，用于：

```text
elder_id / person_id -> deviceSn -> 当前设备连接 / LiveKit 会话
```

收到 `medication.reminder_due` 后，把事件投递给对应音箱的交互层，而不是让 Medication Agent 自己播报。

---

## 4. 数据所有权与隔离

| 数据 | 所有者/主存 | 说明 |
|---|---|---|
| Conversation History | Chat Agent / Memory Service | Medication Agent 不读取完整历史 |
| LiveKit Session | Chat Runtime | 仅交互层维护 |
| deviceSn-person 映射 | OS Identity/Device Service | 领域 Agent 只使用 elder_id/person_id |
| MedicationPlan | Medication Service | 正式用药计划 |
| MedicationOccurrence | Medication Service | 每一次具体服药任务 |
| Intake Record | Medication Service | taken/delayed/skipped/missed |
| Adherence | Medication Service | 基于 occurrence 计算 |
| Reminder Preference | Medication Service/Shared Profile | 只影响提醒体验，不改变医嘱 |

原则：**Chat Agent 不写 Medication DB；Medication Agent 不写 Chat Memory。**

---

## 5. Medication Agent 内部结构

```text
Medication Scheduling Agent
│
├── Harness Semantic Layer
│   ├── 解析自然语言用药意图
│   ├── 生成 Plan Draft
│   └── 处理歧义/澄清结果
│
├── Plan Service
│   ├── create_draft
│   ├── approve_plan
│   ├── update_plan
│   └── pause/complete
│
├── Scheduler
│   ├── 计算 next_due_at
│   ├── 触发 due occurrence
│   └── 延迟重调度
│
├── Occurrence State Machine
│   └── pending/reminding/delayed/taken/skipped/missed
│
├── Safety & Escalation Rules
│   └── 确定性阈值 + HITL
│
└── Repository
    └── SQLite(开发)/PostgreSQL(部署)
```

### 5.1 Harness 的边界

DeepSeek Harness 适合用于：

- 从自然语言提取药名、剂量、时间、餐次等候选字段；
- 处理“等一哈”“晚半个小时”“我刚刚吃了”等语义；
- 输出结构化 tool call；
- 后期做依从性分析、提醒体验反思。

Harness **不负责**：

- 到点计时；
- DB 事务；
- occurrence 状态真值；
- 漏服阈值；
- DDI 权威结论；
- 高风险自动医疗决策。

---

## 6. 核心数据模型

### 6.1 medication_plan

```text
id
elder_id
drug_name
drug_code          # 第二阶段可选
dosage
route
schedule_rule
relation_to_meal
start_date
end_date
status              # draft/pending_confirmation/active/paused/completed
risk_policy          # normal/important/critical，必须来自规则/医护配置
source
created_by
approved_by
created_at
updated_at
```

### 6.2 medication_occurrence

`MedicationOccurrence` 是正式调度的核心业务事实，不应只依赖 Scheduler Job。

```text
id
plan_id
elder_id
scheduled_at
status               # pending/reminding/delayed/taken/skipped/missed
actual_time
confirmation_method  # voice/button/family_proxy/pillbox_signal
confidence            # confirmed/likely/observed_only（可选）
reminder_count
escalation_level
last_reminded_at
next_action_at
created_at
updated_at
```

注意：**药盒打开不等于已经服药。** `pillbox_open` 最多作为观察信号，不能自动等价为 `confirmed_taken`。

### 6.3 medication_event_log（建议）

医疗场景需要全链路审计：

```text
id
elder_id
plan_id
occurrence_id
event_type
source
payload_json
created_at
```

---

## 7. Occurrence 状态机

```text
pending
   │ 到点
   ▼
reminding ───────────────┐
   │                     │ 老人说“晚点”
   │                     ▼
   │                  delayed
   │                     │ 到新时间
   │                     └──────→ reminding
   │
   ├── 老人确认 ──→ taken
   ├── 主动跳过 ──→ skipped
   └── 超时 ─────→ missed / escalate
```

状态变化必须由 Medication Service 统一执行，并保证幂等。

---

## 8. 事件协议建议

### 8.1 Chat -> Medication：创建用药计划请求

```json
{
  "event_type": "medication.plan.create_request",
  "event_id": "uuid",
  "elder_id": "E001",
  "source": "chat_agent",
  "text": "每天早上八点提醒我吃降压药",
  "occurred_at": "2026-09-06T07:30:00+08:00"
}
```

Medication Agent 解析后只创建 `draft/pending_confirmation`，老人语音不能直接新增正式处方计划。

### 8.2 Medication -> Device：到点提醒

```json
{
  "event_type": "medication.reminder_due",
  "event_id": "uuid",
  "elder_id": "E001",
  "occurrence_id": "O001",
  "plan_id": "P001",
  "display": {
    "drug_name": "氨氯地平",
    "dosage": "5mg",
    "relation_to_meal": "餐后"
  }
}
```

Device Event Adapter 将其映射到 deviceSn，并交给现有 LiveKit 交互层播报。

### 8.3 Chat -> Medication：老人回复

```json
{
  "event_type": "medication.user_response",
  "event_id": "uuid",
  "elder_id": "E001",
  "occurrence_id": "O001",
  "text": "等半个小时再喊我",
  "interaction_id": "I001"
}
```

Harness 可解析为：

```json
{
  "action": "delay",
  "delay_minutes": 30
}
```

但最后的 DB 更新和重调度由 Medication Service 完成。

### 8.4 Medication -> OS：结果/异常

```json
{
  "event_type": "medication.intake.updated",
  "elder_id": "E001",
  "occurrence_id": "O001",
  "status": "taken",
  "actual_time": "2026-09-06T20:08:21+08:00"
}
```

或：

```json
{
  "event_type": "medication.missed_dose",
  "elder_id": "E001",
  "occurrence_id": "O001",
  "escalation_level": 1
}
```

---

## 9. 端到端关键流程

### 9.1 创建计划

```text
老人语音
 -> LiveKit ASR
 -> Chat Agent
 -> medication.plan.create_request
 -> Medication Agent/Harness 结构化解析
 -> MedicationPlanDraft
 -> HITL/家属或医护确认
 -> active
 -> 生成/计算 occurrence
```

### 9.2 到点提醒

```text
Scheduler 发现 due occurrence
 -> occurrence: pending -> reminding
 -> medication.reminder_due
 -> Device Event Adapter
 -> elder_id -> deviceSn
 -> LiveKit Chat Runtime
 -> 模板/TTS 播报
```

第一次提醒建议优先使用确定性模板，不必调用 LLM：

```text
{称呼}，到吃药时间了哈。{药名}，{剂量}，{餐前/餐后}。吃了跟我说一声。
```

### 9.3 老人确认

```text
老人：“吃了”
 -> Chat Agent
 -> medication.user_response
 -> Medication Agent
 -> occurrence -> taken
 -> medication.intake.updated
```

### 9.4 延迟

```text
老人：“等半个小时”
 -> Harness 解析 delay=30
 -> occurrence -> delayed
 -> next_action_at = now + 30min
 -> 到时再次 reminder_due
```

### 9.5 无响应/漏服

```text
reminding
 -> grace period 到期
 -> 根据 plan.risk_policy + 规则判断
 -> 再提醒 / 家属通知 / 护士站事件
```

不要将“一次未响应”统一等价为紧急医疗事件；升级策略应可配置且由确定性规则控制。

---

## 10. 轻量化技术选型

### MVP 推荐组合

```text
DeepSeek Harness       # Medication Agent 的语义/工具层
Redis Streams          # Agent 间事件总线
SQLite / PostgreSQL    # 用药业务真值与审计
轻量 Scheduler         # 数据库轮询或独立定时器
现有 LiveKit Runtime   # 老人交互和主动播报
```

### Scheduler 建议

优先级从轻到重：

1. **数据库 due 扫描器（首选 MVP）**：每 10~30 秒查询 `next_action_at <= now()`，简单、易审计、易恢复。
2. **Harness 社区 cron 插件作为代码参考**：参考任务持久化、ticker、cron 管理，但不要把“到点启动新 Agent 执行 Prompt”原样用于服药提醒。
3. 规模扩大后再考虑 APScheduler/队列系统；当前不建议引入 Temporal/Celery/Kafka。

### 可参考开源项目

- DeepSeek Harness 官方：`https://github.com/deepseek-ai/deepseek-harness`
- 官方 session-local Schedule：`packages/schedule/schedule`。可参考工具和持久化思路，但官方 Schedule 是 **Session-local**，不适合作为用药提醒业务真值。
- `dsh-plugin-scheduled-items`：`https://github.com/weibaohui/dsh-plugin-scheduled-items`，可参考 cron/ticker/UI/插件接入方式。
- `dsh-schedule`（社区）：`https://github.com/csiroqa/dsh-schedule`，可参考持久化 schedule 与定时检查实现。

注意：DeepSeek Harness 当前仍处于 Developer Preview，存在兼容性破坏风险，因此 Medication Domain Logic 必须放在独立服务/插件边界内，并固定版本或 commit。

---

## 11. 建议目录结构

```text
medication-agent/
├── README.md
├── src/
│   ├── agent/
│   │   ├── prompt.md
│   │   └── tools.ts
│   ├── domain/
│   │   ├── plan.ts
│   │   ├── occurrence.ts
│   │   └── events.ts
│   ├── services/
│   │   ├── plan_service.ts
│   │   ├── intake_service.ts
│   │   ├── scheduler_service.ts
│   │   └── escalation_service.ts
│   ├── infra/
│   │   ├── repository.ts
│   │   ├── redis_bus.ts
│   │   └── migrations/
│   └── index.ts
└── tests/
    ├── plan.test.ts
    ├── scheduler.test.ts
    └── state_machine.test.ts
```

如果最终将业务服务写在 Python，也可以保持同样的逻辑分层；Harness 只通过 API/Event 调用，不要求业务代码必须 TypeScript。

---

## 12. 第一阶段只实现的工具

对 Harness 暴露：

```text
medication_plan_create_draft
medication_plan_update_draft
medication_plan_list
medication_intake_confirm
medication_intake_delay
medication_intake_skip
```

内部工具（不暴露给 LLM）：

```text
dispatch_due_medications
mark_occurrence_missed
apply_escalation_policy
publish_device_event
```

第一版暂缓：DDI、FHIR、智能药盒、自进化、多 Sub-Agent。

---

## 13. MVP 开发阶段

### Phase 0：主动提醒链路验证

目标：证明“无当前活跃对话时，后台事件仍能找到 deviceSn 并让音箱主动播报”。

验收：创建 5 分钟后任务 -> 当前对话结束/空闲 -> 到点音箱成功播报。

### Phase 1：完整用药闭环

```text
创建计划
 -> 到点提醒
 -> 吃了/晚点/跳过
 -> occurrence 更新
 -> 记录可查询
```

### Phase 2：HITL + 异常升级 + 审计

增加：

- Plan Draft 家属/医护确认；
- 未响应策略；
- 家属事件；
- 幂等/重试/离线补偿；
- 审计日志。

### Phase 3：医疗数据能力

增加：

- Drug Normalization；
- 权威 DDI 数据源；
- EHR/FHIR。

### Phase 4：IoT 与适应性

增加：智能药盒信号、库存、Reminder Preference、低风险提醒策略适应。

---

## 14. Codex 实施约束（必须遵守）

1. **不要修改现有 LiveKit 主对话链路的总体结构。**
2. **不要引入 Supervisor Agent 作为 Chat Agent 的前置入口。**
3. **Medication Agent 与 Chat Agent 只能交换结构化事件/API 数据。**
4. **不要让 Medication Agent 直接依赖 LiveKit SDK/TTS。**
5. **不要让 Chat Agent 直接写 Medication DB。**
6. **Scheduler/状态机必须是 deterministic code，不允许 LLM 决定是否到点或是否漏服。**
7. **所有写操作必须幂等**，使用 `event_id` / `occurrence_id` 防止重复消费。
8. **MedicationOccurrence 是调度业务真值**，不要只把任务存在内存 Timer/Cron 中。
9. **服务器重启后必须可恢复待执行任务。**
10. **药盒开盒不能直接标记为 confirmed_taken。**
11. **老人语音新增用药只能形成 Draft/Pending Confirmation，不能未经授权成为正式医嘱计划。**
12. **第一阶段不要实现药物相互作用自由推理。**

---

## 15. MVP 验收标准

必须通过以下测试：

- [ ] Chat Agent 正常闲聊不受 Medication Agent 部署影响。
- [ ] Chat Agent 与 Medication Agent 可以分别独立重启。
- [ ] 创建计划后，Medication Agent/Harness 暂时不可用时，已持久化 occurrence 仍可恢复。
- [ ] 到点能通过 elder_id/person_id 正确路由到 deviceSn。
- [ ] 老人说“吃了”后只关闭当前 occurrence，不误关闭其他药物任务。
- [ ] 老人说“等半个小时”会产生新的 `next_action_at`，不创建重复业务任务。
- [ ] 重复 Event 不会造成重复 taken/重复通知。
- [ ] 无响应可进入 missed/escalation，而不是无限循环提醒。
- [ ] 所有状态变化可从 event log 审计。
- [ ] Chat Agent 不需要获取 Medication DB 凭据。

---

## 16. 最终架构定位

本项目不采用“Supervisor -> Chat Agent -> Medication Agent”的串行多智能体范式，而采用：

> **LiveKit Chat Agent = 老人侧统一对话接口**  
> **Medication Scheduling Agent = 独立后台领域智能体**  
> **Event Bus + Data Contract = 两者唯一协作边界**  
> **Scheduler + State Machine = 用药业务可靠性核心**

这样既保留现有音箱对话体验和低延迟链路，也为后续 Sleep Agent、Fall Agent、Health Agent 等领域智能体提供统一的解耦扩展方式。
