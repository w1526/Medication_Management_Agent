# Phase 1：真实语音用药提醒闭环

## 1. 架构边界

```text
Medication Scheduler
  -> medication.reminder_due
  -> device.interaction.request / SQLite Outbox
  -> DeviceAdapter
       local：本地 Web/MVP，成功后标记 dispatched
       http：POST Chat Agent，HTTP 2xx 才标记 dispatched
  -> Chat Agent / LiveKit / TTS
  -> POST /api/v1/medication/device-events
       started -> completed / failed / interrupted / expired
  -> ASR 文本 + 可信 interaction_id
  -> POST /api/v1/medication/agent/message
  -> Fast Path 或 HarnessSemanticAdapter
  -> MedicationService
  -> confirmed_taken / delayed / skipped
```

`dispatched`、`started`、`completed` 只表示提醒投递/播报生命周期，不表示老人已经服药。只有 `CONFIRM_TAKEN` 才能更新 occurrence 为 `confirmed_taken`。

## 2. Medication → Chat Agent 请求

HTTP Adapter 默认向 `CHAT_AGENT_BASE_URL + CHAT_AGENT_REMINDER_PATH` 发送 JSON：

```json
{
  "event_id": "evt_...",
  "event_type": "device.interaction.request",
  "trace_id": "trace_...",
  "elder_id": "E001",
  "interaction_id": "interaction_...",
  "occurrence_id": "occ_...",
  "plan_id": "plan_...",
  "medication": {
    "name": "二甲双胍",
    "dose": "1片",
    "instruction": "餐后服用"
  },
  "scheduled_at": "2026-09-20T00:00:00+00:00",
  "expires_at": "2026-09-20T02:00:00+00:00",
  "reminder_text": "E001，该吃二甲双胍了，请服用1片，餐后服用。"
}
```

HTTP 2xx 只表示 Chat Agent 接受了任务。网络错误、timeout 和 5xx 保留 Outbox `pending` 以便重试；4xx 或非法成功响应标记 Outbox `failed`，并把 reminder attempt 记为 `failed`。

## 3. Chat Agent → Medication 生命周期回调

继续调用既有接口：

```text
POST /api/v1/medication/device-events
```

请求至少携带：

```json
{
  "event_id": "chat-agent-event-001",
  "interaction_id": "interaction_...",
  "occurrence_id": "occ_...",
  "elder_id": "E001",
  "attempt_id": "attempt_...",
  "event_type": "STARTED",
  "timestamp": "2026-09-20T00:00:01+00:00",
  "trace_id": "trace_...",
  "reason": null
}
```

支持 `STARTED`、`COMPLETED`、`FAILED`、`INTERRUPTED`、`EXPIRED`，也兼容小写状态名。服务端校验 interaction、elder、occurrence、attempt、过期时间和状态转换；相同 `event_id` 重复提交幂等。

允许的投递状态为：

```text
queued/dispatched -> started -> completed
queued/dispatched/started -> failed
started -> interrupted
queued/dispatched -> expired
```

回调不会更新 occurrence 的服药事实。

## 4. ASR 回复入口与信任模型

Chat Agent 将 ASR 文本提交到既有入口：

```text
POST /api/v1/medication/agent/message
```

```json
{
  "elder_id": "E001",
  "interaction_id": "interaction_...",
  "text": "晓得了，我已经吃了",
  "source": "chat_agent",
  "event_id": "asr-event-001",
  "trace_id": "trace_..."
}
```

`interaction_id` 必须来自 Chat Agent 当前运行时上下文；Medication Service 再通过 interaction 绑定 occurrence。自然语言模型不会猜 occurrence_id。现有 `GET /api/v1/medication/notifications?elder_id=E001` 可用于建立/恢复当前 active interaction 上下文。

确定性 Fast Path 优先识别确认、延迟、跳过和重复表达；复杂表达才调用 Harness。`REPEAT` 只返回当前提醒文案并写审计，不改变 occurrence。

## 5. 配置

默认离线配置：

```dotenv
MEDICATION_DEVICE_ADAPTER=local
```

真实 Chat Agent：

```dotenv
MEDICATION_DEVICE_ADAPTER=http
CHAT_AGENT_BASE_URL=http://127.0.0.1:19090
CHAT_AGENT_REMINDER_PATH=/api/v1/chat-agent/reminders
CHAT_AGENT_TIMEOUT_SECONDS=5
CHAT_AGENT_API_TOKEN=
```

Token 只从环境变量读取，不写入代码或示例值。

## 6. 本地 Fake Chat Agent 测试

运行 Phase 1 集成测试：

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_phase1_voice_loop -v
```

测试会启动进程内 HTTP Fake Chat Agent，验证请求契约、HTTP 成功/失败、Outbox 重试、生命周期回执、绑定安全和最终 `confirmed_taken`。

## 7. LiveKit / Chat Agent 另一侧需要实现的接口

真实 Chat Agent 项目需要：

1. 提供可配置的 HTTP `POST` reminder 接收端，接受上述 `device.interaction.request` 契约，并以 2xx 表示已接收。
2. 用 `interaction_id` 建立当前 LiveKit 会话的可信上下文，不从 ASR 文本猜 occurrence。
3. 播报开始、完成、失败、打断或过期时，调用 `/api/v1/medication/device-events`，携带原始 `interaction_id`、`occurrence_id`、`attempt_id`、`elder_id`、`event_id` 和 `trace_id`。
4. TTS 播报完成后等待老人语音；ASR 文本通过 `/api/v1/medication/agent/message` 转发，并继续携带当前 `interaction_id`。
5. 对 Medication Service 的 HTTP timeout/5xx 做自身重试时复用同一个事件 ID；对 4xx 记录人工可见的失败。
6. `COMPLETED` 不得直接解释为已服药，只有 Medication Service 返回 `CONFIRM_TAKEN` 后才能播放确认话术。

