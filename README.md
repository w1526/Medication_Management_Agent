# Medication Reminder MVP

这是 `medication_reminder_mvp_implementation_v2.1.md` 的最小可运行闭环实现。

当前闭环：

```text
创建草稿 → 提交/审批 → 生成未来 7 天 occurrence
→ scheduler 到点扫描 → reminder_due/outbox
→ device.interaction.request（本地设备适配器）
→ responses: 吃了 / 晚点 / 跳过
→ occurrence 状态、提醒投递状态、审计日志、今日查询
```

核心服务使用 Python 标准库，启动命令和 DeepSeek Harness Agent 统一使用 Python 3.10+。在没有 Redis、PostgreSQL 或 LiveKit SDK 的环境中也可以运行本地 MVP；SQLite 是开发存储，`domain_outbox` 和事件字段是后续替换 Redis Streams/PostgreSQL 的边界。

## Python 3.10+ 与 Harness SDK

DeepSeek Harness 官方 Python SDK 要求 Python 3.10+。建议使用本机可用的 Python 3.10 或更高版本，在当前项目目录内创建独立虚拟环境：

```bash
# 如果命令存在，直接使用；否则替换为本机实际的 Python 3.10+ 路径
python3.10 -m venv .venv
.venv/bin/python -m pip install -r requirements-harness.txt
```

本容器已经准备好项目内 `.venv`（Python 3.10.20）；如果 `.venv` 已存在，直接使用下面的启动命令即可，不需要改动系统 Python。

项目支持读取根目录下的本地 `.env` 文件。完整启动流程如下：

```bash
cd /root/autodl-tmp/Medication_Management_Agent
[ -f .env ] || cp .env.example .env
chmod 600 .env
```

然后用编辑器打开 `.env`，只填写这一行：

```dotenv
DEEPSEEK_API_KEY=在这里填写你的新 Key
```

`.env` 只会在当前服务进程启动时读取，不会修改当前 shell，也不会影响其它项目；已有的系统环境变量优先级更高。`.env` 已加入忽略规则，不应提交到代码仓库。如果未配置 Key，确定性调度和 Web 测试台仍可使用；自然语言 Agent 会返回明确的配置错误。

## 启动

```bash
cd /root/autodl-tmp/Medication_Management_Agent
PYTHONPATH=src .venv/bin/python -m medication_reminder --db data/medication.db --port 18080
```

启动成功后，当前终端会持续运行服务、不会返回 shell 提示符，这是正常现象。修改 `.env` 后需要停止并重新启动服务，配置才会生效。

端口绑定成功后，终端会直接打印 Web 访问地址，例如：

```text
Web UI: http://127.0.0.1:18080/
```

打开浏览器访问：

```text
http://127.0.0.1:18080/
```

## 三方角色工作台

同一个服务现在提供三个相互联通的页面入口，页面读取同一位老人的计划、提醒、服药状态和审计事件：

- 老人端：`http://127.0.0.1:18080/elder`，用于查看今日任务、处理提醒和反馈“吃了/晚点/跳过”。
- 家属端：`http://127.0.0.1:18080/family`，用于创建计划、使用自然语言生成草稿、提交医生审核和协助跟进。
- 医生端：`http://127.0.0.1:18080/doctor`，用于审核/暂停计划、查看今日执行情况和审计记录。

三页会自动同步共享 dashboard；提醒每 2 秒轮询，计划、依从率和事件每 8 秒同步一次。

停止服务请在当前终端按 `Ctrl+C`。如果服务之前已经启动，需要先停止旧进程，再重新执行下面的启动命令，才能加载最新界面。

服务启动后：

```bash
curl http://127.0.0.1:18080/health/live
```

## 可视化测试台

启动服务后，在浏览器打开：

```text
http://127.0.0.1:18080/
```

页面基于开源 Tabler Core Dashboard 风格模板，并针对用药提醒场景做了定制。界面支持创建草稿、审批计划、运行调度、查看今日任务、接收后台提醒、模拟“吃了/晚点/跳过”和查看审计事件。页面每 2 秒轮询当前老人的打开提醒；首次打开页面时，如果提醒仍在交互有效期内，也会显示待处理提醒。首次打开页面需要能够访问 jsDelivr CDN 以加载 Tabler 样式；API 本身不依赖 CDN。

网页端标准验证流程：

1. 在“用自然语言试一下”中输入：`每天晚上10点提醒我吃氨氯地平5mg`，点击“发送”。
2. 查看“已生成计划草稿”卡片，确认药品、剂量、`每日时间 22:00` 和开始日期；未填写开始日期时会标注“已默认从今天开始”。
3. 点击“去审批”，在“计划版本与审批”中点击“审批生效”。计划状态变为“生效中”后，今日任务会生成。
4. 等待计划时间到达。后台 Scheduler 每秒检查一次；也可以点击“运行一轮调度”立即检查当前时间是否已经到点，但它不会把未来时间快进。
5. 到点后页面顶部会出现“现在需要确认用药”提醒卡片；如需浏览器系统通知，点击“开启系统通知”。
6. 点击提醒卡片或今日任务中的“吃了”，然后查看“已确认”数量、任务状态和最近事件，确认闭环完成。

页面和数据库统一使用 `HH:MM` 24 小时制：`晚上10点` 会保存并显示为 `22:00`。修复前已经创建的旧草稿不会自动改写；如果旧草稿显示为 `10:00`，请不要审批它，重新发送自然语言计划或手动创建 `22:00` 的新草稿。

后台 scheduler 每秒运行一次。若要手动推进测试时钟，也可以调用：

```bash
curl -X POST http://127.0.0.1:18080/api/v1/medication/scheduler/run \
  -H 'Content-Type: application/json' \
  -d '{}'
```

如果需要使用其他端口，可通过 `--port` 指定：

```bash
PYTHONPATH=src .venv/bin/python -m medication_reminder --db data/medication.db --port 8080
```

若出现 `OSError: [Errno 98] Address already in use`，说明该端口已被其他进程占用。可以检查占用进程：

```bash
ss -ltnp | grep :8080
```

确认端口未被业务使用后再停止对应进程，或直接换用 `18080` 等空闲端口；不要直接终止不明进程。

## API 最小示例

```bash
# 1. 创建草稿
curl -X POST http://127.0.0.1:18080/api/v1/medication/plans/draft \
  -H 'Content-Type: application/json' \
  -d '{
    "elder_id": "E001",
    "drug_name": "氨氯地平",
    "dosage_text": "5mg",
    "schedule_time": "08:00",
    "start_date": "2026-09-11",
    "relation_to_meal": "餐后",
    "created_by": "family:F001"
  }'

# 2. 将 {plan_id} 提交到待确认（也可以直接调用 approve，服务会完成这一步）
curl -X POST http://127.0.0.1:18080/api/v1/medication/plans/{plan_id}/submit

# 3. 审批；审批人必须显式提供
curl -X POST http://127.0.0.1:18080/api/v1/medication/plans/{plan_id}/approve \
  -H 'Content-Type: application/json' \
  -d '{"approved_by": "doctor:D001"}'

# 4. 查询老人今天的任务
curl 'http://127.0.0.1:18080/api/v1/medication/today?elder_id=E001'

# 5. 查询当前仍在等待确认的提醒（Web 页面也会自动轮询）
curl 'http://127.0.0.1:18080/api/v1/medication/notifications?elder_id=E001'

# 6. 管理台一次读取某位老人的 dashboard 数据
curl 'http://127.0.0.1:18080/api/v1/medication/dashboard?elder_id=E001&limit=30'

# 7. 收到 reminder 后，用 occurrence 的 interaction_id 模拟老人回复
curl -X POST http://127.0.0.1:18080/api/v1/medication/responses \
  -H 'Content-Type: application/json' \
  -d '{
    "elder_id": "E001",
    "interaction_id": "{interaction_id}",
    "action": "CONFIRM_TAKEN",
    "source": "chat_agent"
  }'
```

`POST /api/v1/medication/responses` 也支持只传 `text`，会走本地 fast path：`吃了`、`晚点/半小时后`、`跳过/今天不吃`、`再说一遍`。生产接入时，Chat Agent 应先建立可信 interaction 绑定，再把结构化 `action` 和 `interaction_id` 发给本服务；LLM 不负责猜 occurrence ID。


### 自然语言 Agent API

```bash
curl -X POST http://127.0.0.1:18080/api/v1/medication/agent/message \
  -H 'Content-Type: application/json' \
  -d '{"elder_id":"E001","text":"每天晚上九点提醒我吃二甲双胍500mg","source":"chat_agent"}'
```

计划类文本只会生成 `draft`，不会由 Harness 直接审批或激活；如果自然语言中没有提供开始日期，系统会按 `Asia/Shanghai` 自动默认从当天开始；已有提醒上下文中的“吃了/晚点/跳过”会绑定到可信 interaction 后交给确定性状态机处理。可用以下接口检查 Harness 状态：

```text
GET /api/v1/medication/agent/status
```

如果同一老人同时有多个打开的提醒，回复接口必须额外携带 Chat Agent 已建立的 `interaction_id`；语义模型不能自行猜测任务：

```bash
curl -X POST http://127.0.0.1:18080/api/v1/medication/agent/message \
  -H 'Content-Type: application/json' \
  -d '{"elder_id":"E001","interaction_id":"{interaction_id}","text":"吃了","source":"chat_agent"}'
```

## 关键边界

- `MedicationOccurrence` 是调度真值；`scheduled_at` 不会因延迟而改变。
- `confirmation_deadline_at` 与 `next_reminder_at` 分离，延迟受次数和截止时间限制。
- 计划草稿未经审批不会生成可调度任务；关键修改通过新版本实现。
- 状态更新、审计事件和待发布 outbox 事件在同一 SQLite 事务内提交。
- outbox 发布和响应消费都按 `event_id`/dedup key 幂等；重启后 pending outbox 和 occurrence 会继续处理。
- device event 只表示投递/播报生命周期，不会自动等价为老人已服药。
- 当前默认 `LocalDeviceAdapter` 用于 Web/MVP；设置 `MEDICATION_DEVICE_ADAPTER=http` 后，`ChatAgentHttpAdapter` 会通过 Outbox 将 `device.interaction.request` 投递给 Chat Agent，只有 HTTP 2xx 才标记 `dispatched`。Chat Agent 的播报生命周期继续通过 `/api/v1/medication/device-events` 回传。
- 当前只支持 `Asia/Shanghai`、每日固定 `HH:MM`；餐前/餐后仅展示，不参与动态调度。

## Phase 2：异常升级与人工处置

当 occurrence 的确认截止时间到期仍为 `unconfirmed` 时，M6 在同一 SQLite 事务中把它关闭为 `closed_unconfirmed`，创建唯一 escalation，并经 Outbox 发布：

```text
closed_unconfirmed → CAREGIVER → FAMILY → MANUAL_REVIEW → acknowledge → resolve
```

当前只使用可验证的软件事实，不根据药名猜风险；单次 `SKIP`、提醒投递失败和 `completed` 都不会自动等同于漏服或紧急事件。`ACKNOWLEDGED` 只表示有人接手，会设置新的处理截止时间；超时未 `RESOLVED` 仍会继续升级。现场确认服药请先调用正式人工确认接口：窗口内变为 `confirmed_taken`，`closed_unconfirmed` 之后则记录 late verification 并保留超时事实，再独立关闭 escalation。

常用接口：

```bash
# 查询异常升级
curl 'http://127.0.0.1:18080/api/v1/medication/escalations?elder_id=E001'

# 手动推进持久化升级 deadline
curl -X POST http://127.0.0.1:18080/api/v1/medication/escalations/run \
  -H 'Content-Type: application/json' -d '{}'

# 确认接手
curl -X POST http://127.0.0.1:18080/api/v1/medication/escalations/{escalation_id}/acknowledge \
  -H 'Content-Type: application/json' \
  -d '{"actor_id":"caregiver-001","actor_role":"caregiver","event_id":"ack-001"}'

# 关闭异常；不会改变 occurrence 服药状态
curl -X POST http://127.0.0.1:18080/api/v1/medication/escalations/{escalation_id}/resolve \
  -H 'Content-Type: application/json' \
  -d '{"actor_id":"caregiver-001","actor_role":"caregiver","resolution_code":"NOT_TAKEN","resolution_note":"老人拒绝服药","event_id":"resolve-001"}'
```

M6 timeout、ACK resolution timeout、重复异常阈值和 lookback 配置在 `.env.example` 中；它们是工程测试默认值，不是临床指南。当前通知只由本地 Outbox sink 记录 `simulated` 派发，不发送真实短信、电话或急救通知。详见 [Phase 2 设计文档](docs/phase2_escalation.md)。

## Phase 3：M2 Safety Check

Plan 审批现在会先执行确定性的 M2 安全检查：`PASS`/`WARN` 才能激活，`BLOCK` 或安全引擎故障保持 `pending_confirmation`，不生成 occurrence，也不发送提醒。Scheduler 和 Outbox 发提醒前还会检查当前 Plan Version 的有效 Safety Check、`ruleset_version` 与自动计算的 `ruleset_fingerprint`；规则内容变化即使 version 不变也会 stale。Safety BLOCK 会取消未来未执行 occurrence，但不修改已完成历史。

默认配置是空 Rule Provider：

```dotenv
SAFETY_ENABLED=1
SAFETY_RULE_PROVIDER=empty
SAFETY_RULESET_VERSION=empty-v1
```

因此默认 `PASS` 只表示当前结构检查范围内没有发现问题，不代表临床绝对安全；API 会返回 `coverage_complete=false`，Web 显示“基础安全检查通过 · 临床覆盖不完整”。显式测试规则见 [data/safety_rules.example.json](data/safety_rules.example.json)，只使用 `TEST_DRUG_A/B/C` 虚构药物；项目没有接入真实 DDI、过敏、禁忌或 DrugBank/OpenFDA 数据库。

安全结果接口：

```text
GET  /api/v1/medication/plans/{id}/safety
GET  /api/v1/medication/plans/{id}/safety/history
POST /api/v1/medication/plans/{id}/safety/check
GET  /api/v1/medication/safety/checks/{check_id}
```

详见 [Phase 3 设计](docs/phase3_safety_check.md)、[Phase 3 实现报告](docs/phase3_safety_check_implementation_report.md) 和 [Phase 3.1 修订报告](docs/phase3_1_patch_report.md)。

## 测试

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

