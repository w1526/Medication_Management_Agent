# Phase 3：M2 Safety Check 与 Safety Freeze

## 目标

M2 位于 Plan 审批和 Scheduler 之间。它是确定性、可重放、可审计的安全门：

```text
Draft → Submit → M2 Safety Check
                    ├─ PASS → Active → Occurrence → Scheduler
                    ├─ WARN → Active（保存 warning）→ Occurrence → Scheduler
                    └─ BLOCK → 保持 pending_confirmation，不生成 occurrence
```

M2 不调用 LLM/Harness，不诊断、不改剂量、不停药，也不根据药名猜 DDI、禁忌、过敏或最大剂量。

## 结果与 coverage

每次检查保存 `check_id`、`plan_id`、`plan_version`、`status`、`ruleset_version`、`checked_at`、`trace_id`、`coverage` 和不可变 finding。

- `PASS`：在当前启用的规则范围内没有发现问题。
- `WARN`：存在 `WARN` finding，但允许审批继续。
- `BLOCK`：存在 `BLOCK` finding，或安全引擎/已配置 Provider 故障；不能激活。

`PASS` 不表示临床绝对安全。默认空 Provider 的 coverage 类似：

```json
{
  "structural": "checked",
  "dose_rules": "not_configured",
  "ddi": "not_configured",
  "allergy": "not_configured",
  "contraindication": "not_configured"
}
```

## Provider 与 Rule Pack

`SafetyRuleProvider` 是 M2 的替换边界，可由本地 Rule Pack、未来正式药学数据库或测试 Fixture 实现。当前提供：

- `EmptySafetyRuleProvider`：生产默认，不声称已检查临床规则。
- `FixtureDoseRuleProvider`：只读取显式规则，例如 `TEST_DRUG_A` 的虚构上限。
- `FixtureDDIProvider`：只读取显式虚构药物 pair 规则。
- Allergy/Contraindication provider seam：当前没有患者上下文，coverage 为未配置/无上下文。

示例文件是 `data/safety_rules.example.json`，只包含 `TEST_DRUG_A/B/C`。它不会被生产默认自动启用。

## Safety Freeze

`SafetyFreezeService` 固定拒绝：

- 关闭 `m2.check.disable`；
- 自动覆盖 `m2.block.override`；
- `BLOCK → WARN/PASS` 或 `WARN → PASS` 的严重度放宽；
- LLM/Agent/Harness 直接修改 `plan.medication.dose` 或 `plan.medication.identity`。

项目没有 `force=true` 或 BLOCK override API。`SAFETY_ENABLED` 默认开启，普通运行时不能关闭；当前也没有提供不安全测试绕过开关。

## Scheduler 防御

Occurrence 生成、Scheduler claim 和 Outbox 发提醒都会确认：

1. 当前 Plan Version 有最新 Safety Check；
2. 状态为 `PASS` 或 `WARN`；
3. `check_failed=0`；
4. `ruleset_version` 等于当前服务规则版本。

缺少检查、检查过期、规则版本变化或 BLOCK 时，不发送 `device.interaction.request`，并写入 `medication.safety.blocked`。规则升级不会把旧 PASS 自动当作新 PASS；Scheduler 会先重新检查。

## API

```text
GET  /api/v1/medication/plans/{id}/safety
GET  /api/v1/medication/plans/{id}/safety/history
POST /api/v1/medication/plans/{id}/safety/check
GET  /api/v1/medication/safety/checks/{check_id}
```

审批成功响应保留原 Plan 字段，并增加 `plan`、`safety`/`safety_check`。WARN 成功但保留 finding；BLOCK 使用 409，错误为 `SAFETY_BLOCKED` 或 `SAFETY_CHECK_FAILED`，不使用 500 放行。

## 边界

本阶段实现的是安全检查框架、结构校验和显式规则接入边界，不是正式临床数据库。当前没有 DrugBank/OpenFDA/FHIR/HIS、真实 DDI、过敏、禁忌、肝肾剂量调整或处方建议。接入真实源前需要完成供应商授权、数据版本/有效期、患者上下文绑定、单位/频次标准化、Provider 超时与回退策略、临床药师验证和回归审计。
