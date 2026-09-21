# Phase 5 Multi-Evidence Confirmation Implementation Report

## 1. 结论

Phase 5 M5 已在现有 Phase 1–4 服务上增量完成。Evidence、确定性 Assessment、正常确认、迟到核实、M6 人工复核事件和 Web 模拟入口已连通；没有删除或重建现有数据库，也没有创建 git commit/push。

## 2. 主要修改

- `src/medication_reminder/confirmation.py`
  - 独立的 `confirmation-policy-v1`
  - 稳定 policy fingerprint
  - 强/弱 Evidence、冲突、设备错误和异常取药规则
  - 无模型调用、无剂量推断
- `src/medication_reminder/storage.py`
  - `medication_evidence`
  - `medication_confirmation_assessment`
  - occurrence/elder indexes
  - SQLite immutable update/delete triggers
  - 启动时幂等建表，兼容已有 data.db
- `src/medication_reminder/service.py`
  - Evidence binding、clock-skew、window、idempotency
  - Evidence -> Assessment -> occurrence apply 的单事务链路
  - 语音/按钮/人工统一走 M5
  - closed_unconfirmed 的 late voice/manual verification
  - evidence/assessment/confirmation 查询
- `src/medication_reminder/http.py`
  - Evidence 写入、Evidence 列表、Confirmation 查询 API
- `src/medication_reminder/semantic/agent.py`
  - 只允许绑定的 closed_unconfirmed interaction 进入迟到确认
- `web/assets/app.js`、`web/assets/app.css`
  - 三端 M5 模拟证据面板、Assessment 结果和事件标签
- `tests/test_phase5_confirmation.py`
  - 26 个 M5 回归测试
- `docs/phase5_multi_evidence_confirmation.md`
  - 数据契约、策略、状态、API、事件和限制

## 3. 关键安全约束

1. `BOX_OPENED`、重量减少、药盒关闭和重量观察不会单独确认服药。
2. `OVERDOSE_CONFIRMED` 被拒绝，不产生 overdose 事实。
3. Assessment 不调用 LLM。
4. `USER_VOICE` 必须绑定 interaction；occurrence/elder 绑定由服务端复核。
5. Evidence 和 Assessment immutable；重复 event_id 幂等。
6. observed_at 的未来 clock-skew 会拒绝；超窗 Evidence 保存但不参与 Assessment。
7. 正常确认和 late verification 都只能在统一 M5 service path 写入。
8. closed_unconfirmed 会保留；迟到只写 late_verified 字段和 `medication.intake.late_verified`。
9. 冲突/异常取药只发 `manual_review.request`，M6 不直接改服药事实。

## 4. 新 API 示例

```json
POST /api/v1/medication/evidence
{
  "event_id": "simulated-evidence-001",
  "elder_id": "E001",
  "occurrence_id": "occ_...",
  "source_type": "SENSOR",
  "evidence_type": "WEIGHT_DECREASE_OBSERVED",
  "value": {"raw_before": 10, "raw_after": 9.5, "delta_grams": -0.5}
}
```

返回内容包含：

- `evidence`
- `assessment`（超窗 Evidence 时为 null）
- `occurrence`
- `late`

## 5. 测试与验证

Phase 5 新增测试覆盖：

- 语音、按钮、人工和统一 M5 basis
- 药盒/重量弱证据不确认
- raw value 保留
- 语音 + 无重量变化的 conflict/review
- DEVICE_ERROR 对传感器矛盾的解释
- EXCESS_REMOVAL_SUSPECTED 的人工复核
- overdose 输入拒绝
- 跨 occurrence/elder/interaction 绑定拒绝
- window、future clock-skew、idempotency
- 正常 manual、late manual、late voice
- SKIP/DELAY/REPEAT 不误写 Confirmation
- Assessment/apply 故障回滚
- immutable 表
- HTTP routes
- restart 后 history/fingerprint 保留

最终验收命令：

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=. .venv/bin/python -m unittest discover -s text_bridge/tests -v
node --check web/assets/app.js
```

最终执行结果：

| 检查项 | 结果 |
|---|---|
| 主测试套件 | **146 tests, 146 OK** |
| Phase 5 新增测试 | **26 tests, 26 OK** |
| text_bridge | **10 tests, 10 OK** |
| `node --check web/assets/app.js` | **OK** |
| live HTTP smoke | **PASS**：health=200、dashboard=200、app.js=200、Evidence POST=201、Confirmation GET=200 |

HTTP smoke 使用隔离的临时数据库和本地端口，结束后已清理。

## 6. 后续建议

