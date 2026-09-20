# Phase 3 实现报告：M2 Safety Check + Safety Freeze

## 1. 实现结论

已完成确定性的 M2 安全检查框架，并将其接入 Plan approve、Plan version、Occurrence 生成、Scheduler claim、Outbox 发提醒、Event Log、HTTP API 和 Web 测试台。

普通流程现在是：

```text
Draft → Submit → M2 PASS/WARN → Active → Occurrence → Scheduler
```

BLOCK 或安全组件故障保持计划非 active，不生成 occurrence，不产生提醒交互。

这里的“完成”特指安全检查框架和安全边界已经实现；真实临床 DDI/禁忌/过敏数据库并未接入。

## 2. 修改/新增文件

- `src/medication_reminder/safety.py`：Finding/Result、Provider、Rule Pack、M2 Engine、Freeze。
- `src/medication_reminder/safety_freeze.py`：Freeze 兼容导出。
- `src/medication_reminder/storage.py`：新增 safety check/finding 表及索引。
- `src/medication_reminder/service.py`：审批事务、安全历史、规则版本重检、Scheduler 防御门。
- `src/medication_reminder/http.py`：Safety 查询/手动检查 API。
- `web/assets/app.js`、`web/assets/app.css`：Plan 安全状态与详情展示。
- `.env.example`、`data/safety_rules.example.json`：默认配置和虚构 Fixture Rule Pack。
- `tests/test_phase3_safety.py`：Phase 3 自动化测试。
- `docs/phase3_safety_check.md`：设计说明。

没有修改 LiveKit 代码，没有执行 git commit/push。

## 3. M2 架构与检查时机

`SafetyEngine` 只接收 Plan snapshot 和显式 Provider。它不导入 semantic agent，也不调用 Harness/LLM。Plan approve 在同一个 SQLite transaction 中先写 Safety Check/Finding/Event/Outbox；只有 PASS/WARN 才执行 Plan active 和 occurrence generation。

手动 `POST .../safety/check` 可对指定版本重跑并追加历史，不覆盖旧结果。Scheduler 遇到缺失或 stale check 会先重检；失败则 fail-closed。

## 4. PASS/WARN/BLOCK

Finding 的 severity 由确定性规则决定，M2 按固定聚合：任一 BLOCK → BLOCK，否则任一 WARN → WARN，否则 PASS。当前结构校验包括药名、数值剂量、正数、单位、合法 HH:MM、至少一项 medication、重复 entry 和基本计划字段。

已实现显式 dose rule 与 pairwise DDI Fixture。没有对应 Rule Pack 规则的真实药品不会被模型猜测为风险。

## 5. Coverage 语义

默认空 Provider 的 structural 为 `checked`，dose/DDI/allergy/contraindication 为 `not_configured`。因此 PASS 只表示“当前已启用检查范围没有发现问题”，不是“临床绝对安全”。Provider 已配置但查询异常会变为 BLOCK/`check_failed`，不会伪装成 PASS。

## 6. Provider 与 Rule Pack

Provider 接口允许未来替换 Local Rule Pack、DrugBank 或医院药学数据库，而不改 M2 状态机。生产默认：

```dotenv
SAFETY_ENABLED=1
SAFETY_RULE_PROVIDER=empty
SAFETY_RULESET_VERSION=empty-v1
```

示例规则只包含 `TEST_DRUG_A`、`TEST_DRUG_B`、`TEST_DRUG_C`，没有任何真实药物剂量或 DDI。

## 7. Safety Freeze

`SafetyFreezeService` 拒绝 M2 disable、BLOCK override、严重度放宽和 LLM/Agent 直接改剂量/药物身份。项目没有 `force=true`、管理员旁路或普通 API override。安全故障由 `SAFETY_CHECK_FAILED` 记录并阻止激活。

## 8. Plan approve/revise 集成

approve 现在返回原有 Plan 字段以及 `plan`、`safety`、`safety_check`。WARN 仍成功但保存 finding 和 `medication.safety.warning`；BLOCK 用 409 返回检查 ID、findings、coverage，计划保持 `pending_confirmation`。

revise 继续创建新版本 draft。新版本必须重新检查，不能继承旧版本 PASS；旧版本 check/finding 保留，形成 `Plan v1 → Safety v1`、`Plan v2 → Safety v2`。

重复 approve 在已 active 且安全结果仍有效时返回同一版本，不重复生成 occurrence。

## 9. Scheduler 变化

Occurrence generation、`_claim_due` 和 `_publish_reminder_due` 都检查最新版本 Safety Check、PASS/WARN、`check_failed=0` 和当前 ruleset version。失效时不会生成或发送 `device.interaction.request`，并记录 `medication.safety.blocked`。

## 10. 数据库 schema

新增：

- `medication_safety_check(check_id, plan_id, plan_version, status, ruleset_version, coverage_json, checked_at, trace_id, check_failed, error_message, created_at)`；
- `medication_safety_finding(finding_id, check_id, category, severity, code, message, rule_id, rule_version, evidence_json, created_at)`。

使用现有 `CREATE TABLE IF NOT EXISTS` 启动 schema 方式，不删除 `data/medication.db`，旧库可通过启动自动补表。

## 11. Event 契约

每次检查写入 `medication.safety.checked`。WARN 追加 `medication.safety.warning`；规则阻断追加 `medication.safety.blocked`；引擎/Provider 故障追加 `medication.safety.check_failed`。事件 payload 包含 `check_id`、Plan version、status、ruleset、timestamp、trace、coverage 和 findings，并进入 Event Log/Transactional Outbox。

## 12. API/Web

新增最新检查、历史、手动检查和 check_id 查询 API。Web Plan 表显示安全状态、检查时间、ruleset、coverage 和 findings；前端展示不构成后端安全边界。

## 13. 测试与验证

新增 `tests/test_phase3_safety.py` 12 个测试，覆盖：空 Provider coverage、结构阻断、虚构 dose BLOCK、pairwise DDI BLOCK/WARN、WARN 放行、Provider/Engine 故障、revision 隔离、ruleset stale recheck、重复审批幂等、Safety API 和 Freeze。

回归结果：

```text
tests/                 64 tests, OK (含 Phase 3)
tests/test_phase3...   12 tests, OK
text_bridge/tests/     10 tests, OK
node --check app.js    OK
```

## 14. HTTP Smoke Test

已通过独立 ThreadingHTTPServer 验证 health、Draft、Approve（含 Safety PASS）、latest/history/check 查询和 Scheduler 路径；BLOCK 409 及详情也已由 Application/API 边界测试覆盖。

## 15. 当前真正具备的医学检查能力

当前可靠能力是计划结构检查，以及只对显式 Rule Pack 中虚构测试药物执行的 dose/配对规则匹配。它可以稳定产生 PASS/WARN/BLOCK、保存证据和阻断调度。

## 16. 尚未具备的医学检查能力

没有真实 DDI、过敏、禁忌、肝肾功能、处方适应证、最大剂量临床数据库，也没有 FHIR/HIS/DrugBank/OpenFDA 接入。默认 PASS 不应被解释为临床许可。

## 17. 下一阶段建议

先建立可信患者上下文和身份/RBAC，再接入经药师验证、可版本化、有超时和审计回执的正式药学 Provider；之后补单位/频次标准化、数据有效期、Provider 合同测试、临床 shadow mode 和人工复核闭环。不要先让 LLM 代替规则源或添加 BLOCK override。
