# V1 文本桥接服务

这是独立运行的 LiveKit ↔ 用药管理文本桥接进程。跨进程边界只传 UTF-8
JSON 文本；桥接服务不读取用药数据库，也不负责普通聊天分类。

## 启动

```bash
cd /root/autodl-tmp/Medication_Management_Agent
PYTHONPATH=. python -m text_bridge.bridge
```

默认只监听 `127.0.0.1:18765`，用药服务默认是
`http://127.0.0.1:18080`。生产或跨主机使用前必须配置受保护的网络通道和
`TEXT_BRIDGE_TOKEN`。

必要的试点配置示例：

```dotenv
TEXT_BRIDGE_ENABLED=1
TEXT_BRIDGE_TOKEN=change-me
TEXT_BRIDGE_TEST_MAPPING={"tenant_id":"test_tenant","elder_id":"E001","device_sn":"test_device_001","room_name":"test_room_001"}
TEXT_BRIDGE_JOURNAL_PATH=text_bridge/bridge.sqlite3
```

LiveKit 接头通过请求头传递 token，不会把 token 放进 JSON、URL 或日志。
桥接服务每 2 秒查询一次已有的 `GET /notifications`，只有注册且身份映射
通过的会话才会收到提醒。

## 运行测试

```bash
PYTHONPATH=. python -m unittest discover -s text_bridge/tests -v
```

本服务不能证明老人实际听见或实际吞服；`published`、`dispatched` 也不等于
真实播出。只有 LiveKit 接头回写的 `started/completed/interrupted/failed`
才表示观察到的播放链路状态。
