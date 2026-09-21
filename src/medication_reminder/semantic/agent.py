"""Natural-language boundary that delegates all business truth to the service."""

from datetime import datetime, timezone
import re

from ..service import DomainError, SHANGHAI, parse_fast_path
from ..schedule import ScheduleError, ScheduleType, ScheduleValidator
from .harness_adapter import HarnessSemanticAdapter


def normalize_schedule_time(text, schedule_time):
    """Normalize model time output using explicit Chinese day-part words."""
    if schedule_time is None:
        return schedule_time
    match = re.fullmatch(r"\s*(\d{1,2})(?::(\d{1,2}))?\s*", str(schedule_time))
    if not match:
        return schedule_time
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour > 23 or minute > 59:
        return schedule_time
    compact = str(text or "")
    if any(marker in compact for marker in ("晚上", "今晚", "夜里", "夜间", "晚间", "下午", "午后", "中午")):
        if 1 <= hour <= 11:
            hour += 12
    elif "凌晨" in compact and hour == 12:
        hour = 0
    return "%02d:%02d" % (hour, minute)


class MedicationSemanticAgent:
    """Turn user text into safe service calls; never owns medication state."""

    def __init__(self, medication_service, semantic_adapter=None):
        self.medication_service = medication_service
        self.semantic_adapter = semantic_adapter or HarnessSemanticAdapter()

    def status(self):
        return self.semantic_adapter.status()

    def handle(self, elder_id, text, source="chat_agent", created_by=None,
               interaction_id=None, event_id=None, trace_id=None, occurred_at=None):
        if not str(elder_id or "").strip():
            raise DomainError("elder_id is required")
        if not str(text or "").strip():
            raise DomainError("text is required")
        if interaction_id:
            interaction = self.medication_service.get_open_interaction(
                elder_id, interaction_id=interaction_id
            )
            if interaction is None:
                late_binding = self.medication_service.get_interaction(
                    elder_id, interaction_id
                )
                if not late_binding or late_binding.get("intake_status") != "closed_unconfirmed":
                    raise DomainError(
                        "interaction_id is not open or does not belong to elder", 409
                    )
                # A closed binding is accepted only for CONFIRM_TAKEN.  The
                # deterministic service still enforces the exact occurrence
                # binding and records late verification through M5.
                interaction = late_binding

        else:
            open_interactions = self.medication_service.get_open_interactions(elder_id)
            if len(open_interactions) > 1:
                return {
                    "kind": "clarification",
                    "requires_interaction_id": True,
                    "message": "当前有多个待回应的提醒，请由 Chat Agent 提供对应的 interaction_id。",
                }
            interaction = open_interactions[0] if open_interactions else None
        if interaction:
            fast_action, fast_delay = parse_fast_path(text)
            semantic_source = "fast_path" if fast_action else "deepseek_harness"
            action = fast_action
            delay_minutes = fast_delay
            if action is None:
                parsed = self.semantic_adapter.parse_response(text)
                action = parsed.get("action")
                delay_minutes = parsed.get("delay_minutes")
            if action not in ("CONFIRM_TAKEN", "DELAY", "SKIP", "REPEAT"):
                raise DomainError("semantic agent returned an unsupported response action", 502)
            result = self.medication_service.process_user_response({
                "elder_id": elder_id,
                "interaction_id": interaction["interaction_id"],
                "action": action,
                "delay_minutes": delay_minutes,
                "text": text,
                "source": source,
                "event_id": event_id,
                "trace_id": trace_id,
                "occurred_at": occurred_at,
            })
            return {
                "kind": "medication_response",
                "action": action,
                "occurrence": result["occurrence"],
                "semantic_source": semantic_source,
                "trace_id": result.get("trace_id"),
                "reminder_text": result.get("reminder_text"),
            }

        # Do not send ordinary conversational text to the plan extractor.
        if not any(word in text for word in (
            "提醒", "每天", "每日", "吃药", "服药", "用药", "早餐", "午餐",
            "晚餐", "餐前", "餐后", "每周", "每隔", "每8", "每 8", "周期",
            "按需", "PRN", "疼痛时", "一天", "两次", "一次", "每小时",
            "小时", "隔一段",
        )):
            return {
                "kind": "clarification",
                "message": "当前没有待回应的提醒。请说明药名、剂量和每天的提醒时间。",
            }
        parsed = self.semantic_adapter.parse_plan(text, elder_id)
        start_date_defaulted = not parsed.get("start_date")
        if start_date_defaulted:
            parsed["start_date"] = datetime.now(SHANGHAI).date().isoformat()
            missing_fields = parsed.get("missing_fields") or []
            parsed["missing_fields"] = [
                field for field in missing_fields if field != "start_date"
            ]
        original_schedule_time = parsed.get("schedule_time")
        parsed["schedule_time"] = normalize_schedule_time(text, original_schedule_time)
        schedule_time_normalized = (
            parsed.get("schedule_time") != original_schedule_time
            and parsed.get("schedule_time") is not None
        )

        explicit_schedule = (
            parsed.get("schedule_config")
            or parsed.get("schedule")
            or parsed.get("schedule_time")
        )
        missing = [
            field for field in ("drug_name", "dosage_text", "start_date")
            if not parsed.get(field)
        ]
        if not explicit_schedule:
            missing.append("schedule_config")
        if missing:
            missing.extend(
                field for field in (parsed.get("missing_fields") or [])
                if field not in missing and field != "start_date"
            )
            return {
                "kind": "plan_clarification",
                "missing_fields": missing,
                "draft": parsed,
                "message": "还需要确认：%s。" % "、".join(missing),
            }

        try:
            schedule_type, schedule_config = ScheduleValidator.validate_plan(parsed)
        except ScheduleError as exc:
            raise DomainError(exc.code, 422, dict(exc.details, code=exc.code)) from exc
        parsed["schedule_type"] = schedule_type
        parsed["schedule_config"] = schedule_config
        if schedule_type == ScheduleType.FIXED_TIME:
            parsed["schedule_time"] = schedule_config["times"][0]

        missing.extend(
            field for field in (parsed.get("missing_fields") or [])
            if field not in missing and field != "start_date"
        )
        if missing:
            return {
                "kind": "plan_clarification",
                "missing_fields": missing,
                "draft": parsed,
                "message": "还需要确认：%s。" % "、".join(missing),
            }

        draft = self.medication_service.create_draft({
            "elder_id": elder_id,
            "drug_name": parsed["drug_name"],
            "dosage_text": parsed["dosage_text"],
            "schedule_type": schedule_type,
            "schedule_config": schedule_config,
            "schedule_time": parsed.get("schedule_time"),
            "relation_to_meal": parsed.get("relation_to_meal"),
            "route": parsed.get("route") or "oral",
            "timezone": "Asia/Shanghai",
            "start_date": parsed["start_date"],
            "created_by": created_by or source,
            "source": "deepseek_harness",
        })
        message = "已生成用药计划草稿，审批后才会进入调度。"
        if schedule_type in (ScheduleType.MEAL_RELATION, ScheduleType.ROUTINE_RELATION):
            if self.medication_service.get_routine(elder_id) is None:
                message += " 当前餐次/睡前时间尚未提供，补充作息后才能审批。"
        return {
            "kind": "plan_draft_created",
            "draft": draft,
            "start_date_defaulted": start_date_defaulted,
            "schedule_time_normalized": schedule_time_normalized,
            "message": message,
        }

