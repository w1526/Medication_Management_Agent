# Phase 1：真实语音用药提醒闭环实现报告

## 1. 阶段结论

本阶段已完成 Medication Service 侧的真实语音提醒桥接能力。

在 Fake Chat Agent 环境下，系统可以自动跑通：

```text
Plan Active
→ MedicationOccurrence
→ Scheduler
→ medication.reminder_due
→ device.interaction.request
→ Chat Agent HTTP Adapter
→ dispatched
→ started
→ completed
→ ASR 文本
→ /api/v1/medication/agent/message
→ Fast Path / Harness
→ confirmed_taken
```

重要边界保持不变：

- `completed` 只表示提醒播报完成，不表示老人已经服药。
- 只有 `CONFIRM_TAKEN` 才能将 occurrence 更新为 `confirmed_taken`。
- occurrence 只能通过可信 `interaction_id` 找到，模型不会猜测 `occurrence_id`。
- Chat Agent 不直接访问 Medication SQLite 数据库。

## 2. 原有缺口

原有系统已经具备计划、审批、occurrence、Scheduler、SQLite Outbox、提醒交互和自然语言 Agent，但提醒投递链路只到：

```text
queued → dispatched
```

缺少真实 Chat Agent/LiveKit 的 HTTP 桥接，以及：

```text
started → completed
→ ASR 回复
→ Medication Agent
→ occurrence 状态更新
```

## 3. 本次实现内容

### 3.1 Device Adapter 抽象

新增 [device_adapter.py](../src/medication_reminder/device_adapter.py)：

- `DeviceAdapter`：统一投递接口。
- `LocalDeviceAdapter`：保留本地 Web MVP、离线开发和单元测试能力。
- `ChatAgentHttpAdapter`：通过 HTTP POST 将提醒请求发送给外部 Chat Agent。
- `DeviceAdapterError`：区分瞬态错误和永久错误。

默认使用 local 模式，不依赖外部 Chat Agent 即可启动系统。

### 3.2 Outbox 投递语义

现有 Outbox 继续作为可靠投递边界：

| 外部结果 | Outbox | reminder_attempt |
| --- | --- | --- |
| HTTP 2xx | `published` | `dispatched` |
| 网络错误/timeout | `pending`，允许重试 | 保持原状态 |
| HTTP 5xx | `pending`，允许重试 | 保持原状态 |
| HTTP 4xx | `failed` | `failed` |
| 2xx 非法响应 | `failed` | `failed` |

设备投递失败不会让 Scheduler 崩溃，也不会修改 occurrence 的服药状态。

### 3.3 Reminder 请求契约

`device.interaction.request` 发送给 Chat Agent 的 JSON 至少包含：

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
  "scheduled_at": "...",
  "expires_at": "...",
  "reminder_text": "E001，该吃二甲双胍了，请服用1片，餐后服用。"
}
```

提醒文案由确定性模板生成，不由模型自由生成药名或剂量。

### 3.4 设备生命周期事件

继续使用既有接口：

```text
POST /api/v1/medication/device-events
```

支持：

- `STARTED`
- `COMPLETED`
- `FAILED`
- `INTERRUPTED`
- `EXPIRED`

服务端会校验：

- `event_id` 幂等性。
- `interaction_id` 是否存在且属于对应老人。
- interaction 是否绑定到对应 occurrence 和 attempt。
- interaction 是否已过期或关闭。
- 当前 reminder delivery 状态是否允许转换。

允许的主要状态转换：

```text
queued/dispatched → started → completed
queued/dispatched/started → failed
started → interrupted
queued/dispatched → expired
```

非法倒退或跳跃，例如 `completed → started`、`failed → completed`，会被拒绝。

### 3.5 Agent 语音回复入口

继续使用既有接口：

```text
POST /api/v1/medication/agent/message
```

Chat Agent 应发送：

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

处理顺序为：

```text
校验 interaction
→ Fast Path
→ HarnessSemanticAdapter（仅复杂文本）
→ 确定性 MedicationService 状态机
```

### 3.6 Fast Path

已覆盖以下常见表达：

| 意图 | 示例 |
| --- | --- |
| `CONFIRM_TAKEN` | 吃了、吃过了、刚吃了、吃好了、我已经吃了 |
| `DELAY` | 晚点、等一下、等会、等十分钟、十分钟后提醒我 |
| `SKIP` | 跳过、不吃了、这次不吃、今天不吃 |
| `REPEAT` | 再说一遍、再提醒一下、刚才说什么、没听清 |

SKIP 否定表达优先于“吃了”匹配，避免将“这次不吃了”误判为确认服药。

`REPEAT` 只返回当前 interaction 的确定性提醒文案，occurrence 状态、服药事实和确认状态均不变化。

### 3.7 当前 Interaction 上下文

复用现有接口：

```text
GET /api/v1/medication/notifications?elder_id=E001
```

该接口返回 active、未过期的 interaction，并包含 occurrence、药品、剂量、提醒时间和提醒文案，可用于 Chat Agent 会话恢复。

## 4. 配置项

配置示例位于 [.env.example](../.env.example)：

```dotenv
MEDICATION_DEVICE_ADAPTER=local

# 使用 HTTP Chat Agent 时：
MEDICATION_DEVICE_ADAPTER=http
CHAT_AGENT_BASE_URL=http://127.0.0.1:19090
CHAT_AGENT_REMINDER_PATH=/api/v1/chat-agent/reminders
CHAT_AGENT_TIMEOUT_SECONDS=5
CHAT_AGENT_API_TOKEN=
```

默认仍为 `local`，因此没有 Chat Agent 时项目也可以运行。

## 5. 事件与审计链路

一次提醒会通过以下 ID 关联：

```text
trace_id
interaction_id
occurrence_id
attempt_id
event_id
```

典型审计事件包括：

```text
medication.reminder_due
device.interaction.request
device.reminder.started
device.reminder.completed
medication.user_response
medication.intake.updated
```

## 6. 测试与验证

### 6.1 自动化测试

执行命令：

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=. .venv/bin/python -m unittest discover -s text_bridge/tests -v
```

结果：

- 根目录测试：28/28 通过。
- `text_bridge` 测试：10/10 通过。
- Python 编译检查通过。

新增 [test_phase1_voice_loop.py](../tests/test_phase1_voice_loop.py) 覆盖：

1. Scheduler → HTTP Chat Agent → `dispatched`。
2. `STARTED`、`COMPLETED` 生命周期。
3. `COMPLETED` 不改变 occurrence 服药状态。
4. 带可信 interaction 的 Fast Path 确认。
5. 延迟不改变 `scheduled_at`。
6. 跳过。
7. 重复提醒。
8. 错误 interaction 和错误 elder。
9. 过期 interaction。
10. 重复设备事件幂等。
11. HTTP timeout 重试。
12. HTTP 4xx 永久失败。
13. Fake Chat Agent 完整确认闭环。

### 6.2 HTTP Smoke Test

已启动独立本地服务并验证：

```text
/health/live
/health/ready
创建计划
审批计划
运行 Scheduler
读取 notifications
提交 STARTED
提交 COMPLETED
提交 agent/message ASR 文本
```

最终 occurrence 状态为：

```text
confirmed_taken
```

## 7. 修改文件

新增：

- `src/medication_reminder/config.py`
- `src/medication_reminder/device_adapter.py`
- `tests/test_phase1_voice_loop.py`
- `docs/phase1_voice_loop.md`
- `docs/phase1_voice_loop_implementation_report.md`

修改：

- `src/medication_reminder/service.py`
- `src/medication_reminder/http.py`
- `src/medication_reminder/semantic/agent.py`
- `.env.example`
- `README.md`

本阶段没有重写 Plan 模型、Scheduler、SQLite、Outbox 或存储 schema，也没有引入 Redis/PostgreSQL 等新基础设施。

## 8. 尚未完成内容

以下内容不属于本阶段实现范围：

- 真实 LiveKit 项目侧代码。
- 真实 TTS、ASR 服务部署和音频流管理。
- Chat Agent 外部 HTTP 接收端的生产实现。
- 设备/会话鉴权体系的完整建设。
- Redis Streams、PostgreSQL、FHIR、DDI 和家属升级能力。

## 9. Chat Agent / LiveKit 对接要求

另一侧需要实现：

1. 接收 `device.interaction.request`，HTTP 2xx 仅表示任务已接受。
2. 保存并传播 `interaction_id`、`occurrence_id`、`attempt_id`、`elder_id` 和 `trace_id`。
3. TTS 开始、完成、失败、打断或过期时调用 `/api/v1/medication/device-events`。
4. ASR 文本调用 `/api/v1/medication/agent/message`，不得从文本猜测 occurrence。
5. 对网络错误和 5xx 使用同一 `event_id` 重试，避免重复生命周期事实。
6. 只有收到 Medication Service 的 `CONFIRM_TAKEN` 结果后，才能播报“已记录服药”等确认话术。

## 10. Git 状态

当前项目目录没有 `.git` 元数据，因此无法生成标准 `git diff` 或 `git status`。本阶段未执行 `git commit` 或 `git push`。

