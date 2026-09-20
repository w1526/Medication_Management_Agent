# LiveKit 修改记录

## 记录信息

- 记录日期：2026-09-19
- 对应方案：[LiveKit 与用药管理智能体文本桥接实现方案 V1](./LiveKit与用药管理智能体_文本桥接实现方案_V1.md)
- LiveKit 项目：`/root/autodl-tmp/CODE/agent-starter-python`
- 用药管理项目：`/root/autodl-tmp/Medication_Management_Agent`

## 一、实现边界

本轮按照 V1 方案采用“独立文本桥接服务 + LiveKit 薄接头”：

- Medication 服务继续负责计划、审批、调度、交互有效期和服药状态；LiveKit 不读取 Medication 数据库。
- 两端只通过 UTF-8 JSON 文本通信，不传递音频、模型实例、Python 对象或数据库对象。
- LiveKit 默认关闭文本桥接，只对配置白名单中的老人启用，并要求可信的租户、老人和设备身份。
- 没有 active medication interaction 时，语音回合继续走原 Chat Agent，且不发送 Bridge 请求。
- 有 active medication interaction 时，由 LiveKit 本地先取得回合所有权，再通过 Bridge 处理用药回复。
- SOS/紧急求助和本地音乐控制保持优先级，不被用药 Bridge 阻塞。
- 未将普通聊天分类器、数据库共享、音频传输或文档外的用药能力加入 LiveKit。

## 二、已修改的 LiveKit 原有文件

### `src/agent.py`

- 从可信房间元数据读取 `tenant_id`、`elder_id` 和 `device_sn`。
- 创建 `MedicationOwnershipGate`，把 ownership gate 挂入会话工具上下文。
- 只有 ownership 开启且身份完整时才创建并启动 `TextBridgeSessionAdapter`。
- 将 Bridge 适配器和 ownership gate 传给 `Assistant` 与房间生命周期装配。
- 保留原有会话初始化、普通聊天和非用药能力。

### `src/assistant.py`

- 增加可选的 `TextBridgeSessionAdapter` 和 `MedicationOwnershipGate`。
- 在默认 LLM/tool 响应前检查 active medication interaction。
- Bridge 接管时同步委派用户文本，并用 `StopResponse` 阻断默认响应。
- 支持本地 release 指令；无 active binding 时不发送 Bridge 用户请求。
- 处理 Bridge 异常时使用固定失败提示，不回退到默认 LLM 用药处理。
- 保留纯哭声和上下文紧急表达的 SOS 路径；紧急表达不进入音乐或用药 Bridge 快速路径。
- 会话退出时关闭 Bridge 适配器。

### `src/room_lifecycle.py`

- 在旧的主动提醒入口增加 ownership gate。
- 对由文本 Bridge 接管的用药提醒，阻断旧主动提醒副作用；非用药主动能力保持原行为。

### `src/utils/tool_schema.py`

- 在旧提醒创建/写入入口增加非用药 gate。
- 查询提醒时过滤被 Bridge ownership 接管的用药提醒，并记录可用的非用药提醒 ID。
- 禁止旧 `medication_checkin` 在 Bridge ownership 开启时写入用药状态。
- 旧提醒取消和更新只允许操作已确认的非用药提醒 ID。

## 三、新增的 LiveKit 文本桥接模块

目录：`/root/autodl-tmp/CODE/agent-starter-python/src/text_bridge/`

- `config.py`：Bridge 默认关闭、白名单和连接超时等配置。
- `protocol.py`：V1 JSON 消息结构、类型、绑定版本和字段校验。
- `client.py`：WebSocket 连接、token 请求头、注册、心跳、发送超时和关闭处理。
- `ownership.py`：LiveKit 侧用药 ownership 及旧工具隔离规则。
- `session_adapter.py`：本地 binding、turn 所有权、`StopResponse` 对接、ACK、clear/release、结果关联、旧结果隔离和断线清理。
- `speech_adapter.py`：将 Bridge 的 speak 消息接入 LiveKit 播放，并回传 `started/completed/interrupted/failed`。

## 四、测试与验证

新增测试：

- `tests/test_text_bridge_ownership.py`
- `tests/test_text_bridge_playback.py`
- `tests/test_text_bridge_session.py`
- `tests/test_text_bridge_turn_routing.py`

本轮最终验证结果：

- LiveKit 受影响回归测试：`123 passed, 1 warning`。
- LiveKit Bridge 与 SOS 专项测试：`31 passed`。
- `src/text_bridge` 及四个修改文件通过 Python 编译检查。
- `git diff --check` 通过。

## 五、后续修改要求

后续如果继续修改 LiveKit 代码，必须同步更新本文件，至少记录：修改日期、修改文件、行为变化、ownership/优先级边界以及对应测试结果。不得只在对话中说明而不更新记录。
