# Phase 4：Advanced Scheduling

## 1. 范围

Phase 4 把旧的单一 `schedule_time` 扩展为结构化、确定性的 Schedule Model。自然语言层只负责产生 Draft；日期、时区、餐次偏移和周期计算由 Python 代码完成。

当前只支持 `Asia/Shanghai`，内部所有持久化时间使用 timezone-aware datetime 并以 UTC ISO-8601 字符串保存。

## 2. Schedule Model

Plan Version 保存：

```json
{
  "schedule_type": "FIXED_TIME",
  "schedule_config": {"times": ["08:00", "20:00"]},
  "timezone": "Asia/Shanghai"
}
```

旧数据只有 `schedule_type=daily` 和 `schedule_time=08:00` 时，读取层规范化为 `FIXED_TIME` 与 `{"times":["08:00"]}`，不猜测第二个时刻。

## 3. Schedule Types

- `FIXED_TIME`：每天一个或多个 `HH:MM`，规范化为唯一、升序数组。
- `MEAL_RELATION`：`meal=BREAKFAST|LUNCH|DINNER`、`relation=BEFORE|AFTER`、非负 `offset_minutes`。需要 Elder Routine 的对应 anchor。
- `INTERVAL`：`interval_hours` 为 1–168 的整数，并且必须有 `anchor_at`。anchor 可以是带时区的 ISO datetime，也可以是结合计划开始日期解释的明确 `HH:MM`。
- `WEEKLY`：ISO weekday `1=Monday ... 7=Sunday`，配合一个或多个时间。
- `CYCLE`：`cycle_start_date`、`days_on >= 1`、`days_off >= 0` 和时间数组。按 `(date - cycle_start_date) % (days_on + days_off)` 判断 on/off。
- `PRN`：只保存 `condition_text`，不会自动展开 occurrence，也不会生成每日 reminder。

另外提供轻量 `ROUTINE_RELATION` 扩展，用于 `BEDTIME` 前/后偏移；它不是本阶段新的临床规则。

## 4. Elder Routine

`elder_routine` 只保存显式上下文：

```text
elder_id, breakfast_time, lunch_time, dinner_time, bedtime,
timezone, updated_at
```

通过 `GET/PUT /api/v1/medication/elders/{elder_id}/routine` 管理。缺少餐次或睡前 anchor 时返回 `SCHEDULE_CONTEXT_MISSING`，系统不会默认早餐为 08:00。

## 5. Validation

`ScheduleValidator` 在 draft 创建和 revise 时执行结构校验。结构错误返回 `SCHEDULE_INVALID`；未知或当前未实现的类型返回 `SCHEDULE_UNSUPPORTED`；结构完整但缺少 routine context 在 approve/preview 时返回 `SCHEDULE_CONTEXT_MISSING`。

Schedule 错误不会进入 M2 Safety。合法 Schedule 仍必须经过现有 `M2 -> PASS/WARN -> Active` 生命周期。

## 6. Expansion

`ScheduleExpander` 是 preview 和正式 occurrence generation 共用的唯一算法：

1. 先把 schedule canonicalize。
2. 建立 `[window_start, window_end)` 的 timezone-aware 窗口。
3. 按 Schedule Type 计算候选 datetime。
4. 用 datetime 运算处理跨日餐前/餐后和 interval 跨午夜。
5. 排序、去重，生成 `OccurrenceSpec`。

默认 horizon 是未来 7 天；服务重启或 scheduler 重复运行会重新填充同一窗口。

## 7. Occurrence 审计与幂等

Occurrence 保存：

```text
schedule_type
schedule_snapshot_json
schedule_source
```

身份由 `plan_id + plan_version + scheduled_at` 的稳定 hash 生成，同时保留数据库唯一约束 `(plan_id, plan_version, scheduled_at)`。因此重复 approve、重复 refill、scheduler 重启不会重复生成同一次 occurrence。

## 8. Plan Revision

Active Plan 不原地更新 Schedule。`POST /plans/{id}/revise` 创建新版本，重新经过 schedule validation、Safety Check 和 approve。新版本激活时只取消旧版本未来且未确认的 occurrence；历史 occurrence 保留原 medication/schedule snapshot。

## 9. Routine Change

routine 变化会找到引用该 anchor 的 Active Plan：

1. 保存 routine 新值并记录事件。
2. 对当前 Plan Version 重新执行 M2。
3. M2 PASS/WARN 时取消未来未确认 occurrence 并用相同 Plan Version 重算。
4. 历史 occurrence 不修改；M2 BLOCK 时不生成新的 future occurrence。

也可以显式调用 `POST /api/v1/medication/plans/{id}/schedule/recalculate`。

## 10. Safety 集成

Schedule validation 是 M2 之前的领域门禁。M2 仍负责药物名称、剂量及已配置的安全规则；Phase 4 没有加入 DDI、过敏或剂量推理。新的 Plan Version 不继承旧 Safety Check，routine context change 也会建立新的当前版本 Safety Check 历史。

## 11. Semantic Agent 边界

Harness 可以输出：

```json
{
  "schedule_type": "MEAL_RELATION",
  "schedule_config": {
    "meal": "BREAKFAST",
    "relation": "AFTER",
    "offset_minutes": 30
  }
}
```

它不能输出或猜测 `breakfast_time`，也不能把“一天两次”变成 08:00/20:00。缺少明确参数时只返回 clarification 或保留 Draft，不会 submit/approve。

## 12. API

现有统一接口继续使用：

```text
POST /api/v1/medication/plans/draft
POST /api/v1/medication/plans/{id}/submit
POST /api/v1/medication/plans/{id}/approve
POST /api/v1/medication/plans/{id}/revise
```

新增：

```text
GET  /api/v1/medication/elders/{elder_id}/routine
PUT  /api/v1/medication/elders/{elder_id}/routine
GET  /api/v1/medication/plans/{id}/schedule/preview?horizon_days=7
POST /api/v1/medication/plans/{id}/schedule/recalculate
```

Preview 返回 `items`，含 `scheduled_at`、local 时间、type、snapshot 和 source；preview 不写数据库、不触发 reminder。

## 13. Events

已加入审计事件：

```text
medication.schedule.validated
medication.schedule.expanded
medication.schedule.recalculated
medication.routine.updated
```

schedule invalid 不创建一个虚假的 Plan 事件；Safety 事件仍按 Phase 3 既有规则记录。

## 14. Schema Migration

采用现有幂等 `PRAGMA table_info` + `ALTER TABLE ADD COLUMN` 方式：

- `medication_plan.schedule_config_json`
- `medication_occurrence.schedule_type`
- `medication_occurrence.schedule_snapshot_json`
- `medication_occurrence.schedule_source`
- 新表 `elder_routine`

没有删除 `data/medication.db`，也没有重建旧表。

## 15. Web

Web 计划创建区增加 schedule type 和对应参数；计划列表显示 canonical type/config 摘要以及当前已生成的下一次 occurrence。浏览器校验只是体验层，后端 `ScheduleValidator` 仍是最终边界。

## 16. 当前不支持

不支持复杂 PRN trigger/给药确认、复杂多阶段临床周期、剂量自动调整、全球时区/DST、真实药学数据源、FHIR/HIS/DrugBank/OpenFDA 和医生/家属真实通知系统。

## 17. Phase 4.1 修补

Phase 4.1 是对 Advanced Scheduling 的一致性修补，不引入新的 Schedule Type，也不实现 PRN 自动触发。修补后的规则如下。

### 17.1 Routine Version

`elder_routine.routine_version` 从 `1` 开始。只有早餐、午餐、晚餐、睡前 anchor 或 timezone 的规范化值发生真实变化时才递增；相同值重复 PUT 不改变 `routine_version`、`updated_at`，也不触发事件或重算。

Occurrence 不依赖未来的 `elder_routine` 查询解释历史。Routine 变化时，新生成的 occurrence 引用新版本，旧 occurrence 的 snapshot 永不回写。

### 17.2 Resolved Schedule Snapshot

`medication_occurrence.schedule_snapshot_json` 保存生成该 occurrence 时实际参与计算的最小不可变上下文。除 canonical schedule 配置外，按类型包含：

- `MEAL_RELATION` / `ROUTINE_RELATION`：`resolved_anchor_type`、`resolved_anchor_time`、`resolved_anchor_local_datetime`、`resolved_local_datetime`、`routine_version`。
- `FIXED_TIME`：`times` 和本次的 `selected_time`。
- `INTERVAL`：`anchor_at`、`interval_hours`、本次的 `interval_index`。
- `WEEKLY`：`weekdays`、`times`、`selected_weekday`、`selected_time`。
- `CYCLE`：`cycle_start_date`、`days_on`、`days_off`、本次的 `cycle_day`/`cycle_day_index`、`selected_time`。

例如早餐 08:00 后 30 分钟生成的 snapshot 至少可以独立说明：anchor 是早餐、当时为 08:00、routine version 为 1、最终本地执行时间为 08:30。

### 17.3 Routine Change Transaction

Routine PUT、routine version 写入、M2 重新检查、未来未确认 occurrence 取消/恢复或生成，以及 `medication.routine.updated` / `medication.schedule.recalculated` 事件，均在同一个 SQLite `BEGIN IMMEDIATE` 事务内执行。任一步失败都会 rollback；故障注入测试验证不会留下“新 routine + 旧 active future occurrence”的半更新状态。

M2 返回 `BLOCK` 时允许保存新 routine，但同一事务会保存 BLOCK 结果并取消相关未来 occurrence，不会生成新的 active future occurrence。重复 recalculation 使用原有 `plan_id + plan_version + scheduled_at` identity 恢复同一可重算 occurrence，不创建重复行。

### 17.4 Effective Range Filtering

ScheduleExpander 在所有 Schedule Type 的最终 candidate filtering 阶段统一使用半开区间：

```text
effective_from <= scheduled_at < effective_until
```

其中：

- Active Plan 的 `effective_from` 使用实际激活时间；没有激活时间的 draft/直接 Expander plan 使用 `start_date 00:00` 作为下界。
- 直接传入的 `effective_until` 按 exclusive 处理。
- 现有持久化 Plan 的 `end_date` 保持原有“日历日包含”语义，并统一转换为次日 00:00 的 exclusive upper bound。
- Meal 前移跨日、interval anchor 早于生效时间、cycle start 早于生效时间都可以继续用于数学计算，但候选最终必须落在有效区间内。
- Preview 和正式 occurrence generation 调用同一个 Expander，并且都取 requested horizon 与 Plan effective range 的交集。

因此，生效前的早餐前提醒不会生成；`scheduled_at == effective_from` 可以生成；`scheduled_at == effective_until` 不会生成。

### 17.5 Schema Migration

新增 `elder_routine.routine_version INTEGER NOT NULL DEFAULT 1`。启动时通过已有的 `PRAGMA table_info` + `ALTER TABLE ADD COLUMN` 幂等迁移旧数据库，不要求删除 `data/medication.db`。没有新增 Routine History 表；Occurrence resolved snapshot 承担本次历史解释职责。

### 17.6 Occurrence Identity 的未来 TODO

当前业务假设一个 Plan Version 只有一个 Medication Entry，因此继续使用：

```text
plan_id + plan_version + scheduled_at
```

如果未来一个 Plan 支持多个 Medication Entry，identity 需要升级为：

```text
plan_id + plan_version + medication_entry_id + scheduled_at
```

Phase 4.1 不实现多 Medication Entry Plan。
