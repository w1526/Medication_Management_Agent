# Phase 5：Multi-Evidence Confirmation

## 1. 目标与边界

Phase 5 把“是否服药”拆成三层：

1. **Evidence**：老人、按钮、人工、设备或传感器提交的原始事实。
2. **M5 Assessment**：无模型调用的确定性策略，对同一 occurrence 的有效 Evidence 做融合。
3. **Occurrence 状态**：只有 M5 Assessment 可以把正常窗口内的 occurrence 写成 `confirmed_taken`；迟到核实只补充 `late_verified_*` 字段，不改写 `closed_unconfirmed`。

设备打开药盒、重量减少、药盒关闭和重量观察都只是 Evidence。它们本身不会被解释成“已服药”，也不会推导剂量、过量或 overdose。

M6 只消费 `manual_review.request` 等事件做升级/人工复核，不负责确认服药事实。

## 2. Evidence Contract

Evidence 的最小绑定字段如下：

```json
{
  "event_id": "device-evidence-001",
  "elder_id": "E001",
  "occurrence_id": "occ_...",
  "interaction_id": "interaction_...",
  "source_type": "SENSOR",
  "evidence_type": "WEIGHT_DECREASE_OBSERVED",
  "value": {
    "raw_before": 10.0,
    "raw_after": 9.5,
    "delta_grams": -0.5,
    "unit": "g"
  },
  "observed_at": "2026-09-21T08:00:00+00:00",
  "trace_id": "trace-001"
}
```

支持的 source：

`USER_VOICE`、`USER_BUTTON`、`MANUAL_OPERATOR`、`DEVICE`、`SENSOR`、`CAREGIVER`、`FAMILY`。

支持的 evidence type 包括：

- 强确认：`SELF_REPORTED_TAKEN`、`BUTTON_CONFIRMED`、`MANUAL_REPORTED_TAKEN`。
- 弱观察：`BOX_OPENED`、`BOX_CLOSED`、`WEIGHT_OBSERVATION`、`WEIGHT_DECREASE_OBSERVED`、`NO_WEIGHT_CHANGE`。
- 风险/质量：`EXCESS_REMOVAL_SUSPECTED`、`DEVICE_ERROR`。
- 不支持：`OVERDOSE_CONFIRMED`。该字符串会被明确拒绝，不会落库。

`USER_VOICE` 必须带可信的 `interaction_id`。服务端重新校验 interaction、occurrence 和 elder 的一一绑定；客户端不能通过替换 ID 把 Evidence 写到另一剂药上。

## 3. M5 Deterministic Policy

当前策略版本为 `confirmation-policy-v1`，服务把版本和 SHA-256 fingerprint 同时写入每条 Assessment。

| 有效 Evidence | Assessment result | 状态效果 |
|---|---|---|
| 只有药盒/重量/设备观察 | `UNCONFIRMED` | 保持 `unconfirmed` |
| 语音自报已服药 | `CONFIRMED` / `SELF_REPORT` | 正常窗口进入 `confirmed_taken` |
| 按钮确认 | `CONFIRMED` / `BUTTON_SELF_REPORT` | 正常窗口进入 `confirmed_taken` |
| 人工报告 | `CONFIRMED` / `MANUAL_REPORT` | 正常窗口进入 `confirmed_taken` |
| 多个强确认来源 | `CONFIRMED` / `MULTI_EVIDENCE` | 由最晚强 Evidence 作为实际确认依据 |
| 强确认 + `NO_WEIGHT_CHANGE` | `CONFIRMED` + conflict/review | 保留已确认状态，同时发出冲突和人工复核事件 |
| `EXCESS_REMOVAL_SUSPECTED` | `REVIEW_REQUIRED` | 不确认服药；发出人工复核请求 |
| `DEVICE_ERROR` + 无重量变化 | 不把设备错误当矛盾 | 设备错误解释传感器不可靠 |

Assessment 只读取：

- `invalid=0`
- `out_of_window=0`
- 当前 occurrence 的 Evidence

超出窗口的 Evidence 仍可保存，带 `out_of_window=true)，但不会参与 Assessment。未来时间超过接收时刻允许的 clock-skew 会拒绝。

## 4. 状态与迟到核实

正常流程：

```text
unconfirmed
  └─ M5 CONFIRMED ──> confirmed_taken
```

超时流程保留原事实：

```text
unconfirmed
  └─ scheduler deadline ──> closed_unconfirmed
                              └─ late M5 ──> closed_unconfirmed
                                             + late_verified_taken_*
```

迟到语音必须仍然引用原 interaction；迟到人工核实必须提供 `actor_id`、`actor_role`。迟到核实会写：

- `late_verified_taken_at`
- `late_verified_by`
- `late_verified_source`
- `late_verified_note`

不会把超时 occurrence 改成 `confirmed_taken`。已经是 `confirmed_taken` 的 occurrence 后续收到传感器冲突时也不会被降级。

## 5. 持久化、审计与事务

新增：

- `medication_evidence)：原始 Evidence、绑定、观察/接收时间、raw value、窗口标记、信任字段。
- `medication_confirmation_assessment)：结果、basis、policy version/fingerprint、Evidence IDs、冲突/复核标志、late 标志。

两张表均有 SQLite immutable update/delete triggers。Evidence 写入、Assessment、Occurrence 状态写入、Event Log 和 Outbox 在同一事务中完成；任一 Event/Outbox/状态写入失败都会 rollback，避免只留下半条证据链。

同一 `event_id` 重复提交返回原 Evidence/Assessment，不产生第二条记录；跨 occurrence、跨 elder 或跨 event 类型复用会返回冲突。

## 6. API

新增接口：

```text
POST /api/v1/medication/evidence
POST /api/v1/medication/occurrences/{occurrence_id}/evidence
GET  /api/v1/medication/occurrences/{occurrence_id}/evidence
GET  /api/v1/medication/occurrences/{occurrence_id}/confirmation
```

既有：

```text
POST /api/v1/medication/responses
POST /api/v1/medication/occurrences/{occurrence_id}/confirm
```

语音、按钮和人工确认现在都先进入统一 M5 Evidence/Assessment 路径；没有第二条“直接把 occurrence 改成 confirmed_taken”的业务路径。

## 7. 事件

主要事件：

```text
medication.evidence.recorded
medication.confirmation.assessed
medication.confirmation.confirmed
medication.confirmation.conflict_detected
medication.confirmation.review_required
medication.intake.updated
medication.intake.late_verified
manual_review.request
```

每条 Assessment 事件包含 assessment ID、Evidence IDs、policy version/fingerprint、basis、冲突/复核和 trace ID。M6 只将 `manual_review.request` 纳入后续升级链路。

## 8. Web 模拟入口

老人端、家属端、医生端都显示 **M5 · MULTI-EVIDENCE / 服药证据** 区域：

- 模拟药盒开启
- 模拟重量减少
- 模拟重量无变化
- 模拟异常取药

界面明确标记“模拟证据”，并显示 occurrence、Assessment result、basis、冲突/复核标志、policy version、raw value 以及“参与/不参与 Assessment”。模拟按钮调用真实 API，因此也会经过同一绑定、窗口、事务和审计规则。

## 9. 当前限制

