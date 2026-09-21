"""Deterministic medication reminder domain services."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import os
import re
import uuid

from .config import load_local_env
from .device_adapter import DeviceAdapterError, build_device_adapter
from .confirmation import (
    ASSESSMENT_RESULTS,
    EVIDENCE_TYPES,
    POLICY_VERSION,
    SOURCE_TYPES,
    TAKEN_EVIDENCE_TYPES,
    assess as assess_confirmation_evidence,
    policy_fingerprint as confirmation_policy_fingerprint,
)
from .escalation import (
    ALL_LEVELS,
    ALL_STATUSES,
    LEVEL_CAREGIVER,
    LEVEL_FAMILY,
    LEVEL_MANUAL_REVIEW,
    RESOLUTION_CODES,
    STATUS_ACKNOWLEDGED,
    STATUS_CANCELLED,
    STATUS_OPEN,
    STATUS_RESOLVED,
    initial_level,
    next_level,
    notification_event_type,
    target_role,
)
from .storage import Storage
from .routine import ElderRoutine, RoutineError
from .schedule import (
    ScheduleError,
    ScheduleExpander,
    ScheduleType,
    ScheduleValidator,
)
from .safety import (
    SafetyCheckResult,
    SafetyFinding,
    SafetyFreezeError,
    SafetyFreezeService,
    STATUS_BLOCK,
    STATUS_CHECK_FAILED,
    STATUS_PASS,
    STATUS_WARN,
    _coverage_summary,
    build_safety_engine,
)


UTC = timezone.utc
SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
SUPPORTED_TIMEZONE = "Asia/Shanghai"
UNCONFIRMED = "unconfirmed"
ESCALATION_NOTIFICATION_EVENTS = (
    "caregiver.task.assign", "family_notify.request", "manual_review.request",
)


class DomainError(Exception):
    """An expected API/domain error."""

    def __init__(self, message, status=400, details=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.details = details or {}


def new_id(prefix):
    return "%s_%s" % (prefix, uuid.uuid4().hex)


def iso(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def parse_datetime(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise DomainError("invalid datetime: %s" % value) from exc
    if parsed.tzinfo is None:
        raise DomainError("datetime must include timezone offset")
    return parsed.astimezone(UTC)


def parse_date(value, field_name="date"):
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise DomainError("%s must be YYYY-MM-DD" % field_name) from exc


def now_utc():
    return datetime.now(UTC)


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


def _env_bool(name, default=True):
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def row_dict(row):
    return dict(row) if row is not None else None


def normalize_action(action):
    if action is None:
        return None
    normalized = str(action).strip().upper()
    aliases = {
        "TAKEN": "CONFIRM_TAKEN",
        "CONFIRM": "CONFIRM_TAKEN",
        "SKIP": "SKIP",
        "DELAY": "DELAY",
        "REPEAT": "REPEAT",
    }
    return aliases.get(normalized, normalized)


def parse_fast_path(text, default_delay_minutes=30):
    """Small deterministic response parser; an LLM may replace this adapter."""

    if not text:
        return None, None
    compact = re.sub(r"[\s，。！!？?、,.；;]+", "", str(text).lower())
    confirm_terms = (
        "吃了", "已经吃了", "吃过了", "吃完了", "我服了", "服了", "已服",
        "吃好了", "刚吃了", "我刚刚吃过了",
    )
    skip_terms = ("跳过", "不吃", "今天不吃", "这次不吃", "不用吃")
    repeat_terms = (
        "再说一遍", "重复一下", "再讲一遍", "再提醒一下", "刚才说什么", "没听清",
    )
    if any(term in compact for term in skip_terms):
        return "SKIP", None
    if any(term in compact for term in confirm_terms):
        return "CONFIRM_TAKEN", None
    if any(term in compact for term in repeat_terms):
        return "REPEAT", None
    if "半小时" in compact or "半个小时" in compact:
        return "DELAY", 30
    if "一小时" in compact or "1小时" in compact:
        return "DELAY", 60
    if "十分钟" in compact:
        return "DELAY", 10
    minute_match = re.search(r"(\d{1,3})分钟", compact)
    if minute_match:
        return "DELAY", int(minute_match.group(1))
    if any(term in compact for term in ("晚点", "等一哈", "等会", "等会儿", "等一下", "过会儿")):
        return "DELAY", default_delay_minutes
    return None, None


class MedicationService:
    """Application service containing the MVP state machine."""

    def __init__(self, database=":memory:", config=None, device_adapter=None):
        load_local_env()
        self.storage = Storage(database)
        self.config = {
            "occurrence_window_days": 7,
            "default_confirmation_window_minutes": 120,
            "default_max_snooze_count": 3,
            "default_delay_minutes": 30,
            "interaction_ttl_minutes": 30,
            "outbox_lease_seconds": 60,
            # M5 values are engineering policy defaults, not clinical
            # parameters.  They are persisted on every assessment so a later
            # policy change cannot silently rewrite historical conclusions.
            "confirmation_policy_version": os.environ.get(
                "CONFIRMATION_POLICY_VERSION", POLICY_VERSION
            ),
            "confirmation_policy_fingerprint": os.environ.get(
                "CONFIRMATION_POLICY_FINGERPRINT", confirmation_policy_fingerprint()
            ),
            "evidence_pre_window_minutes": _env_int(
                "EVIDENCE_PRE_WINDOW_MINUTES", 120
            ),
            "evidence_post_window_minutes": _env_int(
                "EVIDENCE_POST_WINDOW_MINUTES", 120
            ),
            "evidence_clock_skew_seconds": _env_int(
                "EVIDENCE_CLOCK_SKEW_SECONDS", 300
            ),
            "device_adapter": os.environ.get("MEDICATION_DEVICE_ADAPTER", "local"),
            "chat_agent_base_url": os.environ.get("CHAT_AGENT_BASE_URL", ""),
            "chat_agent_reminder_path": os.environ.get("CHAT_AGENT_REMINDER_PATH", ""),
            "chat_agent_timeout_seconds": os.environ.get("CHAT_AGENT_TIMEOUT_SECONDS", "5"),
            "chat_agent_api_token": os.environ.get("CHAT_AGENT_API_TOKEN", ""),
            # M6 values are engineering/test defaults, not clinical guidance.
            "escalation_enabled": _env_bool("ESCALATION_ENABLED", True),
            "escalation_caregiver_timeout_minutes": _env_int(
                "ESCALATION_CAREGIVER_TIMEOUT_MINUTES", 30
            ),
            "escalation_family_timeout_minutes": _env_int(
                "ESCALATION_FAMILY_TIMEOUT_MINUTES", 60
            ),
            "escalation_ack_resolution_timeout_minutes": _env_int(
                "ESCALATION_ACK_RESOLUTION_TIMEOUT_MINUTES", 30
            ),
            "escalation_repeat_missed_count": _env_int(
                "ESCALATION_REPEAT_MISSED_COUNT", 2
            ),
            "escalation_repeat_missed_lookback_hours": _env_int(
                "ESCALATION_REPEAT_MISSED_LOOKBACK_HOURS", 24
            ),
            "escalation_skip_trigger_count": _env_int(
                "ESCALATION_SKIP_TRIGGER_COUNT", 3
            ),
            "escalation_skip_lookback_hours": _env_int(
                "ESCALATION_SKIP_LOOKBACK_HOURS", 24
            ),
            "escalation_silence_window_minutes": _env_int(
                "ESCALATION_SILENCE_WINDOW_MINUTES", 0
            ),
            # M2 is enabled by default and cannot be disabled through a
            # normal runtime configuration/API mutation.
            "safety_enabled": _env_bool("SAFETY_ENABLED", True),
            "safety_rule_provider": os.environ.get("SAFETY_RULE_PROVIDER", "empty"),
            "safety_ruleset_version": os.environ.get(
                "SAFETY_RULESET_VERSION", "empty-v1"
            ),
            "safety_rule_file": os.environ.get("SAFETY_RULE_FILE", ""),
        }
        if config:
            self.config.update(config)
        self.safety_freeze = SafetyFreezeService()
        try:
            self.safety_freeze.assert_safety_enabled(self.config.get("safety_enabled", True))
        except SafetyFreezeError as exc:
            raise RuntimeError(str(exc)) from exc
        self.safety_engine = build_safety_engine(self.config)
        self.safety_ruleset_version = self.safety_engine.ruleset_version
        self.safety_ruleset_fingerprint = self.safety_engine.ruleset_fingerprint
        self.confirmation_policy_version = str(
            self.config.get("confirmation_policy_version") or POLICY_VERSION
        )
        self.confirmation_policy_fingerprint = str(
            self.config.get("confirmation_policy_fingerprint") or confirmation_policy_fingerprint()
        )
        self.schedule_expander = ScheduleExpander(SUPPORTED_TIMEZONE)
        self.device_adapter = device_adapter or build_device_adapter(self.config)
        # Read-only dashboard queries can use independent SQLite connections.
        self.read_executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="medication-read"
        )

    def close(self):
        self.read_executor.shutdown(wait=True, cancel_futures=True)
        close_adapter = getattr(self.device_adapter, "close", None)
        if close_adapter:
            close_adapter()
        self.storage.close()

    # ---------- event/outbox primitives ----------

    def _event(self, event_type, source, elder_id=None, payload=None,
               occurred_at=None, event_id=None, plan_id=None,
               occurrence_id=None, trace_id=None):
        occurred = occurred_at or now_utc()
        event = {
            "event_id": event_id or new_id("evt"),
            "event_type": event_type,
            "occurred_at": iso(occurred),
            "source": source,
            "elder_id": elder_id,
            "plan_id": plan_id,
            "occurrence_id": occurrence_id,
            "payload": payload or {},
        }
        resolved_trace_id = trace_id or event["payload"].get("trace_id")
        if resolved_trace_id:
            event["trace_id"] = resolved_trace_id
        return event

    def _insert_event_log(self, connection, event, processed_at=None):
        cursor = connection.execute(
            """INSERT OR IGNORE INTO medication_event_log
               (event_id, elder_id, plan_id, occurrence_id, event_type, source,
                payload_json, occurred_at, received_at, processed_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event["event_id"],
                event.get("elder_id"),
                event.get("plan_id"),
                event.get("occurrence_id"),
                event["event_type"],
                event.get("source", "unknown"),
                json_text(event.get("payload", {})),
                event["occurred_at"],
                iso(now_utc()),
                iso(processed_at or now_utc()),
                iso(now_utc()),
            ),
        )
        return cursor.rowcount == 1

    def _enqueue_outbox(self, connection, event, dedup_key=None):
        connection.execute(
            """INSERT OR IGNORE INTO domain_outbox
               (event_id, event_type, payload_json, dedup_key, status,
                retry_count, created_at)
               VALUES (?, ?, ?, ?, 'pending', 0, ?)""",
            (
                event["event_id"],
                event["event_type"],
                json_text(event),
                dedup_key,
                iso(now_utc()),
            ),
        )

    def _audit_and_enqueue(self, connection, event, dedup_key=None):
        self._insert_event_log(connection, event)
        self._enqueue_outbox(connection, event, dedup_key=dedup_key)

    def _event_already_logged(self, connection, event_id):
        row = connection.execute(
            "SELECT event_id FROM medication_event_log WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        return row is not None

    # ---------- M5 evidence and confirmation ----------

    @staticmethod
    def _normalize_evidence_source(value):
        normalized = str(value or "").strip().upper().replace("-", "_")
        aliases = {
            "VOICE": "USER_VOICE",
            "BUTTON": "USER_BUTTON",
            "MANUAL": "MANUAL_OPERATOR",
        }
        return aliases.get(normalized, normalized)

    @staticmethod
    def _normalize_evidence_type(value):
        return str(value or "").strip().upper().replace("-", "_")

    @staticmethod
    def _evidence_value(data):
        if "value" in data:
            value = data.get("value")
        elif "value_json" in data:
            value = data.get("value_json")
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise DomainError("value_json must contain valid JSON", 422) from exc
        else:
            value = {}
            for key in (
                "raw_before", "raw_after", "delta", "delta_grams", "unit",
                "normalized", "text", "action", "late_verified_source",
                "late_verified_note", "confirmation_method",
            ):
                if key in data:
                    value[key] = data[key]
        if value is None:
            value = {}
        try:
            json_text(value)
        except (TypeError, ValueError) as exc:
            raise DomainError("value must be JSON serializable", 422) from exc
        return value

    def _evidence_binding_in_transaction(self, connection, data):
        occurrence_id = str(data.get("occurrence_id") or "").strip()
        interaction_id = str(data.get("interaction_id") or "").strip() or None
        if not occurrence_id and interaction_id:
            interaction = connection.execute(
                "SELECT * FROM medication_interaction WHERE interaction_id=?",
                (interaction_id,),
            ).fetchone()
            if interaction is None:
                raise DomainError("interaction not found", 404)
            occurrence_id = interaction["occurrence_id"]
        if not occurrence_id:
            raise DomainError("occurrence_id is required")
        occurrence = connection.execute(
            "SELECT * FROM medication_occurrence WHERE occurrence_id=?",
            (occurrence_id,),
        ).fetchone()
        if occurrence is None:
            raise DomainError("occurrence not found", 404)
        if data.get("elder_id") and data["elder_id"] != occurrence["elder_id"]:
            raise DomainError("elder_id does not match occurrence", 409)
        if interaction_id:
            interaction = connection.execute(
                "SELECT * FROM medication_interaction WHERE interaction_id=?",
                (interaction_id,),
            ).fetchone()
            if interaction is None:
                raise DomainError("interaction not found", 404)
            if interaction["occurrence_id"] != occurrence_id:
                raise DomainError("interaction_id does not match occurrence", 409)
            if data.get("elder_id") and data["elder_id"] != interaction["elder_id"]:
                raise DomainError("elder_id does not match interaction", 409)
        return occurrence, interaction_id

    def _evidence_window_flags(self, occurrence, observed_at):
        pre_minutes = max(0, int(self.config.get("evidence_pre_window_minutes", 120)))
        post_minutes = max(0, int(self.config.get("evidence_post_window_minutes", 120)))
        start = parse_datetime(occurrence["scheduled_at"]) - timedelta(minutes=pre_minutes)
        end = parse_datetime(occurrence["confirmation_deadline_at"]) + timedelta(minutes=post_minutes)
        return observed_at < start or observed_at > end

    @staticmethod
    def _hydrate_evidence(row):
        if row is None:
            return None
        item = row_dict(row)
        try:
            item["value"] = json.loads(item.pop("value_json"))
        except (TypeError, ValueError, json.JSONDecodeError):
            item["value"] = {}
            item.pop("value_json", None)
        for key in ("identity_trusted", "source_trusted", "out_of_window", "invalid"):
            item[key] = bool(item.get(key, 0))
        return item

    @staticmethod
    def _hydrate_assessment(row):
        if row is None:
            return None
        item = row_dict(row)
        try:
            item["evidence_ids"] = json.loads(item.pop("evidence_ids_json"))
        except (TypeError, ValueError, json.JSONDecodeError):
            item["evidence_ids"] = []
            item.pop("evidence_ids_json", None)
        for key in ("conflict_detected", "review_required", "late"):
            item[key] = bool(item.get(key, 0))
        return item

    def _record_evidence_in_transaction(self, connection, data, *, assess=True,
                                        identity_trusted=False, source_trusted=False,
                                        require_event_id=True, received_at=None):
        data = dict(data or {})
        event_id = str(data.get("event_id") or "").strip()
        if require_event_id and not event_id:
            raise DomainError("event_id is required for medication evidence")
        if not event_id:
            event_id = new_id("evidence_evt")
        source_type = self._normalize_evidence_source(data.get("source_type"))
        evidence_type = self._normalize_evidence_type(data.get("evidence_type"))
        if source_type not in SOURCE_TYPES:
            raise DomainError("unsupported evidence source_type", 422)
        if evidence_type not in EVIDENCE_TYPES:
            if evidence_type == "OVERDOSE_CONFIRMED":
                raise DomainError("OVERDOSE_CONFIRMED is not supported by M5", 422)
            raise DomainError("unsupported evidence_type", 422)

        existing = connection.execute(
            "SELECT * FROM medication_evidence WHERE event_id=?", (event_id,)
        ).fetchone()
        if existing is not None:
            if data.get("occurrence_id") and data["occurrence_id"] != existing["occurrence_id"]:
                raise DomainError("event_id is already bound to another occurrence", 409)
            if data.get("elder_id") and data["elder_id"] != existing["elder_id"]:
                raise DomainError("event_id is already bound to another elder", 409)
            if self._normalize_evidence_type(data.get("evidence_type")) != existing["evidence_type"]:
                raise DomainError("event_id is already used by another evidence type", 409)
            assessment = connection.execute(
                """SELECT * FROM medication_confirmation_assessment
                   WHERE occurrence_id=? ORDER BY assessed_at DESC, created_at DESC
                   LIMIT 1""",
                (existing["occurrence_id"],),
            ).fetchone()
            return {
                "duplicate": True,
                "evidence": existing,
                "assessment": assessment,
                "occurrence_id": existing["occurrence_id"],
                "late": bool(assessment and assessment["late"]),
            }
        logged = connection.execute(
            "SELECT event_type FROM medication_event_log WHERE event_id=?", (event_id,)
        ).fetchone()
        if logged is not None:
            raise DomainError("event_id is already used by another event", 409)

        occurrence, interaction_id = self._evidence_binding_in_transaction(connection, data)
        clock = received_at or now_utc()
        observed_at = parse_datetime(data["observed_at"]) if data.get("observed_at") else clock
        skew_seconds = max(0, int(self.config.get("evidence_clock_skew_seconds", 300)))
        if observed_at > clock + timedelta(seconds=skew_seconds):
            raise DomainError(
                "observed_at is too far in the future", 422,
                {"observed_at": iso(observed_at), "received_at": iso(clock),
                 "clock_skew_seconds": skew_seconds},
            )
        if source_type == "USER_VOICE" and not interaction_id:
            raise DomainError("USER_VOICE evidence requires a trusted interaction_id", 409)
        if occurrence["intake_status"] == UNCONFIRMED and evidence_type in TAKEN_EVIDENCE_TYPES:
            if parse_datetime(occurrence["confirmation_deadline_at"]) <= clock:
                raise DomainError(
                    "confirmation window is closed; run scheduler before late verification",
                    409,
                )
        value = self._evidence_value(data)
        out_of_window = self._evidence_window_flags(occurrence, observed_at)
        trace_id = str(data.get("trace_id") or "evidence:%s" % event_id)
        evidence_id = str(data.get("evidence_id") or new_id("evidence"))
        actor_id = str(data.get("actor_id") or "").strip() or None
        actor_role = str(data.get("actor_role") or "").strip() or None
        device_id = str(data.get("device_id") or "").strip() or None
        created_at = iso(clock)
        connection.execute(
            """INSERT INTO medication_evidence
               (evidence_id, event_id, elder_id, occurrence_id, interaction_id,
                source_type, evidence_type, value_json, observed_at, received_at,
                actor_id, actor_role, device_id, identity_trusted, source_trusted,
                trace_id, out_of_window, invalid, invalid_reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?)""",
            (
                evidence_id, event_id, occurrence["elder_id"], occurrence["occurrence_id"],
                interaction_id, source_type, evidence_type, json_text(value), iso(observed_at),
                iso(clock), actor_id, actor_role, device_id, int(bool(identity_trusted)),
                int(bool(source_trusted)), trace_id, int(bool(out_of_window)), created_at,
            ),
        )
        evidence_event = self._event(
            "medication.evidence.recorded", "m5_evidence",
            elder_id=occurrence["elder_id"], plan_id=occurrence["plan_id"],
            occurrence_id=occurrence["occurrence_id"], payload={
                "evidence_id": evidence_id, "event_id": event_id,
                "interaction_id": interaction_id, "source_type": source_type,
                "evidence_type": evidence_type, "value": value,
                "observed_at": iso(observed_at), "received_at": iso(clock),
                "actor_id": actor_id, "actor_role": actor_role, "device_id": device_id,
                "identity_trusted": bool(identity_trusted),
                "source_trusted": bool(source_trusted),
                "out_of_window": bool(out_of_window), "trace_id": trace_id,
            },
            occurred_at=observed_at, event_id=event_id, trace_id=trace_id,
        )
        self._audit_and_enqueue(connection, evidence_event, "evidence:%s" % evidence_id)

        assessment = None
        late = bool(
            occurrence["intake_status"] == "closed_unconfirmed"
            and evidence_type in TAKEN_EVIDENCE_TYPES and not out_of_window
        )
        if assess and not out_of_window:
            assessment = self._create_confirmation_assessment_in_transaction(
                connection, occurrence, trace_id=trace_id, assessed_at=clock, late=late
            )
        return {
            "duplicate": False,
            "evidence": connection.execute(
                "SELECT * FROM medication_evidence WHERE evidence_id=?", (evidence_id,)
            ).fetchone(),
            "assessment": assessment,
            "occurrence_id": occurrence["occurrence_id"],
            "late": late,
        }

    def _create_confirmation_assessment_in_transaction(self, connection, occurrence,
                                                        trace_id, assessed_at, late=False):
        rows = connection.execute(
            """SELECT * FROM medication_evidence
               WHERE occurrence_id=? AND invalid=0 AND out_of_window=0
               ORDER BY observed_at, created_at, evidence_id""",
            (occurrence["occurrence_id"],),
        ).fetchall()
        policy = assess_confirmation_evidence([row_dict(row) for row in rows])
        if not policy["evidence_ids"]:
            return None
        assessment_id = new_id("assessment")
        assessment_late = bool(
            late or (
                occurrence["intake_status"] == "closed_unconfirmed"
                and any(item["evidence_type"] in TAKEN_EVIDENCE_TYPES for item in rows)
            )
        )
        created_at = iso(assessed_at)
        connection.execute(
            """INSERT INTO medication_confirmation_assessment
               (assessment_id, occurrence_id, result, basis, policy_version,
                policy_fingerprint, evidence_ids_json, conflict_detected,
                review_required, late, assessed_at, trace_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                assessment_id, occurrence["occurrence_id"], policy["result"],
                policy["basis"], self.confirmation_policy_version,
                self.confirmation_policy_fingerprint, json_text(policy["evidence_ids"]),
                int(bool(policy["conflict_detected"])), int(bool(policy["review_required"])),
                int(assessment_late), iso(assessed_at), trace_id, created_at,
            ),
        )
        assessment_payload = {
            "assessment_id": assessment_id,
            "occurrence_id": occurrence["occurrence_id"],
            "result": policy["result"],
            "basis": policy["basis"],
            "policy_version": self.confirmation_policy_version,
            "policy_fingerprint": self.confirmation_policy_fingerprint,
            "evidence_ids": policy["evidence_ids"],
            "conflict_detected": bool(policy["conflict_detected"]),
            "review_required": bool(policy["review_required"]),
            "late": assessment_late,
            "trace_id": trace_id,
        }
        assessed_event = self._event(
            "medication.confirmation.assessed", "m5_confirmation",
            elder_id=occurrence["elder_id"], plan_id=occurrence["plan_id"],
            occurrence_id=occurrence["occurrence_id"], payload=assessment_payload,
            occurred_at=assessed_at, trace_id=trace_id,
        )
        self._audit_and_enqueue(
            connection, assessed_event,
            "confirmation_assessed:%s" % assessment_id,
        )
        if policy["result"] == "CONFIRMED":
            confirmed_event = self._event(
                "medication.confirmation.confirmed", "m5_confirmation",
                elder_id=occurrence["elder_id"], plan_id=occurrence["plan_id"],
                occurrence_id=occurrence["occurrence_id"], payload=assessment_payload,
                occurred_at=assessed_at, trace_id=trace_id,
            )
            self._audit_and_enqueue(
                connection, confirmed_event,
                "confirmation_confirmed:%s" % assessment_id,
            )
        if policy["conflict_detected"]:
            conflict_event = self._event(
                "medication.confirmation.conflict_detected", "m5_confirmation",
                elder_id=occurrence["elder_id"], plan_id=occurrence["plan_id"],
                occurrence_id=occurrence["occurrence_id"], payload=assessment_payload,
                occurred_at=assessed_at, trace_id=trace_id,
            )
            self._audit_and_enqueue(
                connection, conflict_event,
                "confirmation_conflict:%s" % assessment_id,
            )
        if policy["review_required"]:
            review_event = self._event(
                "medication.confirmation.review_required", "m5_confirmation",
                elder_id=occurrence["elder_id"], plan_id=occurrence["plan_id"],
                occurrence_id=occurrence["occurrence_id"], payload=assessment_payload,
                occurred_at=assessed_at, trace_id=trace_id,
            )
            self._audit_and_enqueue(
                connection, review_event,
                "confirmation_review:%s" % assessment_id,
            )
            manual_review = self._event(
                "manual_review.request", "m5_confirmation",
                elder_id=occurrence["elder_id"], plan_id=occurrence["plan_id"],
                occurrence_id=occurrence["occurrence_id"], payload=dict(
                    assessment_payload,
                    review_reason=(
                        "EXCESS_REMOVAL_SUSPECTED"
                        if policy["has_excess_removal"] else "EVIDENCE_CONFLICT"
                    ),
                    m6_action="MANUAL_REVIEW",
                ),
                occurred_at=assessed_at, trace_id=trace_id,
            )
            self._audit_and_enqueue(
                connection, manual_review,
                "m5_manual_review:%s" % assessment_id,
            )
        self._apply_confirmation_assessment_in_transaction(
            connection, occurrence, policy, assessment_id, rows, assessment_late,
            assessed_at, trace_id,
        )
        return connection.execute(
            "SELECT * FROM medication_confirmation_assessment WHERE assessment_id=?",
            (assessment_id,),
        ).fetchone()

    def _apply_confirmation_assessment_in_transaction(self, connection, occurrence,
                                                       policy, assessment_id, evidence_rows,
                                                       late, assessed_at, trace_id):
        if policy["result"] != "CONFIRMED":
            return
        current = connection.execute(
            "SELECT * FROM medication_occurrence WHERE occurrence_id=?",
            (occurrence["occurrence_id"],),
        ).fetchone()
        strong = [
            row for row in evidence_rows
            if row["evidence_type"] in TAKEN_EVIDENCE_TYPES
        ]
        if not strong:
            return
        chosen = sorted(strong, key=lambda row: (row["observed_at"], row["created_at"]))[-1]
        chosen_value = json.loads(chosen["value_json"])
        method = {
            "SELF_REPORTED_TAKEN": "voice",
            "BUTTON_CONFIRMED": "button",
            "MANUAL_REPORTED_TAKEN": "manual",
        }.get(chosen["evidence_type"], "m5")
        if current["intake_status"] == UNCONFIRMED:
            cursor = connection.execute(
                """UPDATE medication_occurrence
                   SET intake_status='confirmed_taken', actual_time=?,
                       confirmation_method=?, reminder_claimed_at=NULL, updated_at=?
                   WHERE occurrence_id=? AND intake_status='unconfirmed'""",
                (chosen["observed_at"], method, iso(assessed_at), occurrence["occurrence_id"]),
            )
            if cursor.rowcount != 1:
                raise DomainError("occurrence state changed concurrently", 409)
            connection.execute(
                """UPDATE medication_interaction SET status='closed', updated_at=?
                   WHERE occurrence_id=? AND status='open'""",
                (iso(assessed_at), occurrence["occurrence_id"]),
            )
            payload = {
                "occurrence_id": occurrence["occurrence_id"],
                "assessment_id": assessment_id,
                "status": "confirmed_taken",
                "actual_time": chosen["observed_at"],
                "confirmation_method": method,
                "basis": policy["basis"],
                "evidence_ids": policy["evidence_ids"],
                "late": False,
                "trace_id": trace_id,
            }
            event = self._event(
                "medication.intake.updated", "m5_confirmation",
                elder_id=occurrence["elder_id"], plan_id=occurrence["plan_id"],
                occurrence_id=occurrence["occurrence_id"], payload=payload,
                occurred_at=parse_datetime(chosen["observed_at"]), trace_id=trace_id,
            )
            self._audit_and_enqueue(
                connection, event,
                "m5_intake_confirmed:%s" % assessment_id,
            )
        elif current["intake_status"] == "closed_unconfirmed" and late:
            late_source = str(
                chosen_value.get("late_verified_source")
                or chosen["actor_role"]
                or chosen["source_type"]
            ).strip()
            late_note = str(chosen_value.get("late_verified_note") or "").strip() or None
            cursor = connection.execute(
                """UPDATE medication_occurrence
                   SET late_verified_taken_at=?, late_verified_by=?,
                       late_verified_source=?, late_verified_note=?, updated_at=?
                   WHERE occurrence_id=? AND intake_status='closed_unconfirmed'""",
                (
                    chosen["observed_at"], chosen["actor_id"], late_source, late_note,
                    iso(assessed_at), occurrence["occurrence_id"],
                ),
            )
            if cursor.rowcount != 1:
                raise DomainError("occurrence state changed concurrently", 409)
            payload = {
                "occurrence_id": occurrence["occurrence_id"],
                "assessment_id": assessment_id,
                "status": "closed_unconfirmed",
                "late": True,
                "late_verified_taken": True,
                "late_verified_taken_at": chosen["observed_at"],
                "late_verified_by": chosen["actor_id"],
                "late_verified_source": late_source,
                "late_verified_note": late_note,
                "basis": policy["basis"],
                "evidence_ids": policy["evidence_ids"],
                "trace_id": trace_id,
            }
            event = self._event(
                "medication.intake.late_verified", "m5_confirmation",
                elder_id=occurrence["elder_id"], plan_id=occurrence["plan_id"],
                occurrence_id=occurrence["occurrence_id"], payload=payload,
                occurred_at=parse_datetime(chosen["observed_at"]), trace_id=trace_id,
            )
            self._audit_and_enqueue(
                connection, event,
                "m5_late_verified:%s" % assessment_id,
            )

    def record_evidence(self, data=None):
        """Record one immutable Evidence and run M5 when it is in-window."""

        data = dict(data or {})
        if not str(data.get("event_id") or "").strip():
            raise DomainError("event_id is required for medication evidence")
        if not str(data.get("elder_id") or "").strip():
            raise DomainError("elder_id is required for medication evidence")
        with self.storage.transaction() as connection:
            result = self._record_evidence_in_transaction(connection, data)
        evidence = self._hydrate_evidence(result["evidence"])
        assessment = self._hydrate_assessment(result["assessment"])
        occurrence = self.get_occurrence(result["occurrence_id"])
        return {
            "duplicate": result["duplicate"],
            "evidence": evidence,
            "assessment": assessment,
            "occurrence": occurrence,
            "late": bool(result["late"]),
        }

    # Explicit alias for callers that prefer the domain name.
    record_medication_evidence = record_evidence

    def list_evidence(self, occurrence_id):
        self.get_occurrence(occurrence_id)
        rows = self.storage.fetchall(
            "SELECT * FROM medication_evidence WHERE occurrence_id=? ORDER BY observed_at, created_at, evidence_id",
            (occurrence_id,),
        )
        return [self._hydrate_evidence(row) for row in rows]

    def list_confirmation_assessments(self, occurrence_id):
        self.get_occurrence(occurrence_id)
        rows = self.storage.fetchall(
            """SELECT * FROM medication_confirmation_assessment
               WHERE occurrence_id=? ORDER BY assessed_at, created_at, assessment_id""",
            (occurrence_id,),
        )
        return [self._hydrate_assessment(row) for row in rows]

    def get_confirmation(self, occurrence_id):
        occurrence = self.get_occurrence(occurrence_id)
        evidence = self.list_evidence(occurrence_id)
        history = self.list_confirmation_assessments(occurrence_id)
        latest = history[-1] if history else None
        return {
            "occurrence_id": occurrence_id,
            "intake_status": occurrence["intake_status"],
            "confirmation_basis": latest.get("basis") if latest else None,
            "evidence_count": len(evidence),
            "evidence_conflict": bool(latest and latest.get("conflict_detected")),
            "review_required": bool(latest and latest.get("review_required")),
            "latest_assessment": latest,
            "assessment_history": history,
            "evidence": evidence,
            "policy_version": self.confirmation_policy_version,
            "policy_fingerprint": self.confirmation_policy_fingerprint,
        }

    # ---------- M2 safety check and audit persistence ----------

    def _latest_safety_check_in_transaction(self, connection, plan_id, version=None):
        if version is None:
            row = connection.execute(
                """SELECT * FROM medication_safety_check
                   WHERE plan_id=?
                   ORDER BY plan_version DESC, checked_at DESC, created_at DESC, rowid DESC
                   LIMIT 1""",
                (plan_id,),
            ).fetchone()
        else:
            row = connection.execute(
                """SELECT * FROM medication_safety_check
                   WHERE plan_id=? AND plan_version=?
                   ORDER BY checked_at DESC, created_at DESC, rowid DESC
                   LIMIT 1""",
                (plan_id, int(version)),
            ).fetchone()
        return row_dict(row)

    def _hydrate_safety_check(self, row):
        if row is None:
            return None
        item = row_dict(row)
        try:
            item["coverage"] = json.loads(item.pop("coverage_json"))
        except (TypeError, ValueError, json.JSONDecodeError):
            item["coverage"] = {}
        item["check_failed"] = bool(item.get("check_failed", 0))
        item.update(_coverage_summary(item.get("coverage") or {}))
        findings = self.storage.fetchall(
            """SELECT * FROM medication_safety_finding
               WHERE check_id=? ORDER BY created_at, finding_id""",
            (item["check_id"],),
        )
        item["findings"] = []
        for finding in findings:
            value = row_dict(finding)
            try:
                value["evidence"] = json.loads(value.pop("evidence_json"))
            except (TypeError, ValueError, json.JSONDecodeError):
                value["evidence"] = {}
            item["findings"].append(value)
        if item.get("error_message"):
            item["error"] = item["error_message"]
        item.pop("error_message", None)
        return item

    def _save_safety_check_in_transaction(self, connection, plan, result, clock=None):
        """Persist one immutable check plus its audit/outbox events."""

        result = dict(result or {})
        plan_id = str(plan["plan_id"])
        plan_version = int(plan["version"])
        previous = self._latest_safety_check_in_transaction(
            connection, plan_id, plan_version
        )
        if previous is not None:
            previous["check_failed"] = bool(previous.get("check_failed", 0))
        status = str(result.get("status") or STATUS_BLOCK).upper()
        if status == STATUS_CHECK_FAILED:
            status = STATUS_BLOCK
        if status not in (STATUS_PASS, STATUS_WARN, STATUS_BLOCK):
            status = STATUS_BLOCK
        check_id = str(result.get("check_id") or new_id("safety"))
        checked_at = result.get("checked_at") or iso(clock or now_utc())
        trace_id = str(
            result.get("trace_id")
            or "safety:%s:v%s:%s" % (plan_id, plan_version, check_id)
        )
        ruleset_version = str(
            result.get("ruleset_version") or self.safety_ruleset_version
        )
        ruleset_fingerprint = str(
            result.get("ruleset_fingerprint") or self.safety_ruleset_fingerprint
        )
        findings = []
        for raw in result.get("findings") or []:
            finding = raw.to_dict() if isinstance(raw, SafetyFinding) else dict(raw)
            finding.setdefault("finding_id", new_id("finding"))
            # Engine finding IDs are reproducible for the same input.  The
            # database ID is scoped to this immutable check so repeated checks
            # of the same version retain independent history rows.
            finding["finding_id"] = "%s:%s" % (check_id, finding["finding_id"])
            finding.setdefault("category", "system")
            finding.setdefault("severity", "BLOCK")
            finding.setdefault("code", "SAFETY_CHECK_FAILED")
            finding.setdefault("message", "safety check did not complete")
            finding.setdefault("evidence", {})
            finding.setdefault("rule_id", None)
            finding.setdefault("rule_version", ruleset_version)
            findings.append(finding)
        coverage = dict(result.get("coverage") or {})
        check_failed = bool(result.get("check_failed")) or any(
            finding["code"] == "SAFETY_CHECK_FAILED" for finding in findings
        )
        error_message = result.get("error")
        created_at = iso(clock or now_utc())
        connection.execute(
            """INSERT INTO medication_safety_check
               (check_id, plan_id, plan_version, status, ruleset_version,
                ruleset_fingerprint, coverage_json, checked_at, trace_id,
                check_failed, error_message, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                check_id, plan_id, plan_version, status, ruleset_version,
                ruleset_fingerprint, json_text(coverage), checked_at, trace_id,
                int(check_failed), str(error_message) if error_message else None,
                created_at,
            ),
        )
        for finding in findings:
            connection.execute(
                """INSERT INTO medication_safety_finding
                   (finding_id, check_id, category, severity, code, message,
                    rule_id, rule_version, evidence_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    finding["finding_id"], check_id, finding["category"],
                    finding["severity"], finding["code"], finding["message"],
                    finding.get("rule_id"), finding.get("rule_version"),
                    json_text(finding.get("evidence") or {}), created_at,
                ),
            )
        event_payload = {
            "check_id": check_id,
            "plan_id": plan_id,
            "plan_version": plan_version,
            "status": status,
            "ruleset_version": ruleset_version,
            "ruleset_fingerprint": ruleset_fingerprint,
            "timestamp": checked_at,
            "trace_id": trace_id,
            "coverage": coverage,
            "findings": findings,
        }
        occurred_at = parse_datetime(checked_at)
        checked_event = self._event(
            "medication.safety.checked",
            "m2_safety",
            elder_id=plan["elder_id"],
            plan_id=plan_id,
            payload=event_payload,
            occurred_at=occurred_at,
            trace_id=trace_id,
        )
        self._audit_and_enqueue(
            connection, checked_event,
            "safety_checked:%s" % check_id,
        )
        if status == STATUS_WARN:
            warning_event = self._event(
                "medication.safety.warning",
                "m2_safety",
                elder_id=plan["elder_id"],
                plan_id=plan_id,
                payload=event_payload,
                occurred_at=occurred_at,
                trace_id=trace_id,
            )
            self._audit_and_enqueue(
                connection, warning_event,
                "safety_warning:%s" % check_id,
            )
        if status == STATUS_BLOCK:
            previous_block_same = bool(
                previous
                and previous.get("status") == STATUS_BLOCK
                and previous.get("ruleset_version") == ruleset_version
                and previous.get("ruleset_fingerprint") == ruleset_fingerprint
                and bool(previous.get("check_failed")) == check_failed
            )
            if not previous_block_same:
                event_type = (
                    "medication.safety.check_failed" if check_failed
                    else "medication.safety.blocked"
                )
                blocked_event = self._event(
                    event_type,
                    "m2_safety",
                    elder_id=plan["elder_id"],
                    plan_id=plan_id,
                    payload=event_payload,
                    occurred_at=occurred_at,
                    trace_id=trace_id,
                )
                self._audit_and_enqueue(
                    connection, blocked_event,
                    "%s:%s" % ("safety_check_failed" if check_failed else "safety_blocked", check_id),
                )
        result.update({
            "check_id": check_id,
            "plan_id": plan_id,
            "plan_version": plan_version,
            "status": status,
            "ruleset_version": ruleset_version,
            "ruleset_fingerprint": ruleset_fingerprint,
            "checked_at": checked_at,
            "trace_id": trace_id,
            "findings": findings,
            "coverage": coverage,
            "check_failed": check_failed,
        })
        result.update(_coverage_summary(coverage))
        if error_message:
            result["error"] = str(error_message)
        return result

    def _run_safety_check_in_transaction(self, connection, plan, clock=None, trace_id=None):
        try:
            result = self.safety_engine.check(
                plan, checked_at=clock or now_utc(), trace_id=trace_id
            )
            if isinstance(result, SafetyCheckResult):
                result = result.to_dict()
            elif not isinstance(result, dict):
                raise RuntimeError("safety engine returned an unsupported result")
        except Exception as exc:  # fail closed even if the engine itself breaks
            result = {
                "status": STATUS_BLOCK,
                "ruleset_version": self.safety_ruleset_version,
                "checked_at": iso(clock or now_utc()),
                "trace_id": trace_id,
                "check_failed": True,
                "error": "%s: %s" % (type(exc).__name__, str(exc)),
                "coverage": {"safety_engine": "failed"},
                "findings": [{
                    "category": "system",
                    "severity": "BLOCK",
                    "code": "SAFETY_CHECK_FAILED",
                    "message": "safety check failed; plan cannot enter scheduling",
                    "evidence": {"error_type": type(exc).__name__},
                    "rule_id": "m2.service",
                    "rule_version": self.safety_ruleset_version,
                }],
            }
        saved = self._save_safety_check_in_transaction(connection, plan, result, clock)
        if saved.get("status") == STATUS_BLOCK:
            self._apply_safety_block_to_future_occurrences(
                connection, plan, saved, clock or now_utc()
            )
        return saved

    def _safety_check_is_current(self, plan, check):
        return bool(
            check
            and int(check.get("plan_version", -1)) == int(plan["version"])
            and check.get("ruleset_version") == self.safety_ruleset_version
            and check.get("ruleset_fingerprint") == self.safety_ruleset_fingerprint
            and check.get("status") in (STATUS_PASS, STATUS_WARN, STATUS_BLOCK)
        )

    def _ensure_plan_safety_in_transaction(self, connection, plan, clock=None):
        latest = self._latest_safety_check_in_transaction(
            connection, plan["plan_id"], plan["version"]
        )
        if latest is not None:
            latest["check_failed"] = bool(latest.get("check_failed", 0))
        if self._safety_check_is_current(plan, latest):
            if latest.get("status") == STATUS_BLOCK:
                self._apply_safety_block_to_future_occurrences(
                    connection, plan, latest, clock or now_utc()
                )
            return latest
        return self._run_safety_check_in_transaction(connection, plan, clock)

    def _attach_plan_safety(self, plan):
        item = self._normalize_plan_row(plan)
        check = self.get_latest_safety_check(item["plan_id"], item["version"])
        item["safety_status"] = check["status"] if check else "NOT_CHECKED"
        item["latest_safety_check_id"] = check["check_id"] if check else None
        item["safety_check"] = check
        return item

    def get_safety_check(self, check_id):
        row = self.storage.fetchone(
            "SELECT * FROM medication_safety_check WHERE check_id=?", (check_id,)
        )
        if row is None:
            raise DomainError("safety check not found", 404)
        return self._hydrate_safety_check(row)

    def get_latest_safety_check(self, plan_id, version=None):
        row = self.storage.fetchone(
            """SELECT * FROM medication_safety_check
               WHERE plan_id=? AND (? IS NULL OR plan_version=?)
               ORDER BY plan_version DESC, checked_at DESC, created_at DESC, rowid DESC
               LIMIT 1""",
            (plan_id, version, version),
        )
        return self._hydrate_safety_check(row)

    def list_safety_history(self, plan_id):
        rows = self.storage.fetchall(
            """SELECT * FROM medication_safety_check
               WHERE plan_id=? ORDER BY plan_version, checked_at, created_at, rowid""",
            (plan_id,),
        )
        return [self._hydrate_safety_check(row) for row in rows]

    def check_plan_safety(self, plan_id, version=None, now=None, trace_id=None):
        clock = now or now_utc()
        with self.storage.transaction() as connection:
            plan = self._select_plan(connection, plan_id, version)
            self._run_safety_check_in_transaction(connection, plan, clock, trace_id)
        return self.get_latest_safety_check(plan_id, plan["version"])

    # ---------- deterministic schedule helpers ----------

    def _schedule_domain_error(self, error, status=None):
        if isinstance(error, ScheduleError):
            details = dict(error.details)
            details.setdefault("code", error.code)
            return DomainError(error.code, status or (
                409 if error.code == "SCHEDULE_CONTEXT_MISSING" else 422
            ), details)
        return error

    def _normalize_plan_row(self, row):
        item = dict(row or {})
        raw_config = item.get("schedule_config_json")
        config = None
        if raw_config:
            try:
                config = json.loads(raw_config) if isinstance(raw_config, str) else raw_config
            except (TypeError, ValueError, json.JSONDecodeError):
                config = None
        if not isinstance(config, dict):
            config = {}
        try:
            schedule_type, config = ScheduleValidator.validate(
                item.get("schedule_type"), config,
                schedule_time=item.get("schedule_time") or item.get("time"),
            )
        except ScheduleError as exc:
            legacy_type = str(item.get("schedule_type") or "").strip().upper()
            if raw_config or legacy_type not in ("", "DAILY", "FIXED_TIME"):
                raise self._schedule_domain_error(exc) from exc
            # A pre-Phase-4 row may have schedule_type=daily and only
            # schedule_time.  Its legacy value remains the sole source of
            # truth; do not invent another daily time.
            schedule_type, config = ScheduleValidator.validate(
                ScheduleType.FIXED_TIME,
                {"times": [item.get("schedule_time") or item.get("time")]},
            )
        item["schedule_type"] = schedule_type
        item["schedule_config"] = config
        item["schedule"] = dict(config)
        item["schedule_snapshot"] = {"type": schedule_type, **dict(config)}
        if schedule_type == ScheduleType.FIXED_TIME and config.get("times"):
            item["schedule_time"] = config["times"][0]
        return item

    def _schedule_bundle(self, plan):
        normalized = self._normalize_plan_row(plan)
        return normalized["schedule_type"], normalized["schedule_config"]

    def _routine_in_transaction(self, connection, elder_id):
        row = connection.execute(
            "SELECT * FROM elder_routine WHERE elder_id=?", (elder_id,)
        ).fetchone()
        return ElderRoutine.from_mapping(dict(row)) if row else None

    def _assert_schedule_ready_in_transaction(self, connection, plan):
        plan = self._normalize_plan_row(plan)
        try:
            schedule_type, config = self._schedule_bundle(plan)
            if schedule_type == ScheduleType.PRN:
                return None
            routine = None
            if schedule_type in (
                ScheduleType.MEAL_RELATION, ScheduleType.ROUTINE_RELATION
            ):
                routine = self._routine_in_transaction(connection, plan["elder_id"])
            self.schedule_expander.expand(
                plan=dict(plan, schedule_type=schedule_type,
                          schedule_config=config),
                start=plan["start_date"],
                end=plan["start_date"],
                routine=routine,
            )
            return routine
        except ScheduleError as exc:
            raise self._schedule_domain_error(exc) from exc

    def _routine_for_plan_in_transaction(self, connection, plan):
        schedule_type, _config = self._schedule_bundle(plan)
        if schedule_type in (
            ScheduleType.MEAL_RELATION, ScheduleType.ROUTINE_RELATION
        ):
            return self._routine_in_transaction(connection, plan["elder_id"])
        return None

    def _occurrence_identity(self, plan, scheduled_at):
        identity = "%s|%s|%s" % (
            plan["plan_id"], int(plan["version"]), iso(scheduled_at)
        )
        return "occ_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]

    def _schedule_source_snapshot(self, spec):
        return json_text({
            "type": spec.schedule_type,
            "config": spec.schedule_snapshot,
            "timezone": spec.timezone,
            "source": spec.schedule_source,
        })

    def _expand_plan_specs_in_transaction(self, connection, plan, clock,
                                          horizon_days=None):
        plan = self._normalize_plan_row(plan)
        routine = self._routine_for_plan_in_transaction(connection, plan)
        horizon = int(horizon_days or self.config["occurrence_window_days"])
        window_start = clock.astimezone(SHANGHAI)
        plan_start = datetime.combine(
            parse_date(plan["start_date"], "start_date"), time.min, tzinfo=SHANGHAI
        )
        window_start = max(window_start, plan_start)
        window_end = window_start + timedelta(days=horizon)
        try:
            return self.schedule_expander.expand(
                plan=plan,
                start=window_start,
                end=window_end,
                routine=routine,
            )
        except ScheduleError as exc:
            raise self._schedule_domain_error(exc) from exc

    # ---------- plan lifecycle ----------

    def _validate_plan_fields(self, data, defaults=None):
        defaults = defaults or {}
        incoming = dict(data or {})
        merged = dict(defaults)
        merged.update(incoming)
        # A revision may use either the new object form or the legacy
        # schedule_time field.  Do not let the inherited canonical config
        # silently win over an explicit schedule change.
        schedule_fields = {
            "schedule_type", "schedule", "schedule_config", "schedule_time", "time",
            "times", "meal", "relation", "offset_minutes", "interval_hours", "hours",
            "anchor_at", "weekdays", "cycle_start_date", "days_on", "days_off",
            "condition_text", "anchor",
        }
        if "schedule_config" not in incoming and (
            "schedule" in incoming or schedule_fields.intersection(incoming)
        ):
            merged.pop("schedule_config", None)
            if "schedule" not in incoming:
                merged.pop("schedule", None)
        required = ("elder_id", "start_date")
        for field in required:
            if not str(merged.get(field, "")).strip():
                raise DomainError("missing required field: %s" % field)
        if merged.get("timezone", SUPPORTED_TIMEZONE) != SUPPORTED_TIMEZONE:
            raise DomainError("MVP only supports timezone=%s" % SUPPORTED_TIMEZONE)
        try:
            schedule_type, schedule_config = ScheduleValidator.validate_plan(merged)
        except ScheduleError as exc:
            raise self._schedule_domain_error(exc) from exc
        start = parse_date(merged["start_date"], "start_date")
        end_value = merged.get("end_date")
        end = parse_date(end_value, "end_date") if end_value else None
        if end and end < start:
            raise DomainError("end_date must not be before start_date")
        try:
            confirmation_window = int(
                merged.get(
                    "confirmation_window_minutes",
                    self.config["default_confirmation_window_minutes"],
                )
            )
            max_snooze_count = int(
                merged.get("max_snooze_count", self.config["default_max_snooze_count"])
            )
        except (TypeError, ValueError) as exc:
            raise DomainError(
                "confirmation_window_minutes/max_snooze_count must be integers"
            ) from exc
        if confirmation_window <= 0 or max_snooze_count < 0:
            raise DomainError("invalid confirmation or snooze configuration")

        schedule_time = ""
        if schedule_config.get("times"):
            schedule_time = schedule_config["times"][0]
        elif schedule_type == ScheduleType.INTERVAL:
            anchor = schedule_config.get("anchor_at", "")
            if re.fullmatch(r"\d{2}:\d{2}", str(anchor)):
                schedule_time = anchor
            elif anchor:
                try:
                    schedule_time = parse_datetime(anchor).astimezone(SHANGHAI).strftime("%H:%M")
                except DomainError:
                    schedule_time = ""

        return {
            "elder_id": str(merged["elder_id"]).strip(),
            "drug_name": str(merged.get("drug_name") or "").strip(),
            "dosage_text": str(merged.get("dosage_text") or "").strip(),
            "route": str(merged.get("route", "oral")).strip() or "oral",
            "schedule_type": schedule_type,
            "schedule_time": schedule_time,
            "timezone": SUPPORTED_TIMEZONE,
            "relation_to_meal": merged.get("relation_to_meal"),
            "schedule_config": schedule_config,
            "start_date": start.isoformat(),
            "end_date": end.isoformat() if end else None,
            "source": str(merged.get("source", "management_api")),
            "created_by": str(merged.get("created_by", "unknown")),
            "device_sn": merged.get("device_sn"),
            "confirmation_window_minutes": confirmation_window,
            "max_snooze_count": max_snooze_count,
        }

    def create_draft(self, data):
        plan = self._validate_plan_fields(data)
        plan_id = str(data.get("plan_id") or new_id("plan"))
        current = iso(now_utc())
        with self.storage.transaction() as connection:
            connection.execute(
                """INSERT INTO medication_plan
                   (plan_id, version, elder_id, drug_name, dosage_text, route,
                    schedule_type, schedule_time, timezone, relation_to_meal,
                    schedule_config_json, start_date, end_date, status, source,
                    created_by, device_sn, confirmation_window_minutes,
                    max_snooze_count, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    plan_id, 1, plan["elder_id"], plan["drug_name"], plan["dosage_text"],
                    plan["route"], plan["schedule_type"], plan["schedule_time"],
                    plan["timezone"], plan["relation_to_meal"],
                    json_text(plan["schedule_config"]), plan["start_date"],
                    plan["end_date"], "draft", plan["source"], plan["created_by"],
                    plan["device_sn"], plan["confirmation_window_minutes"],
                    plan["max_snooze_count"], current, current,
                ),
            )
            validation_event = self._event(
                "medication.schedule.validated", "medication_service",
                elder_id=plan["elder_id"], plan_id=plan_id,
                payload={
                    "plan_id": plan_id,
                    "plan_version": 1,
                    "schedule_type": plan["schedule_type"],
                    "schedule_config": plan["schedule_config"],
                },
            )
            self._audit_and_enqueue(
                connection, validation_event,
                "schedule_validated:%s:1" % plan_id,
            )
            event = self._event(
                "medication.plan.created", "medication_service",
                elder_id=plan["elder_id"], plan_id=plan_id,
                payload={"plan_id": plan_id, "plan_version": 1, "status": "draft"},
            )
            self._audit_and_enqueue(connection, event, "plan_created:%s:1" % plan_id)
        return self.get_plan(plan_id, 1)

    def submit_plan(self, plan_id, version=None):
        with self.storage.transaction() as connection:
            plan = self._select_plan(connection, plan_id, version)
            if plan["status"] != "draft":
                raise DomainError("only draft plans can be submitted", 409)
            timestamp = iso(now_utc())
            connection.execute(
                "UPDATE medication_plan SET status='pending_confirmation', updated_at=? "
                "WHERE plan_id=? AND version=?",
                (timestamp, plan_id, plan["version"]),
            )
            event = self._event(
                "medication.plan.pending_confirmation", "medication_service",
                elder_id=plan["elder_id"], plan_id=plan_id,
                payload={"plan_id": plan_id, "plan_version": plan["version"]},
            )
            self._audit_and_enqueue(connection, event)
        return self.get_plan(plan_id, plan["version"])

    def _assert_activation_safety_in_transaction(self, connection, plan, safety):
        """Invariant for every internal transition into ``active``."""

        if safety is not None:
            safety["check_failed"] = bool(safety.get("check_failed", 0))
        if (
            not self._safety_check_is_current(plan, safety)
            or safety.get("status") not in (STATUS_PASS, STATUS_WARN)
            or safety.get("check_failed")
        ):
            raise DomainError(
                "plan activation requires a current PASS/WARN safety check",
                409,
                {
                    "plan_id": plan["plan_id"],
                    "plan_version": plan["version"],
                    "safety_check_id": safety.get("check_id") if safety else None,
                    "status": safety.get("status") if safety else "NOT_CHECKED",
                },
            )

    def approve_plan(self, plan_id, approved_by, version=None, now=None):
        if not str(approved_by or "").strip():
            raise DomainError("approved_by is required")
        clock = now or now_utc()
        blocked_result = None
        with self.storage.transaction() as connection:
            plan = self._select_plan(connection, plan_id, version)
            # Schedule context is a domain prerequisite.  It is checked before
            # the plan can enter M2, so missing breakfast/bedtime is never
            # misreported as a clinical Safety BLOCK.
            self._assert_schedule_ready_in_transaction(connection, plan)
            if plan["status"] == "active":
                # Approval is idempotent after activation.  Re-check only when
                # the active version has become stale under a new ruleset.
                safety = self._ensure_plan_safety_in_transaction(connection, plan, clock)
                if safety.get("status") == STATUS_BLOCK:
                    blocked_result = safety
            else:
                if plan["status"] == "draft":
                    timestamp = iso(clock)
                    connection.execute(
                        "UPDATE medication_plan SET status='pending_confirmation', updated_at=? "
                        "WHERE plan_id=? AND version=?",
                        (timestamp, plan_id, plan["version"]),
                    )
                    submit_event = self._event(
                        "medication.plan.pending_confirmation", "medication_service",
                        elder_id=plan["elder_id"], plan_id=plan_id,
                        payload={"plan_id": plan_id, "plan_version": plan["version"]},
                        occurred_at=clock,
                    )
                    self._audit_and_enqueue(connection, submit_event)
                    plan = dict(plan)
                    plan["status"] = "pending_confirmation"
                if plan["status"] != "pending_confirmation":
                    raise DomainError("only pending_confirmation plans can be approved", 409)
                safety = self._run_safety_check_in_transaction(connection, plan, clock)
                if safety.get("status") == STATUS_BLOCK:
                    blocked_result = safety
                else:
                    self._assert_activation_safety_in_transaction(connection, plan, safety)
                    timestamp = iso(clock)
                    cursor = connection.execute(
                        """UPDATE medication_plan
                           SET status='active', approved_by=?, approved_at=?, effective_from=?, updated_at=?
                           WHERE plan_id=? AND version=? AND status='pending_confirmation'""",
                        (str(approved_by), timestamp, timestamp, timestamp, plan_id, plan["version"]),
                    )
                    if cursor.rowcount != 1:
                        raise DomainError("plan state changed concurrently", 409)
                    if plan["version"] > 1:
                        connection.execute(
                            """UPDATE medication_plan SET status='completed', updated_at=?
                               WHERE plan_id=? AND version < ? AND status='active'""",
                            (timestamp, plan_id, plan["version"]),
                        )
                        self._cancel_future_occurrences_in_transaction(
                            connection, plan_id, plan["version"] - 1, clock,
                            "replaced_by_plan_version_%s" % plan["version"],
                        )
                    approved = dict(plan)
                    approved.update({
                        "status": "active",
                        "approved_by": str(approved_by),
                        "approved_at": timestamp,
                        "effective_from": timestamp,
                        "updated_at": timestamp,
                    })
                    self._ensure_occurrences_in_transaction(connection, approved, clock)
                    event = self._event(
                        "medication.plan.approved", "medication_service",
                        elder_id=plan["elder_id"], plan_id=plan_id,
                        payload={
                            "plan_id": plan_id,
                            "plan_version": plan["version"],
                            "approved_by": str(approved_by),
                            "safety_check_id": safety["check_id"],
                            "safety_status": safety["status"],
                            "ruleset_version": safety["ruleset_version"],
                            "ruleset_fingerprint": safety["ruleset_fingerprint"],
                        },
                        occurred_at=clock,
                    )
                    self._audit_and_enqueue(connection, event)
        if blocked_result is not None:
            message = "SAFETY_CHECK_FAILED" if blocked_result.get("check_failed") else "SAFETY_BLOCKED"
            raise DomainError(
                message,
                409,
                {
                    "check_id": blocked_result.get("check_id"),
                    "plan_id": plan_id,
                    "plan_version": int(plan["version"]),
                    "status": blocked_result.get("status", STATUS_BLOCK),
                    "ruleset_version": blocked_result.get("ruleset_version"),
                    "findings": blocked_result.get("findings", []),
                    "coverage": blocked_result.get("coverage", {}),
                },
            )
        response = self.get_plan(plan_id, plan["version"])
        safety = response.get("safety_check") or self.get_latest_safety_check(
            plan_id, plan["version"]
        )
        response["safety"] = safety
        response["safety_check"] = safety
        response["plan"] = dict(response)
        return response

    def revise_plan(self, plan_id, data):
        with self.storage.transaction() as connection:
            active = self._select_active_plan(connection, plan_id)
            plan = self._validate_plan_fields(data or {}, defaults=active)
            version = int(active["version"]) + 1
            timestamp = iso(now_utc())
            connection.execute(
                """INSERT INTO medication_plan
                   (plan_id, version, elder_id, drug_name, dosage_text, route,
                    schedule_type, schedule_time, timezone, relation_to_meal,
                    schedule_config_json, start_date, end_date, status, source,
                    created_by, device_sn, confirmation_window_minutes,
                    max_snooze_count, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    plan_id, version, plan["elder_id"], plan["drug_name"],
                    plan["dosage_text"], plan["route"], plan["schedule_type"],
                    plan["schedule_time"], plan["timezone"], plan["relation_to_meal"],
                    json_text(plan["schedule_config"]), plan["start_date"],
                    plan["end_date"], "draft", plan["source"], plan["created_by"],
                    plan["device_sn"], plan["confirmation_window_minutes"],
                    plan["max_snooze_count"], timestamp, timestamp,
                ),
            )
            validation_event = self._event(
                "medication.schedule.validated", "medication_service",
                elder_id=plan["elder_id"], plan_id=plan_id,
                payload={
                    "plan_id": plan_id,
                    "plan_version": version,
                    "schedule_type": plan["schedule_type"],
                    "schedule_config": plan["schedule_config"],
                    "previous_version": active["version"],
                },
            )
            self._audit_and_enqueue(
                connection, validation_event,
                "schedule_validated:%s:%s" % (plan_id, version),
            )
            event = self._event(
                "medication.plan.version.created", "medication_service",
                elder_id=plan["elder_id"], plan_id=plan_id,
                payload={"plan_id": plan_id, "plan_version": version,
                         "previous_version": active["version"]},
            )
            self._audit_and_enqueue(connection, event)
        return self.get_plan(plan_id, version)

    def update_plan(self, plan_id, data):
        """Reject a direct row update; active changes must create a revision."""

        with self.storage.transaction() as connection:
            plan = self._select_plan(connection, plan_id)
            try:
                self.safety_freeze.assert_plan_write_allowed(
                    plan["status"], data or {}, operation="update_plan"
                )
            except SafetyFreezeError as exc:
                raise DomainError(str(exc), 409) from exc
            raise DomainError(
                "direct plan updates are not supported; use revise_plan", 409
            )

    def pause_plan(self, plan_id, version=None, now=None):
        clock = now or now_utc()
        with self.storage.transaction() as connection:
            plan = self._select_active_plan(connection, plan_id, version)
            timestamp = iso(clock)
            connection.execute(
                "UPDATE medication_plan SET status='paused', updated_at=? "
                "WHERE plan_id=? AND version=? AND status='active'",
                (timestamp, plan_id, plan["version"]),
            )
            self._cancel_future_occurrences_in_transaction(
                connection, plan_id, plan["version"], clock, "plan_paused"
            )
            event = self._event(
                "medication.plan.paused", "medication_service",
                elder_id=plan["elder_id"], plan_id=plan_id,
                payload={"plan_id": plan_id, "plan_version": plan["version"]},
                occurred_at=clock,
            )
            self._audit_and_enqueue(connection, event)
        return self.get_plan(plan_id, plan["version"])

    # ---------- explicit elder routine context ----------

    def get_routine(self, elder_id):
        if not str(elder_id or "").strip():
            raise DomainError("elder_id is required")
        row = self.storage.fetchone(
            "SELECT * FROM elder_routine WHERE elder_id=?", (elder_id,)
        )
        return dict(row) if row else None

    def get_elder_routine(self, elder_id):
        return self.get_routine(elder_id)

    @staticmethod
    def _routine_anchor_changed(old_routine, new_routine, plan):
        if old_routine is None:
            return True
        schedule_type = plan.get("schedule_type")
        config = plan.get("schedule_config") or {}
        if schedule_type == ScheduleType.MEAL_RELATION:
            field = {
                "BREAKFAST": "breakfast_time",
                "LUNCH": "lunch_time",
                "DINNER": "dinner_time",
            }.get(str(config.get("meal") or "").upper())
        elif schedule_type == ScheduleType.ROUTINE_RELATION:
            field = "bedtime"
        else:
            return False
        return bool(field and getattr(old_routine, field) != getattr(new_routine, field))

    def _recalculate_active_plan_in_transaction(self, connection, plan, clock,
                                                reason="schedule_recalculated"):
        plan = self._normalize_plan_row(plan)
        self._assert_schedule_ready_in_transaction(connection, plan)
        safety = self._run_safety_check_in_transaction(connection, plan, clock)
        cancelled = []
        inserted = 0
        if safety.get("status") != STATUS_BLOCK:
            cancelled = self._cancel_future_occurrences_in_transaction(
                connection, plan["plan_id"], plan["version"], clock, reason
            )
            inserted = self._ensure_occurrences_in_transaction(
                connection, plan, clock, allow_reactivation=True
            )
        event = self._event(
            "medication.schedule.recalculated", "medication_service",
            elder_id=plan["elder_id"], plan_id=plan["plan_id"],
            payload={
                "plan_id": plan["plan_id"],
                "plan_version": plan["version"],
                "schedule_type": plan["schedule_type"],
                "schedule_config": plan["schedule_config"],
                "reason": reason,
                "safety_status": safety.get("status"),
                "cancelled_occurrence_ids": cancelled,
                "occurrences_created": inserted,
            },
            occurred_at=clock,
        )
        self._audit_and_enqueue(
            connection, event,
            "schedule_recalculated:%s:%s:%s" % (
                plan["plan_id"], plan["version"], reason
            ),
        )
        return {
            "plan_id": plan["plan_id"],
            "plan_version": plan["version"],
            "safety_status": safety.get("status"),
            "cancelled_occurrence_ids": cancelled,
            "occurrences_created": inserted,
        }

    def recalculate_plan_schedule(self, plan_id, version=None, now=None):
        clock = now or now_utc()
        if isinstance(clock, str):
            clock = parse_datetime(clock)
        with self.storage.transaction() as connection:
            plan = self._select_active_plan(connection, plan_id, version)
            result = self._recalculate_active_plan_in_transaction(
                connection, plan, clock
            )
        result["plan"] = self.get_plan(plan_id, plan["version"])
        return result

    def recalculate_schedule(self, plan_id, version=None, now=None):
        return self.recalculate_plan_schedule(plan_id, version, now)

    def preview_schedule(self, plan_id, version=None, horizon_days=None, now=None):
        clock = now or now_utc()
        if isinstance(clock, str):
            clock = parse_datetime(clock)
        horizon = int(horizon_days or self.config["occurrence_window_days"])
        if horizon <= 0 or horizon > 366:
            raise DomainError("horizon_days must be between 1 and 366")
        plan = self.get_plan(plan_id, version)
        routine = self.get_routine(plan["elder_id"])
        plan_start = datetime.combine(
            parse_date(plan["start_date"], "start_date"), time.min, tzinfo=SHANGHAI
        )
        window_start = max(clock.astimezone(SHANGHAI), plan_start)
        try:
            specs = self.schedule_expander.expand(
                plan=plan,
                start=window_start,
                end=window_start + timedelta(days=horizon),
                routine=routine,
            )
        except ScheduleError as exc:
            raise self._schedule_domain_error(exc) from exc
        items = [
            {
                "scheduled_at": iso(spec.scheduled_at),
                "scheduled_at_local": spec.scheduled_at.isoformat(),
                "schedule_type": spec.schedule_type,
                "schedule_snapshot": dict(spec.schedule_snapshot),
                "schedule_source": spec.schedule_source,
                "timezone": spec.timezone,
            }
            for spec in specs
        ]
        return {
            "plan_id": plan_id,
            "plan_version": plan["version"],
            "horizon_days": horizon,
            "schedule_type": plan["schedule_type"],
            "schedule_config": plan["schedule_config"],
            "items": items,
            "occurrences": items,
            "persisted": False,
        }

    def preview_plan_schedule(self, plan_id, version=None, horizon_days=None, now=None):
        return self.preview_schedule(plan_id, version, horizon_days, now)

    def update_routine(self, elder_id, data=None, now=None):
        if not str(elder_id or "").strip():
            raise DomainError("elder_id is required")
        clock = now or now_utc()
        if isinstance(clock, str):
            clock = parse_datetime(clock)
        data = dict(data or {})
        routine_fields = (
            "breakfast_time", "lunch_time", "dinner_time", "bedtime", "timezone",
        )
        with self.storage.transaction() as connection:
            previous_row = connection.execute(
                "SELECT * FROM elder_routine WHERE elder_id=?", (elder_id,)
            ).fetchone()
            previous = ElderRoutine.from_mapping(dict(previous_row)) if previous_row else None
            merged = previous.to_dict() if previous else {"elder_id": elder_id}
            # Version and timestamps are domain-owned.  A PUT can change only
            # explicit routine values; it cannot forge a historical version.
            for field_name in routine_fields:
                if field_name in data:
                    merged[field_name] = data[field_name]
            merged["elder_id"] = elder_id
            try:
                candidate = ElderRoutine.from_mapping(merged)
            except RoutineError as exc:
                raise DomainError("SCHEDULE_INVALID", 422, {"message": str(exc)}) from exc

            changed = previous is None or any(
                getattr(previous, field_name) != getattr(candidate, field_name)
                for field_name in routine_fields
            )
            if not changed:
                # A semantically identical PUT must not advance version/time,
                # emit an event, or start another schedule recalculation.
                return {
                    "routine": previous.to_dict(),
                    "recalculated": [],
                }

            timestamp = iso(clock)
            routine = ElderRoutine(
                elder_id=elder_id,
                breakfast_time=candidate.breakfast_time,
                lunch_time=candidate.lunch_time,
                dinner_time=candidate.dinner_time,
                bedtime=candidate.bedtime,
                timezone=candidate.timezone,
                routine_version=(previous.routine_version + 1) if previous else 1,
                updated_at=timestamp,
            )

            rows = connection.execute(
                "SELECT * FROM medication_plan WHERE elder_id=? AND status='active'",
                (elder_id,),
            ).fetchall()
            affected_plans = []
            for row in rows:
                plan = self._normalize_plan_row(row_dict(row))
                if self._routine_anchor_changed(previous, routine, plan):
                    affected_plans.append(plan)

            connection.execute(
                """INSERT INTO elder_routine
                   (elder_id, breakfast_time, lunch_time, dinner_time, bedtime,
                    timezone, routine_version, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(elder_id) DO UPDATE SET
                       breakfast_time=excluded.breakfast_time,
                       lunch_time=excluded.lunch_time,
                       dinner_time=excluded.dinner_time,
                       bedtime=excluded.bedtime,
                       timezone=excluded.timezone,
                       routine_version=excluded.routine_version,
                       updated_at=excluded.updated_at""",
                (
                    elder_id, routine.breakfast_time, routine.lunch_time,
                    routine.dinner_time, routine.bedtime, routine.timezone,
                    routine.routine_version, timestamp,
                ),
            )
            routine_payload = {
                "elder_id": elder_id,
                "routine": routine.to_dict(),
                "previous_routine": previous.to_dict() if previous else None,
                "old_routine_version": previous.routine_version if previous else None,
                "new_routine_version": routine.routine_version,
                "affected_plan_ids": [plan["plan_id"] for plan in affected_plans],
            }
            routine_event = self._event(
                "medication.routine.updated", "medication_service",
                elder_id=elder_id, payload=routine_payload, occurred_at=clock,
            )
            self._audit_and_enqueue(
                connection, routine_event,
                "routine_updated:%s:%s" % (elder_id, routine.routine_version),
            )
            recalculated = []
            for plan in affected_plans:
                recalculated.append(
                    self._recalculate_active_plan_in_transaction(
                        connection, plan, clock, reason="routine_changed"
                    )
                )
        return {"routine": self.get_routine(elder_id), "recalculated": recalculated}

    def upsert_routine(self, elder_id, data=None, now=None):
        return self.update_routine(elder_id, data, now)

    def update_elder_routine(self, elder_id, data=None, now=None):
        return self.update_routine(elder_id, data, now)

    def list_plans(self, elder_id=None):
        if elder_id:
            rows = self.storage.fetchall(
                "SELECT * FROM medication_plan WHERE elder_id=? ORDER BY plan_id, version",
                (elder_id,),
            )
        else:
            rows = self.storage.fetchall(
                "SELECT * FROM medication_plan ORDER BY elder_id, plan_id, version"
            )
        return [self._attach_plan_safety(row_dict(row)) for row in rows]

    def list_plans_by_id(self, plan_id):
        rows = self.storage.fetchall(
            "SELECT * FROM medication_plan WHERE plan_id=? ORDER BY version",
            (plan_id,),
        )
        if not rows:
            raise DomainError("plan not found", 404)
        return [self._attach_plan_safety(row_dict(row)) for row in rows]

    def get_plan(self, plan_id, version=None):
        row = self.storage.fetchone(
            "SELECT * FROM medication_plan WHERE plan_id=? AND (? IS NULL OR version=?)",
            (plan_id, version, version),
        )
        if row is None:
            raise DomainError("plan not found", 404)
        return self._attach_plan_safety(row_dict(row))

    def _select_plan(self, connection, plan_id, version=None):
        if version is None:
            row = connection.execute(
                "SELECT * FROM medication_plan WHERE plan_id=? ORDER BY version DESC LIMIT 1",
                (plan_id,),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM medication_plan WHERE plan_id=? AND version=?",
                (plan_id, int(version)),
            ).fetchone()
        if row is None:
            raise DomainError("plan not found", 404)
        return self._normalize_plan_row(row_dict(row))

    def _select_active_plan(self, connection, plan_id, version=None):
        if version is None:
            row = connection.execute(
                "SELECT * FROM medication_plan WHERE plan_id=? AND status='active' "
                "ORDER BY version DESC LIMIT 1", (plan_id,)
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM medication_plan WHERE plan_id=? AND version=? AND status='active'",
                (plan_id, int(version)),
            ).fetchone()
        if row is None:
            raise DomainError("active plan not found", 409)
        return self._normalize_plan_row(row_dict(row))

    # ---------- occurrence generation and scheduler ----------

    def _local_schedule(self, plan, day):
        # Compatibility helper retained for callers that used the old MVP
        # private method.  New expansion goes through ScheduleExpander.
        hour, minute = [int(part) for part in plan["schedule_time"].split(":")]
        return datetime.combine(day, time(hour, minute), tzinfo=SHANGHAI)

    def _ensure_occurrences_in_transaction(self, connection, plan, clock,
                                          allow_reactivation=False):
        plan = self._normalize_plan_row(plan)
        specs = self._expand_plan_specs_in_transaction(connection, plan, clock)
        inserted = 0
        reactivated = 0
        inserted_times = []
        for spec in specs:
            scheduled = spec.scheduled_at.astimezone(UTC)
            deadline = scheduled + timedelta(
                minutes=int(plan["confirmation_window_minutes"])
            )
            scheduled_text = iso(scheduled)
            occurrence_id = self._occurrence_identity(plan, scheduled)
            existing = connection.execute(
                "SELECT intake_status, cancel_reason FROM medication_occurrence "
                "WHERE occurrence_id=?",
                (occurrence_id,),
            ).fetchone()
            if (
                allow_reactivation
                and existing is not None
                and existing["intake_status"] == "cancelled"
                and existing["cancel_reason"] in ("routine_changed", "schedule_recalculated")
            ):
                cursor = connection.execute(
                    """UPDATE medication_occurrence
                       SET confirmation_deadline_at=?, next_reminder_at=?,
                           intake_status='unconfirmed', actual_time=NULL,
                           confirmation_method=NULL, reminder_count=0,
                           snooze_count=0, max_snooze_count=?, max_snooze_until=?,
                           reminder_claimed_at=NULL, cancel_reason=NULL,
                           cancelled_by_safety_check_id=NULL, updated_at=?
                       WHERE occurrence_id=? AND intake_status='cancelled'""",
                    (
                        iso(deadline), scheduled_text,
                        int(plan["max_snooze_count"]), iso(deadline), iso(clock),
                        occurrence_id,
                    ),
                )
                if cursor.rowcount:
                    inserted += cursor.rowcount
                    reactivated += cursor.rowcount
                    inserted_times.append(scheduled_text)
                continue
            cursor = connection.execute(
                """INSERT OR IGNORE INTO medication_occurrence
                   (occurrence_id, plan_id, plan_version, elder_id, scheduled_at,
                    confirmation_deadline_at, next_reminder_at, intake_status,
                    max_snooze_count, max_snooze_until, drug_name_snapshot,
                    dosage_snapshot, relation_to_meal_snapshot, schedule_type,
                    schedule_snapshot_json, schedule_source, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'unconfirmed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    occurrence_id, plan["plan_id"], plan["version"], plan["elder_id"],
                    scheduled_text, iso(deadline), scheduled_text,
                    int(plan["max_snooze_count"]), iso(deadline), plan["drug_name"],
                    plan["dosage_text"], plan.get("relation_to_meal"),
                    spec.schedule_type, json_text(spec.schedule_snapshot),
                    spec.schedule_source, iso(clock), iso(clock),
                ),
            )
            if cursor.rowcount:
                inserted += cursor.rowcount
                inserted_times.append(scheduled_text)
        if inserted:
            payload = {
                "plan_id": plan["plan_id"],
                "plan_version": plan["version"],
                "schedule_type": plan["schedule_type"],
                "schedule_config": plan["schedule_config"],
                "occurrence_count": inserted,
                "occurrences_reactivated": reactivated,
                "scheduled_at": inserted_times,
                "horizon_days": int(self.config["occurrence_window_days"]),
            }
            event = self._event(
                "medication.schedule.expanded", "medication_service",
                elder_id=plan["elder_id"], plan_id=plan["plan_id"],
                payload=payload, occurred_at=clock,
            )
            self._audit_and_enqueue(
                connection, event,
                "schedule_expanded:%s:%s:%s" % (
                    plan["plan_id"], plan["version"], inserted_times[-1]
                ),
            )
        return inserted

    def ensure_occurrences(self, now=None):
        clock = now or now_utc()
        with self.storage.transaction() as connection:
            plans = connection.execute(
                "SELECT * FROM medication_plan WHERE status='active'"
            ).fetchall()
            inserted = 0
            for row in plans:
                plan = self._normalize_plan_row(row_dict(row))
                try:
                    self._assert_schedule_ready_in_transaction(connection, plan)
                except DomainError:
                    # An active plan can outlive its routine context.  Keep
                    # history intact and wait for an explicit context update;
                    # never guess an anchor or generate a reminder.
                    continue
                safety = self._ensure_plan_safety_in_transaction(connection, plan, clock)
                if safety.get("status") == STATUS_BLOCK:
                    # A stale ruleset or a provider failure never opens a new
                    # scheduling window.  The safety event already explains why.
                    continue
                inserted += self._ensure_occurrences_in_transaction(connection, plan, clock)
        return inserted

    def _cancel_future_occurrences_in_transaction(self, connection, plan_id, version,
                                                   clock, reason, safety_check_id=None):
        """Cancel only future, still-unconfirmed occurrences.

        The returned IDs let a caller emit one durable transition event.
        Completed or historical intake facts are deliberately outside this
        update predicate.
        """

        timestamp = iso(clock)
        rows = connection.execute(
            """SELECT occurrence_id FROM medication_occurrence
               WHERE plan_id=? AND plan_version=? AND intake_status='unconfirmed'
                 AND scheduled_at > ?
               ORDER BY scheduled_at, occurrence_id""",
            (plan_id, version, timestamp),
        ).fetchall()
        occurrence_ids = [row["occurrence_id"] for row in rows]
        if not occurrence_ids:
            return []
        connection.execute(
            """UPDATE medication_occurrence
               SET intake_status='cancelled', cancel_reason=?,
                   cancelled_by_safety_check_id=COALESCE(?, cancelled_by_safety_check_id),
                   reminder_claimed_at=NULL, updated_at=?
               WHERE plan_id=? AND plan_version=? AND intake_status='unconfirmed'
                 AND scheduled_at > ?""",
            (reason, safety_check_id, timestamp, plan_id, version, timestamp),
        )
        placeholders = ",".join("?" for _ in occurrence_ids)
        connection.execute(
            """UPDATE medication_interaction SET status='expired', updated_at=?
               WHERE occurrence_id IN (%s) AND status='open'""" % placeholders,
            [timestamp] + occurrence_ids,
        )
        return occurrence_ids

    def _apply_safety_block_to_future_occurrences(self, connection, plan, safety, clock):
        """Apply one BLOCK result to future occurrences in the same transaction."""

        occurrence_ids = self._cancel_future_occurrences_in_transaction(
            connection, plan["plan_id"], plan["version"], clock,
            "SAFETY_BLOCKED", safety.get("check_id"),
        )
        if not occurrence_ids:
            return []
        payload = {
            "plan_id": plan["plan_id"],
            "plan_version": plan["version"],
            "status": safety.get("status", STATUS_BLOCK),
            "reason": "SAFETY_BLOCKED",
            "safety_check_id": safety.get("check_id"),
            "ruleset_version": safety.get("ruleset_version"),
            "ruleset_fingerprint": safety.get("ruleset_fingerprint"),
            "occurrence_ids": occurrence_ids,
            "cancelled_count": len(occurrence_ids),
            "trace_id": safety.get("trace_id"),
        }
        event = self._event(
            "medication.safety.future_occurrences_cancelled",
            "m2_safety",
            elder_id=plan.get("elder_id"),
            plan_id=plan["plan_id"],
            payload=payload,
            occurred_at=clock,
            trace_id=safety.get("trace_id"),
        )
        self._audit_and_enqueue(
            connection, event,
            "safety_future_occurrences_cancelled:%s" % safety.get("check_id"),
        )
        return occurrence_ids

    def _recent_closed_unconfirmed_count(self, connection, occurrence, clock):
        lookback_hours = max(
            int(self.config.get("escalation_repeat_missed_lookback_hours", 24)), 0
        )
        since = iso(clock - timedelta(hours=lookback_hours))
        row = connection.execute(
            """SELECT COUNT(*) AS missed_count FROM medication_occurrence o
               WHERE o.elder_id=? AND o.plan_id=? AND o.drug_name_snapshot=?
                 AND o.intake_status='closed_unconfirmed'
                 AND EXISTS (
                     SELECT 1 FROM medication_event_log l
                     WHERE l.occurrence_id=o.occurrence_id
                       AND l.event_type='medication.intake.unconfirmed'
                       AND l.occurred_at >= ? AND l.occurred_at <= ?
                 )""",
            (
                occurrence["elder_id"], occurrence["plan_id"],
                occurrence["drug_name_snapshot"], since, iso(clock),
            ),
        ).fetchone()
        return int(row["missed_count"])

    def _escalation_payload(self, escalation, occurrence, level=None):
        escalation = dict(escalation)
        occurrence = dict(occurrence)
        selected_level = level or escalation["current_level"]
        return {
            "escalation_id": escalation["escalation_id"],
            "elder_id": escalation["elder_id"],
            "occurrence_id": escalation["occurrence_id"],
            "interaction_id": escalation.get("interaction_id"),
            "plan_id": escalation["plan_id"],
            "plan_version": escalation["plan_version"],
            "reason": escalation["reason"],
            "level": selected_level,
            "source_event_id": escalation["source_event_id"],
            "scheduled_at": occurrence["scheduled_at"],
            "opened_at": escalation["opened_at"],
            "respond_before": escalation.get("next_escalation_at"),
            "resolution_deadline_at": escalation.get("resolution_deadline_at"),
            "needs_manual_review": bool(escalation.get("needs_manual_review", 0)),
            "medication": {
                "name": occurrence["drug_name_snapshot"],
                "dose": occurrence["dosage_snapshot"],
                "instruction": occurrence.get("relation_to_meal_snapshot"),
            },
        }

    def _create_escalation_step_in_transaction(
        self, connection, escalation, occurrence, level, clock
    ):
        existing = connection.execute(
            "SELECT * FROM medication_escalation_step WHERE escalation_id=? AND level=?",
            (escalation["escalation_id"], level),
        ).fetchone()
        if existing is not None:
            return row_dict(existing)
        event_type = notification_event_type(level)
        if not event_type:
            raise DomainError("unsupported escalation notification level", 500)
        trace_id = "escalation:%s" % escalation["escalation_id"]
        payload = self._escalation_payload(escalation, occurrence, level)
        payload.update({
            "target_role": target_role(level),
            "action": event_type,
            "priority": "high" if level == LEVEL_MANUAL_REVIEW else "normal",
            "trace_id": trace_id,
        })
        event = self._event(
            event_type,
            "m6_escalation",
            elder_id=escalation["elder_id"],
            plan_id=escalation["plan_id"],
            occurrence_id=escalation["occurrence_id"],
            payload=payload,
            occurred_at=clock,
            trace_id=trace_id,
        )
        self._audit_and_enqueue(
            connection,
            event,
            "escalation_notification:%s:%s" % (escalation["escalation_id"], level),
        )
        step = {
            "step_id": new_id("escalation_step"),
            "escalation_id": escalation["escalation_id"],
            "level": level,
            "target_role": target_role(level),
            "action": event_type,
            "status": "queued",
            "scheduled_at": iso(clock),
            "executed_at": None,
            "event_id": event["event_id"],
            "created_at": iso(clock),
        }
        connection.execute(
            """INSERT INTO medication_escalation_step
               (step_id, escalation_id, level, target_role, action, status,
                scheduled_at, executed_at, event_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                step["step_id"], step["escalation_id"], step["level"],
                step["target_role"], step["action"], step["status"],
                step["scheduled_at"], step["executed_at"], step["event_id"],
                step["created_at"],
            ),
        )
        return step

    def _open_escalation_in_transaction(self, connection, occurrence, source_event, clock):
        if not self.config.get("escalation_enabled", True):
            return None
        existing = connection.execute(
            "SELECT * FROM medication_escalation WHERE occurrence_id=?",
            (occurrence["occurrence_id"],),
        ).fetchone()
        if existing is not None:
            return row_dict(existing)

        missed_count = self._recent_closed_unconfirmed_count(connection, occurrence, clock)
        repeat_threshold = int(self.config.get("escalation_repeat_missed_count", 2))
        level = initial_level(missed_count, repeat_threshold)
        timeout_key = {
            LEVEL_CAREGIVER: "escalation_caregiver_timeout_minutes",
            LEVEL_FAMILY: "escalation_family_timeout_minutes",
        }.get(level)
        timeout_minutes = int(self.config.get(timeout_key, 0)) if timeout_key else 0
        next_at = iso(clock + timedelta(minutes=max(timeout_minutes, 0))) if timeout_key else None
        interaction = connection.execute(
            """SELECT interaction_id FROM medication_interaction
               WHERE occurrence_id=? ORDER BY created_at DESC LIMIT 1""",
            (occurrence["occurrence_id"],),
        ).fetchone()
        escalation = {
            "escalation_id": new_id("escalation"),
            "elder_id": occurrence["elder_id"],
            "occurrence_id": occurrence["occurrence_id"],
            "interaction_id": interaction["interaction_id"] if interaction else None,
            "plan_id": occurrence["plan_id"],
            "plan_version": occurrence["plan_version"],
            "reason": "closed_unconfirmed",
            "source_event_id": source_event["event_id"],
            "current_level": level,
            "status": STATUS_OPEN,
            "needs_manual_review": 0,
            "opened_at": iso(clock),
            "next_escalation_at": next_at,
            "resolution_deadline_at": None,
            "acknowledged_at": None,
            "resolved_at": None,
            "acknowledged_by": None,
            "resolved_by": None,
            "resolution_code": None,
            "resolution_note": None,
            "created_at": iso(clock),
            "updated_at": iso(clock),
        }
        cursor = connection.execute(
            """INSERT OR IGNORE INTO medication_escalation
               (escalation_id, elder_id, occurrence_id, interaction_id, plan_id,
                plan_version, reason, source_event_id, current_level, status,
                needs_manual_review, opened_at, next_escalation_at,
                resolution_deadline_at, acknowledged_at, resolved_at,
                acknowledged_by, resolved_by, resolution_code, resolution_note,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                escalation["escalation_id"], escalation["elder_id"],
                escalation["occurrence_id"], escalation["interaction_id"],
                escalation["plan_id"], escalation["plan_version"],
                escalation["reason"], escalation["source_event_id"],
                escalation["current_level"], escalation["status"],
                escalation["needs_manual_review"], escalation["opened_at"],
                escalation["next_escalation_at"], escalation["resolution_deadline_at"],
                escalation["acknowledged_at"], escalation["resolved_at"],
                escalation["acknowledged_by"], escalation["resolved_by"],
                escalation["resolution_code"], escalation["resolution_note"],
                escalation["created_at"], escalation["updated_at"],
            ),
        )
        if cursor.rowcount != 1:
            existing = connection.execute(
                "SELECT * FROM medication_escalation WHERE occurrence_id=?",
                (occurrence["occurrence_id"],),
            ).fetchone()
            return row_dict(existing)

        payload = self._escalation_payload(escalation, occurrence)
        payload.update({"missed_count": missed_count, "trace_id": "escalation:%s" % escalation["escalation_id"]})
        opened = self._event(
            "medication.escalation.opened",
            "m6_escalation",
            elder_id=escalation["elder_id"],
            plan_id=escalation["plan_id"],
            occurrence_id=escalation["occurrence_id"],
            payload=payload,
            occurred_at=clock,
            trace_id=payload["trace_id"],
        )
        self._audit_and_enqueue(
            connection,
            opened,
            "escalation_opened:%s" % escalation["escalation_id"],
        )
        self._create_escalation_step_in_transaction(
            connection, escalation, occurrence, level, clock
        )
        return escalation

    def _close_expired(self, clock):
        closed = []
        with self.storage.transaction() as connection:
            rows = connection.execute(
                """SELECT * FROM medication_occurrence
                   WHERE intake_status='unconfirmed' AND confirmation_deadline_at <= ?""",
                (iso(clock),),
            ).fetchall()
            for row in rows:
                cursor = connection.execute(
                    """UPDATE medication_occurrence
                       SET intake_status='closed_unconfirmed', reminder_claimed_at=NULL, updated_at=?
                       WHERE occurrence_id=? AND intake_status='unconfirmed'
                         AND confirmation_deadline_at <= ?""",
                    (iso(clock), row["occurrence_id"], iso(clock)),
                )
                if cursor.rowcount != 1:
                    continue
                connection.execute(
                    "UPDATE medication_interaction SET status='expired', updated_at=? "
                    "WHERE occurrence_id=? AND status='open'",
                    (iso(clock), row["occurrence_id"]),
                )
                event = self._event(
                    "medication.intake.unconfirmed", "scheduler",
                    elder_id=row["elder_id"], plan_id=row["plan_id"],
                    occurrence_id=row["occurrence_id"],
                    payload={"occurrence_id": row["occurrence_id"],
                             "status": "closed_unconfirmed",
                             "scheduled_at": row["scheduled_at"]},
                    occurred_at=clock,
                )
                self._audit_and_enqueue(connection, event,
                                        "unconfirmed_closed:%s" % row["occurrence_id"])
                self._open_escalation_in_transaction(connection, row, event, clock)
                closed.append(row["occurrence_id"])
        return closed

    def process_due_escalations(self, now=None):
        """Advance due OPEN or ACKNOWLEDGED escalations atomically."""

        clock = now or now_utc()
        due_at = iso(clock)
        processed = []
        with self.storage.transaction() as connection:
            rows = connection.execute(
                """SELECT e.*, o.scheduled_at, o.drug_name_snapshot,
                          o.dosage_snapshot, o.relation_to_meal_snapshot
                   FROM medication_escalation e
                   JOIN medication_occurrence o ON o.occurrence_id=e.occurrence_id
                   WHERE (e.status=? AND e.next_escalation_at IS NOT NULL
                          AND e.next_escalation_at <= ?)
                      OR (e.status=? AND e.resolution_deadline_at IS NOT NULL
                          AND e.resolution_deadline_at <= ?)
                   ORDER BY COALESCE(e.next_escalation_at, e.resolution_deadline_at),
                            e.escalation_id""",
                (STATUS_OPEN, due_at, STATUS_ACKNOWLEDGED, due_at),
            ).fetchall()
            for row in rows:
                current = row["current_level"]
                promoted = next_level(current)
                if promoted is None:
                    continue
                next_timeout = (
                    int(self.config.get("escalation_family_timeout_minutes", 60))
                    if promoted == LEVEL_FAMILY else 0
                )
                next_at = (
                    iso(clock + timedelta(minutes=max(next_timeout, 0)))
                    if promoted != LEVEL_MANUAL_REVIEW else None
                )
                needs_manual_review = int(promoted == LEVEL_MANUAL_REVIEW)
                cursor = connection.execute(
                    """UPDATE medication_escalation
                       SET status=?, current_level=?, next_escalation_at=?,
                           resolution_deadline_at=NULL,
                           needs_manual_review=?, updated_at=?
                       WHERE escalation_id=? AND current_level=?
                         AND ((status=? AND next_escalation_at IS NOT NULL
                               AND next_escalation_at <= ?)
                              OR (status=? AND resolution_deadline_at IS NOT NULL
                                  AND resolution_deadline_at <= ?))""",
                    (
                        STATUS_OPEN, promoted, next_at, needs_manual_review, due_at,
                        row["escalation_id"], current,
                        STATUS_OPEN, due_at, STATUS_ACKNOWLEDGED, due_at,
                    ),
                )
                if cursor.rowcount != 1:
                    continue
                escalation = dict(row)
                escalation.update({
                    "status": STATUS_OPEN,
                    "current_level": promoted,
                    "next_escalation_at": next_at,
                    "resolution_deadline_at": None,
                    "needs_manual_review": needs_manual_review,
                })
                occurrence = {
                    "occurrence_id": row["occurrence_id"],
                    "scheduled_at": row["scheduled_at"],
                    "drug_name_snapshot": row["drug_name_snapshot"],
                    "dosage_snapshot": row["dosage_snapshot"],
                    "relation_to_meal_snapshot": row["relation_to_meal_snapshot"],
                }
                transition_payload = self._escalation_payload(
                    escalation, occurrence, promoted
                )
                transition_payload.update({
                    "from_level": current,
                    "to_level": promoted,
                    "trace_id": "escalation:%s" % row["escalation_id"],
                })
                transition = self._event(
                    "medication.escalation.escalated",
                    "m6_escalation",
                    elder_id=row["elder_id"],
                    plan_id=row["plan_id"],
                    occurrence_id=row["occurrence_id"],
                    payload=transition_payload,
                    occurred_at=clock,
                    trace_id=transition_payload["trace_id"],
                )
                self._audit_and_enqueue(
                    connection,
                    transition,
                    "escalation_escalated:%s:%s" % (row["escalation_id"], promoted),
                )
                self._create_escalation_step_in_transaction(
                    connection, escalation, occurrence, promoted, clock
                )
                processed.append({
                    "escalation_id": row["escalation_id"],
                    "from_level": current,
                    "level": promoted,
                    "needs_manual_review": bool(needs_manual_review),
                })
        return processed

    def run_escalation_cycle(self, now=None, publish=True):
        clock = now or now_utc()
        processed = self.process_due_escalations(clock)
        published = self.publish_outbox() if publish else []
        return {
            "escalations_processed": processed,
            "outbox_published": published,
        }

    def _claim_due(self, clock):
        claimed = []
        with self.storage.transaction() as connection:
            rows = connection.execute(
                """SELECT o.*, p.device_sn FROM medication_occurrence o
                   JOIN medication_plan p ON p.plan_id=o.plan_id AND p.version=o.plan_version
                   JOIN medication_safety_check sc ON sc.check_id=(
                       SELECT latest.check_id FROM medication_safety_check latest
                       WHERE latest.plan_id=o.plan_id AND latest.plan_version=o.plan_version
                       ORDER BY latest.checked_at DESC, latest.created_at DESC LIMIT 1
                   )
                   WHERE o.intake_status='unconfirmed'
                     AND p.status='active'
                     AND sc.status IN ('PASS', 'WARN')
                     AND sc.check_failed=0
                     AND sc.ruleset_version=?
                     AND sc.ruleset_fingerprint=?
                     AND o.confirmation_deadline_at > ?
                     AND o.next_reminder_at <= ?
                     AND o.reminder_claimed_at IS NULL
                   ORDER BY o.next_reminder_at, o.occurrence_id""",
                (
                    self.safety_ruleset_version, self.safety_ruleset_fingerprint,
                    iso(clock), iso(clock),
                ),
            ).fetchall()
            for row in rows:
                interaction_id = new_id("interaction")
                attempt_id = new_id("attempt")
                trace_id = new_id("trace")
                expires = min(
                    parse_datetime(row["confirmation_deadline_at"]),
                    clock + timedelta(minutes=int(self.config["interaction_ttl_minutes"])),
                )
                event = self._event(
                    "medication.reminder_due", "scheduler",
                    elder_id=row["elder_id"], plan_id=row["plan_id"],
                    occurrence_id=row["occurrence_id"],
                    payload={
                        "occurrence_id": row["occurrence_id"],
                        "plan_id": row["plan_id"],
                        "plan_version": row["plan_version"],
                        "interaction_id": interaction_id,
                        "attempt_id": attempt_id,
                        "device_sn": row["device_sn"],
                        "drug_name": row["drug_name_snapshot"],
                        "dosage": row["dosage_snapshot"],
                        "relation_to_meal": row["relation_to_meal_snapshot"],
                        "trace_id": trace_id,
                        "scheduled_at": row["scheduled_at"],
                        "expires_at": iso(expires),
                        "medication": {
                            "name": row["drug_name_snapshot"],
                            "dose": row["dosage_snapshot"],
                            "instruction": row["relation_to_meal_snapshot"],
                        },
                        "reminder_text": self._reminder_text(row),
                        "text": self._reminder_text(row),
                    },
                    occurred_at=clock, trace_id=trace_id,
                )
                connection.execute(
                    """INSERT INTO medication_interaction
                       (interaction_id, elder_id, device_sn, occurrence_id,
                        opened_at, expires_at, status, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
                    (interaction_id, row["elder_id"], row["device_sn"], row["occurrence_id"],
                     iso(clock), iso(expires), iso(clock), iso(clock)),
                )
                level = int(row["reminder_count"]) + 1
                connection.execute(
                    """INSERT INTO reminder_attempt
                       (attempt_id, occurrence_id, interaction_id, level, scheduled_at,
                        delivery_status, event_id, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)""",
                    (attempt_id, row["occurrence_id"], interaction_id, level,
                     row["next_reminder_at"], event["event_id"], iso(clock), iso(clock)),
                )
                cursor = connection.execute(
                    """UPDATE medication_occurrence
                       SET reminder_count=reminder_count+1, reminder_claimed_at=?, updated_at=?
                       WHERE occurrence_id=? AND intake_status='unconfirmed'
                         AND reminder_claimed_at IS NULL""",
                    (iso(clock), iso(clock), row["occurrence_id"]),
                )
                if cursor.rowcount != 1:
                    raise DomainError("concurrent occurrence claim failed", 409)
                self._audit_and_enqueue(
                    connection, event,
                    "reminder_due:%s:%s" % (row["occurrence_id"], level),
                )
                claimed.append({
                    "occurrence_id": row["occurrence_id"],
                    "interaction_id": interaction_id,
                    "attempt_id": attempt_id,
                    "event_id": event["event_id"],
                })
        return claimed

    def _reminder_text(self, row):
        relation = row["relation_to_meal_snapshot"]
        suffix = ("，%s" % relation) if relation else ""
        return "%s，该吃%s了，请服用%s%s。" % (
            row["elder_id"], row["drug_name_snapshot"], row["dosage_snapshot"], suffix
        )

    def run_scheduler_cycle(self, now=None, publish=True):
        clock = now or now_utc()
        inserted = self.ensure_occurrences(clock)
        closed = self._close_expired(clock)
        escalations = self.process_due_escalations(clock)
        claimed = self._claim_due(clock)
        published = self.publish_outbox() if publish else []
        return {
            "occurrences_created": inserted,
            "closed_unconfirmed": closed,
            "escalations_processed": escalations,
            "reminders_claimed": claimed,
            "outbox_published": published,
        }

    # ---------- response/intake state machine ----------

    def process_user_response(self, data):
        data = dict(data or {})
        event_id = str(data.get("event_id") or new_id("evt"))
        action = normalize_action(data.get("action"))
        delay_minutes = data.get("delay_minutes")
        if action is None:
            action, parsed_delay = parse_fast_path(
                data.get("text"), int(self.config["default_delay_minutes"])
            )
            if delay_minutes is None:
                delay_minutes = parsed_delay
        if action not in ("CONFIRM_TAKEN", "DELAY", "SKIP", "REPEAT"):
            raise DomainError("could not understand medication response", 422)
        occurrence_id = data.get("occurrence_id")
        interaction_id = data.get("interaction_id")
        if not interaction_id:
            raise DomainError("interaction_id is required; response cannot guess a task")
        occurred_at = parse_datetime(data["occurred_at"]) if data.get("occurred_at") else now_utc()
        clock = now_utc()
        trace_id = str(data.get("trace_id") or "interaction:%s" % interaction_id)
        with self.storage.transaction() as connection:
            if self._event_already_logged(connection, event_id):
                current = self._occurrence_for_interaction(connection, interaction_id)
                return {"duplicate": True, "action": action, "occurrence": row_dict(current)}
            interaction = connection.execute(
                "SELECT * FROM medication_interaction WHERE interaction_id=?",
                (interaction_id,),
            ).fetchone()
            if interaction is None:
                raise DomainError("interaction not found", 404)
            if data.get("elder_id") and data["elder_id"] != interaction["elder_id"]:
                raise DomainError("elder_id does not match interaction", 409)
            if occurrence_id and occurrence_id != interaction["occurrence_id"]:
                raise DomainError("occurrence_id does not match trusted interaction", 409)
            occurrence = connection.execute(
                "SELECT * FROM medication_occurrence WHERE occurrence_id=?",
                (interaction["occurrence_id"],),
            ).fetchone()
            if occurrence is None:
                raise DomainError("occurrence not found", 404)
            late_voice = (
                action == "CONFIRM_TAKEN" and occurrence["intake_status"] == "closed_unconfirmed"
            )
            if (interaction["status"] != "open" or parse_datetime(interaction["expires_at"]) <= clock) and not late_voice:
                raise DomainError("interaction is expired or already closed", 409)
            incoming = self._event(
                "medication.user_response", data.get("source", "chat_agent"),
                elder_id=interaction["elder_id"], plan_id=occurrence["plan_id"],
                occurrence_id=occurrence["occurrence_id"], payload={
                    "interaction_id": interaction_id,
                    "trace_id": trace_id,
                    "action": action,
                    "delay_minutes": delay_minutes,
                    "text": data.get("text"),
                }, occurred_at=occurred_at, event_id=event_id, trace_id=trace_id,
            )
            self._insert_event_log(connection, incoming)
            if action == "REPEAT":
                return {
                    "duplicate": False,
                    "action": action,
                    "trace_id": trace_id,
                    "reminder_text": self._reminder_text(occurrence),
                    "occurrence": row_dict(occurrence),
                }
            if occurrence["intake_status"] != UNCONFIRMED and not late_voice:
                raise DomainError("occurrence is already in terminal state", 409)
            timestamp = iso(clock)
            if action == "DELAY":
                try:
                    minutes = int(delay_minutes)
                except (TypeError, ValueError) as exc:
                    raise DomainError("delay_minutes must be an integer") from exc
                if minutes <= 0:
                    raise DomainError("delay_minutes must be positive")
                if int(occurrence["snooze_count"]) >= int(occurrence["max_snooze_count"]):
                    raise DomainError("maximum snooze count reached", 409)
                next_time = clock + timedelta(minutes=minutes)
                deadline = parse_datetime(occurrence["max_snooze_until"])
                if next_time > deadline:
                    raise DomainError("delay would pass confirmation deadline", 409)
                connection.execute(
                    """UPDATE medication_occurrence
                       SET next_reminder_at=?, snooze_count=snooze_count+1,
                           reminder_claimed_at=NULL, updated_at=?
                       WHERE occurrence_id=? AND intake_status='unconfirmed'""",
                    (iso(next_time), timestamp, occurrence["occurrence_id"]),
                )
                connection.execute(
                    "UPDATE medication_interaction SET status='closed', updated_at=? WHERE interaction_id=?",
                    (timestamp, interaction_id),
                )
                output = self._event(
                    "medication.reminder.delayed", "medication_service",
                    elder_id=occurrence["elder_id"], plan_id=occurrence["plan_id"],
                    occurrence_id=occurrence["occurrence_id"], payload={
                        "interaction_id": interaction_id,
                        "trace_id": trace_id,
                        "next_reminder_at": iso(next_time),
                        "snooze_count": int(occurrence["snooze_count"]) + 1,
                    }, occurred_at=clock, trace_id=trace_id,
                )
            else:
                source = str(data.get("source") or "chat_agent").strip().lower()
                source_type = data.get("source_type") or (
                    "USER_BUTTON"
                    if source.endswith("_ui") or source in ("web", "web_test", "button")
                    else "USER_VOICE"
                )
                evidence_result = self._record_evidence_in_transaction(
                    connection,
                    dict(
                        data,
                        event_id="%s:evidence" % event_id,
                        elder_id=occurrence["elder_id"],
                        occurrence_id=occurrence["occurrence_id"],
                        interaction_id=interaction_id,
                        source_type=source_type,
                        evidence_type=(
                            "SELF_REPORTED_NOT_TAKEN"
                            if action == "SKIP"
                            else "BUTTON_CONFIRMED"
                            if self._normalize_evidence_source(source_type) == "USER_BUTTON"
                            else "SELF_REPORTED_TAKEN"
                        ),
                        observed_at=iso(occurred_at),
                        value={"text": data.get("text"), "action": action},
                        trace_id=trace_id,
                    ),
                    assess=action == "CONFIRM_TAKEN",
                    identity_trusted=self._normalize_evidence_source(source_type) in (
                        "USER_VOICE", "USER_BUTTON"
                    ),
                )
                if action == "SKIP":
                    connection.execute(
                        """UPDATE medication_occurrence
                           SET intake_status='skipped', actual_time=?,
                               confirmation_method='voice', reminder_claimed_at=NULL, updated_at=?
                           WHERE occurrence_id=? AND intake_status='unconfirmed'""",
                        (iso(occurred_at), timestamp, occurrence["occurrence_id"]),
                    )
                    connection.execute(
                        "UPDATE medication_interaction SET status='closed', updated_at=? WHERE interaction_id=?",
                        (timestamp, interaction_id),
                    )
                    output = self._event(
                        "medication.intake.updated", "medication_service",
                        elder_id=occurrence["elder_id"], plan_id=occurrence["plan_id"],
                        occurrence_id=occurrence["occurrence_id"], payload={
                            "interaction_id": interaction_id, "trace_id": trace_id,
                            "status": "skipped", "actual_time": iso(occurred_at),
                            "confirmation_method": "voice",
                        }, occurred_at=clock, trace_id=trace_id,
                    )
                    self._audit_and_enqueue(connection, output)
            updated = connection.execute(
                "SELECT * FROM medication_occurrence WHERE occurrence_id=?",
                (occurrence["occurrence_id"],),
            ).fetchone()
            if action == "DELAY":
                self._audit_and_enqueue(connection, output)
            response = {
                "duplicate": False,
                "action": action,
                "trace_id": trace_id,
                "occurrence": row_dict(updated),
            }
            if action == "CONFIRM_TAKEN":
                response["assessment"] = self._hydrate_assessment(evidence_result["assessment"])
                response["evidence"] = self._hydrate_evidence(evidence_result["evidence"])
            elif action == "SKIP":
                response["evidence"] = self._hydrate_evidence(evidence_result["evidence"])
            return response

    def _occurrence_for_interaction(self, connection, interaction_id):
        interaction = connection.execute(
            "SELECT occurrence_id FROM medication_interaction WHERE interaction_id=?",
            (interaction_id,),
        ).fetchone()
        if interaction is None:
            raise DomainError("interaction not found", 404)
        return connection.execute(
            "SELECT * FROM medication_occurrence WHERE occurrence_id=?",
            (interaction["occurrence_id"],),
        ).fetchone()

    # ---------- device delivery lifecycle ----------

    def process_device_event(self, data):
        data = dict(data or {})
        event_id = str(data.get("event_id") or new_id("evt"))
        raw_event_type = str(data.get("event_type") or data.get("status") or "").strip().lower()
        aliases = {
            "started": "device.reminder.started",
            "completed": "device.reminder.completed",
            "failed": "device.reminder.failed",
            "interrupted": "device.reminder.interrupted",
            "expired": "device.reminder.expired",
        }
        event_type = aliases.get(raw_event_type, raw_event_type)
        allowed_events = (
            "device.reminder.started", "device.reminder.completed",
            "device.reminder.failed", "device.reminder.interrupted",
            "device.reminder.expired",
        )
        if event_type not in allowed_events:
            raise DomainError("unsupported device event_type")
        if not str(data.get("interaction_id") or "").strip():
            raise DomainError("interaction_id is required for device events")
        if not str(data.get("elder_id") or "").strip():
            raise DomainError("elder_id is required for device events")
        occurred_at = parse_datetime(data["timestamp"]) if data.get("timestamp") else now_utc()
        received_at = now_utc()
        with self.storage.transaction() as connection:
            existing = connection.execute(
                "SELECT payload_json FROM medication_event_log WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if existing is not None:
                payload = json.loads(existing["payload_json"])
                lookup = dict(data)
                lookup.setdefault("attempt_id", payload.get("attempt_id"))
                lookup.setdefault("interaction_id", payload.get("interaction_id"))
                attempt = self._select_attempt(connection, lookup)
                return {"duplicate": True, "attempt": row_dict(attempt)}

            attempt = self._select_attempt(connection, data)
            if attempt is None:
                raise DomainError("reminder attempt not found", 404)
            if data.get("attempt_id") and data["attempt_id"] != attempt["attempt_id"]:
                raise DomainError("attempt_id does not match reminder interaction", 409)
            interaction_id = data["interaction_id"]
            if interaction_id != attempt["interaction_id"]:
                raise DomainError("interaction_id does not match reminder attempt", 409)
            interaction = connection.execute(
                "SELECT * FROM medication_interaction WHERE interaction_id=?",
                (interaction_id,),
            ).fetchone()
            if interaction is None:
                raise DomainError("interaction not found", 404)
            if interaction["occurrence_id"] != attempt["occurrence_id"]:
                raise DomainError("interaction is not bound to reminder attempt", 409)
            occurrence = connection.execute(
                "SELECT * FROM medication_occurrence WHERE occurrence_id=?",
                (attempt["occurrence_id"],),
            ).fetchone()
            if occurrence is None:
                raise DomainError("occurrence not found", 404)
            if data.get("elder_id") and data["elder_id"] != interaction["elder_id"]:
                raise DomainError("elder_id does not match interaction", 409)
            if data.get("occurrence_id") and data["occurrence_id"] != occurrence["occurrence_id"]:
                raise DomainError("occurrence_id does not match trusted interaction", 409)
            if interaction["status"] != "open" or parse_datetime(interaction["expires_at"]) <= received_at:
                raise DomainError("interaction is expired or already closed", 409)

            current_status = attempt["delivery_status"]
            allowed_transitions = {
                "device.reminder.started": {"queued", "dispatched"},
                "device.reminder.completed": {"started"},
                "device.reminder.failed": {"queued", "dispatched", "started"},
                "device.reminder.interrupted": {"started"},
                "device.reminder.expired": {"queued", "dispatched"},
            }
            if current_status not in allowed_transitions[event_type]:
                raise DomainError(
                    "illegal reminder delivery transition: %s -> %s" %
                    (current_status, event_type.rsplit(".", 1)[-1]),
                    409,
                )

            timestamp = iso(received_at)
            new_status = event_type.rsplit(".", 1)[-1]
            reason = data.get("reason") or data.get("failure_reason")
            if new_status == "started":
                cursor = connection.execute(
                    """UPDATE reminder_attempt
                       SET delivery_status='started', started_at=COALESCE(started_at, ?), updated_at=?
                       WHERE attempt_id=? AND delivery_status IN ('queued', 'dispatched')""",
                    (timestamp, timestamp, attempt["attempt_id"]),
                )
            elif new_status == "completed":
                cursor = connection.execute(
                    """UPDATE reminder_attempt
                       SET delivery_status='completed', completed_at=?, updated_at=?
                       WHERE attempt_id=? AND delivery_status='started'""",
                    (timestamp, timestamp, attempt["attempt_id"]),
                )
            elif new_status == "failed":
                cursor = connection.execute(
                    """UPDATE reminder_attempt
                       SET delivery_status='failed', failure_reason=?, updated_at=?
                       WHERE attempt_id=? AND delivery_status IN ('queued', 'dispatched', 'started')""",
                    (reason, timestamp, attempt["attempt_id"]),
                )
            elif new_status == "interrupted":
                cursor = connection.execute(
                    """UPDATE reminder_attempt
                       SET delivery_status='interrupted', failure_reason=?, updated_at=?
                       WHERE attempt_id=? AND delivery_status='started'""",
                    (reason, timestamp, attempt["attempt_id"]),
                )
            else:
                cursor = connection.execute(
                    """UPDATE reminder_attempt
                       SET delivery_status='expired', failure_reason=?, updated_at=?
                       WHERE attempt_id=? AND delivery_status IN ('queued', 'dispatched')""",
                    (reason, timestamp, attempt["attempt_id"]),
                )
            if cursor.rowcount != 1:
                raise DomainError("reminder delivery state changed concurrently", 409)
            trace_id = str(
                data.get("trace_id") or "interaction:%s" % interaction_id
            )
            event = self._event(
                event_type, data.get("source", "device_adapter"),
                elder_id=interaction["elder_id"], plan_id=occurrence["plan_id"],
                occurrence_id=occurrence["occurrence_id"], payload={
                    "attempt_id": attempt["attempt_id"],
                    "interaction_id": interaction_id,
                    "trace_id": trace_id,
                    "delivery_status": new_status,
                    "timestamp": iso(occurred_at),
                    "reason": reason,
                }, event_id=event_id, occurred_at=occurred_at, trace_id=trace_id,
            )
            self._insert_event_log(connection, event)
            updated = connection.execute(
                "SELECT * FROM reminder_attempt WHERE attempt_id=?",
                (attempt["attempt_id"],),
            ).fetchone()
            return {"duplicate": False, "attempt": row_dict(updated), "trace_id": trace_id}

    def _select_attempt(self, connection, data):
        if data.get("attempt_id"):
            return connection.execute(
                "SELECT * FROM reminder_attempt WHERE attempt_id=?", (data["attempt_id"],)
            ).fetchone()
        if data.get("interaction_id"):
            return connection.execute(
                "SELECT * FROM reminder_attempt WHERE interaction_id=? ORDER BY created_at DESC LIMIT 1",
                (data["interaction_id"],),
            ).fetchone()
        return None

    # ---------- outbox / local adapter ----------

    def publish_outbox(self, limit=100):
        published = []
        for _ in range(10):
            lease_cutoff = now_utc() - timedelta(seconds=int(self.config["outbox_lease_seconds"]))
            rows = self.storage.fetchall(
                """SELECT * FROM domain_outbox
                   WHERE status='pending' OR (status='publishing' AND locked_at < ?)
                   ORDER BY created_at LIMIT ?""",
                (iso(lease_cutoff), int(limit)),
            )
            if not rows:
                break
            made_progress = False
            for row in rows:
                event_id = row["event_id"]
                with self.storage.transaction() as connection:
                    cursor = connection.execute(
                        """UPDATE domain_outbox SET status='publishing', locked_at=?, retry_count=retry_count+1
                           WHERE event_id=? AND (status='pending' OR (status='publishing' AND locked_at < ?))""",
                        (iso(now_utc()), event_id, iso(lease_cutoff)),
                    )
                    if cursor.rowcount != 1:
                        continue
                made_progress = True
                try:
                    event = json.loads(row["payload_json"])
                    self._publish_one(event)
                    with self.storage.transaction() as connection:
                        if event.get("event_type") in ESCALATION_NOTIFICATION_EVENTS:
                            self._mark_local_escalation_notification_simulated(
                                connection, event
                            )
                        connection.execute(
                            """UPDATE domain_outbox SET status='published', published_at=?, locked_at=NULL
                               WHERE event_id=? AND status='publishing'""",
                            (iso(now_utc()), event_id),
                        )
                    published.append(event_id)
                except DeviceAdapterError as exc:
                    if exc.retryable:
                        self._retry_outbox_event(event_id, exc)
                    else:
                        self._fail_outbox_event(event_id, event, exc)
                except Exception as exc:
                    self._retry_outbox_event(event_id, exc)
            if not made_progress:
                break
        return published

    def _retry_outbox_event(self, event_id, error):
        with self.storage.transaction() as connection:
            connection.execute(
                "UPDATE domain_outbox SET status=?, locked_at=NULL, last_error=? WHERE event_id=?",
                ("pending", str(error), event_id),
            )

    def _fail_outbox_event(self, event_id, event, error):
        payload = dict(event.get("payload") or {})
        with self.storage.transaction() as connection:
            connection.execute(
                "UPDATE domain_outbox SET status=?, locked_at=NULL, last_error=? WHERE event_id=?",
                ("failed", str(error), event_id),
            )
            attempt = None
            if payload.get("attempt_id"):
                attempt = connection.execute(
                    "SELECT * FROM reminder_attempt WHERE attempt_id=?",
                    (payload["attempt_id"],),
                ).fetchone()
            occurrence = None
            if attempt is not None:
                occurrence = connection.execute(
                    "SELECT * FROM medication_occurrence WHERE occurrence_id=?",
                    (attempt["occurrence_id"],),
                ).fetchone()
                if attempt["delivery_status"] not in ("completed", "failed", "interrupted", "expired"):
                    connection.execute(
                        "UPDATE reminder_attempt SET delivery_status=?, failure_reason=?, updated_at=? WHERE attempt_id=?",
                        ("failed", str(error), iso(now_utc()), attempt["attempt_id"]),
                    )
            elder_id = occurrence["elder_id"] if occurrence is not None else event.get("elder_id")
            plan_id = occurrence["plan_id"] if occurrence is not None else event.get("plan_id")
            occurrence_id = occurrence["occurrence_id"] if occurrence is not None else event.get("occurrence_id")
            trace_id = str(payload.get("trace_id") or event.get("trace_id") or "interaction:%s" % payload.get("interaction_id", "unknown"))
            failure = self._event(
                "device.reminder.failed", "device_adapter",
                elder_id=elder_id, plan_id=plan_id, occurrence_id=occurrence_id,
                payload={
                    "attempt_id": payload.get("attempt_id"),
                    "interaction_id": payload.get("interaction_id"),
                    "trace_id": trace_id,
                    "delivery_status": "failed",
                    "reason": str(error),
                    "error_kind": getattr(error, "kind", "adapter_error"),
                    "source_event_id": event_id,
                },
                trace_id=trace_id,
            )
            self._insert_event_log(connection, failure)

    def _mark_local_escalation_notification_simulated(self, connection, event):
        """Record local consumption without claiming external delivery."""

        payload = event.get("payload") or {}
        escalation_id = payload.get("escalation_id")
        level = payload.get("level")
        if not escalation_id or not level:
            return
        connection.execute(
            """UPDATE medication_escalation_step
               SET status='simulated', executed_at=COALESCE(executed_at, ?)
               WHERE escalation_id=? AND level=?
                 AND status IN ('queued', 'dispatched', 'simulated')""",
            (iso(now_utc()), escalation_id, level),
        )

    def _publish_escalation_notification(self, event):
        """Local notification seam; state is finalized with the Outbox update."""

        # The actual step update is intentionally performed by publish_outbox()
        # in the same transaction as domain_outbox -> published.
        return None

    def _publish_one(self, event):
        event_type = event["event_type"]
        if event_type == "medication.reminder_due":
            self._publish_reminder_due(event)
        elif event_type == "device.interaction.request":
            self._publish_device_request(event)
        elif event_type in (
            "caregiver.task.assign", "family_notify.request", "manual_review.request",
        ):
            self._publish_escalation_notification(event)
        # Other domain events are already durable in the event log. This is
        # the seam where Redis Streams or real notification channels can be
        # added without changing state code.

    def _publish_reminder_due(self, event):
        payload = event["payload"]
        with self.storage.transaction() as connection:
            occurrence = connection.execute(
                """SELECT o.*, p.status AS plan_status, p.version AS current_plan_version,
                          sc.status AS safety_status, sc.ruleset_version AS safety_ruleset_version,
                          sc.ruleset_fingerprint AS safety_ruleset_fingerprint,
                          sc.check_failed AS safety_check_failed, sc.check_id AS safety_check_id
                   FROM medication_occurrence o JOIN medication_plan p
                   ON p.plan_id=o.plan_id AND p.version=o.plan_version
                   LEFT JOIN medication_safety_check sc ON sc.check_id=(
                       SELECT latest.check_id FROM medication_safety_check latest
                       WHERE latest.plan_id=o.plan_id AND latest.plan_version=o.plan_version
                       ORDER BY latest.checked_at DESC, latest.created_at DESC LIMIT 1
                   )
                   WHERE o.occurrence_id=?""",
                (payload["occurrence_id"],),
            ).fetchone()
            ordinary_invalid = (
                occurrence is None
                or occurrence["intake_status"] != UNCONFIRMED
                or occurrence["plan_status"] != "active"
                or int(occurrence["current_plan_version"]) != int(payload["plan_version"])
                or parse_datetime(occurrence["confirmation_deadline_at"]) <= now_utc()
            )
            safety_invalid = bool(
                occurrence is not None
                and occurrence["intake_status"] == UNCONFIRMED
                and (
                    occurrence["safety_status"] not in (STATUS_PASS, STATUS_WARN)
                    or int(occurrence["safety_check_failed"] or 0) != 0
                    or occurrence["safety_ruleset_version"] != self.safety_ruleset_version
                    or occurrence["safety_ruleset_fingerprint"] != self.safety_ruleset_fingerprint
                )
            )
            if ordinary_invalid or safety_invalid:
                if occurrence is not None:
                    connection.execute(
                        "UPDATE reminder_attempt SET delivery_status='expired', updated_at=? "
                        "WHERE attempt_id=? AND delivery_status='queued'",
                        (iso(now_utc()), payload["attempt_id"]),
                    )
                if safety_invalid:
                    blocked = self._event(
                        "medication.safety.blocked", "scheduler",
                        elder_id=event.get("elder_id"), plan_id=event.get("plan_id"),
                        occurrence_id=event.get("occurrence_id"),
                        payload={
                            "reason": "scheduler_safety_gate",
                            "check_id": occurrence["safety_check_id"],
                            "plan_version": occurrence["plan_version"],
                            "ruleset_version": self.safety_ruleset_version,
                            "ruleset_fingerprint": self.safety_ruleset_fingerprint,
                            "stored_ruleset_version": occurrence["safety_ruleset_version"],
                            "stored_ruleset_fingerprint": occurrence["safety_ruleset_fingerprint"],
                            "source_event_id": event["event_id"],
                        },
                    )
                    self._audit_and_enqueue(
                        connection, blocked,
                        "safety_scheduler_blocked:%s" % event["event_id"],
                    )
                else:
                    stale = self._event(
                        "medication.reminder.discarded", "device_adapter",
                        elder_id=event.get("elder_id"), plan_id=event.get("plan_id"),
                        occurrence_id=event.get("occurrence_id"),
                        payload={
                            "reason": "occurrence_or_plan_not_valid",
                            "source_event_id": event["event_id"],
                        },
                    )
                    self._audit_and_enqueue(
                        connection, stale,
                        "reminder_discarded:%s" % event["event_id"],
                    )
                return
            trace_id = str(payload.get("trace_id") or event.get("trace_id") or "interaction:%s" % payload["interaction_id"])
            request = self._event(
                "device.interaction.request", "medication_service",
                elder_id=event.get("elder_id"), plan_id=event.get("plan_id"),
                occurrence_id=event.get("occurrence_id"), payload={
                    "interaction_id": payload["interaction_id"],
                    "occurrence_id": payload["occurrence_id"],
                    "attempt_id": payload["attempt_id"],
                    "device_sn": payload.get("device_sn"),
                    "interaction_type": "medication_reminder",
                    "trace_id": trace_id,
                    "plan_id": payload.get("plan_id") or event.get("plan_id"),
                    "scheduled_at": payload.get("scheduled_at") or occurrence["scheduled_at"],
                    "expires_at": payload.get("expires_at") or occurrence["confirmation_deadline_at"],
                    "medication": payload.get("medication") or {
                        "name": occurrence["drug_name_snapshot"],
                        "dose": occurrence["dosage_snapshot"],
                        "instruction": occurrence["relation_to_meal_snapshot"],
                    },
                    "reminder_text": payload.get("reminder_text") or payload.get("text") or self._reminder_text(occurrence),
                    "text": payload.get("text") or payload.get("reminder_text") or self._reminder_text(occurrence),
                    "source_event_id": event["event_id"],
                },
                trace_id=trace_id,
            )
            self._audit_and_enqueue(
                connection, request,
                "device_request:%s" % payload["interaction_id"],
            )

    def _publish_device_request(self, event):
        payload = event["payload"]
        self.device_adapter.dispatch(event)
        with self.storage.transaction() as connection:
            connection.execute(
                """UPDATE reminder_attempt SET delivery_status='dispatched', updated_at=?
                   WHERE attempt_id=? AND delivery_status='queued'""",
                (iso(now_utc()), payload.get("attempt_id")),
            )

    def _escalation_item(self, row, include_steps=False):
        item = row_dict(row)
        item["needs_manual_review"] = bool(item.get("needs_manual_review", 0))
        item["late_verified_taken"] = bool(item.get("late_verified_taken_at"))
        item["medication"] = {
            "name": item.get("drug_name_snapshot"),
            "dose": item.get("dosage_snapshot"),
            "instruction": item.get("relation_to_meal_snapshot"),
        }
        if include_steps:
            steps = self.storage.fetchall(
                """SELECT * FROM medication_escalation_step
                   WHERE escalation_id=? ORDER BY scheduled_at, step_id""",
                (item["escalation_id"],),
            )
            item["steps"] = [row_dict(step) for step in steps]
        return item

    def list_escalations(self, elder_id=None, status=None, level=None, limit=100):
        clauses = []
        parameters = []
        if elder_id:
            clauses.append("e.elder_id=?")
            parameters.append(elder_id)
        if status:
            status = str(status).upper()
            if status not in ALL_STATUSES:
                raise DomainError("unsupported escalation status")
            clauses.append("e.status=?")
            parameters.append(status)
        if level:
            level = str(level).upper()
            if level not in ALL_LEVELS:
                raise DomainError("unsupported escalation level")
            clauses.append("e.current_level=?")
            parameters.append(level)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        parameters.append(min(max(int(limit), 1), 1000))
        rows = self.storage.fetchall(
            """SELECT e.*, o.scheduled_at, o.drug_name_snapshot,
                      o.dosage_snapshot, o.relation_to_meal_snapshot,
                      o.late_verified_taken_at, o.late_verified_by,
                      o.late_verified_source, o.late_verified_note,
                      o.intake_status
               FROM medication_escalation e
               JOIN medication_occurrence o ON o.occurrence_id=e.occurrence_id
               %s ORDER BY e.opened_at DESC LIMIT ?""" % where,
            parameters,
        )
        return [self._escalation_item(row) for row in rows]

    def get_escalation(self, escalation_id):
        row = self.storage.fetchone(
            """SELECT e.*, o.scheduled_at, o.drug_name_snapshot,
                      o.dosage_snapshot, o.relation_to_meal_snapshot,
                      o.late_verified_taken_at, o.late_verified_by,
                      o.late_verified_source, o.late_verified_note,
                      o.intake_status
               FROM medication_escalation e
               JOIN medication_occurrence o ON o.occurrence_id=e.occurrence_id
               WHERE e.escalation_id=?""",
            (escalation_id,),
        )
        if row is None:
            raise DomainError("escalation not found", 404)
        return self._escalation_item(row, include_steps=True)

    def escalation_summary(self, elder_id=None):
        items = self.list_escalations(elder_id=elder_id, limit=1000)
        by_level = {level: 0 for level in ALL_LEVELS}
        ack_durations = []
        resolve_durations = []
        for item in items:
            by_level[item["current_level"]] = by_level.get(item["current_level"], 0) + 1
            if item.get("acknowledged_at"):
                try:
                    ack_durations.append(
                        (parse_datetime(item["acknowledged_at"]) - parse_datetime(item["opened_at"])).total_seconds()
                    )
                except DomainError:
                    pass
            if item.get("resolved_at"):
                try:
                    resolve_durations.append(
                        (parse_datetime(item["resolved_at"]) - parse_datetime(item["opened_at"])).total_seconds()
                    )
                except DomainError:
                    pass
        average_ack = round(sum(ack_durations) / len(ack_durations), 3) if ack_durations else None
        average_resolve = round(sum(resolve_durations) / len(resolve_durations), 3) if resolve_durations else None
        return {
            "open_escalations": sum(item["status"] == STATUS_OPEN for item in items),
            "acknowledged_escalations": sum(item["status"] == STATUS_ACKNOWLEDGED for item in items),
            "resolved_escalations": sum(item["status"] == STATUS_RESOLVED for item in items),
            "escalation_by_level": by_level,
            "average_time_to_ack": average_ack,
            "average_time_to_resolve": average_resolve,
        }

    def _actor_fields(self, data):
        actor_id = str(data.get("actor_id") or "").strip()
        actor_role = str(data.get("actor_role") or "").strip()
        if not actor_id or not actor_role:
            raise DomainError("actor_id and actor_role are required")
        return actor_id, actor_role

    def acknowledge_escalation(self, escalation_id, data=None):
        data = dict(data or {})
        actor_id, actor_role = self._actor_fields(data)
        event_id = str(data.get("event_id") or new_id("evt"))
        clock = now_utc()
        occurred_at = parse_datetime(data["occurred_at"]) if data.get("occurred_at") else clock
        duplicate = False
        with self.storage.transaction() as connection:
            existing_event = connection.execute(
                "SELECT event_type FROM medication_event_log WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if existing_event is not None:
                if existing_event["event_type"] != "medication.escalation.acknowledged":
                    raise DomainError("event_id is already used by another event", 409)
                duplicate = True
            else:
                escalation = connection.execute(
                    "SELECT * FROM medication_escalation WHERE escalation_id=?",
                    (escalation_id,),
                ).fetchone()
                if escalation is None:
                    raise DomainError("escalation not found", 404)
                if escalation["status"] != STATUS_OPEN:
                    raise DomainError("only OPEN escalations can be acknowledged", 409)
                timestamp = iso(clock)
                ack_timeout_minutes = max(
                    int(self.config.get("escalation_ack_resolution_timeout_minutes", 30)),
                    0,
                )
                resolution_deadline = (
                    iso(clock + timedelta(minutes=ack_timeout_minutes))
                    if next_level(escalation["current_level"]) is not None else None
                )
                cursor = connection.execute(
                    """UPDATE medication_escalation
                       SET status=?, acknowledged_at=?, acknowledged_by=?,
                           next_escalation_at=NULL, resolution_deadline_at=?,
                           updated_at=?
                       WHERE escalation_id=? AND status=?""",
                    (
                        STATUS_ACKNOWLEDGED, timestamp, actor_id,
                        resolution_deadline, timestamp, escalation_id, STATUS_OPEN,
                    ),
                )
                if cursor.rowcount != 1:
                    raise DomainError("escalation state changed concurrently", 409)
                payload = {
                    "escalation_id": escalation_id,
                    "elder_id": escalation["elder_id"],
                    "occurrence_id": escalation["occurrence_id"],
                    "plan_id": escalation["plan_id"],
                    "level": escalation["current_level"],
                    "reason": escalation["reason"],
                    "resolution_deadline_at": resolution_deadline,
                    "actor_id": actor_id,
                    "actor_role": actor_role,
                    "trace_id": "escalation:%s" % escalation_id,
                }
                event = self._event(
                    "medication.escalation.acknowledged",
                    "m6_escalation",
                    elder_id=escalation["elder_id"],
                    plan_id=escalation["plan_id"],
                    occurrence_id=escalation["occurrence_id"],
                    payload=payload,
                    occurred_at=occurred_at,
                    event_id=event_id,
                    trace_id=payload["trace_id"],
                )
                self._audit_and_enqueue(
                    connection, event,
                    "escalation_ack:%s:%s" % (escalation_id, event_id),
                )
                connection.execute(
                    """UPDATE medication_escalation_step
                       SET status='acknowledged'
                       WHERE escalation_id=? AND level=?
                         AND status IN ('queued', 'dispatched', 'simulated', 'delivered')""",
                    (escalation_id, escalation["current_level"]),
                )
        return {
            "duplicate": duplicate,
            "escalation": self.get_escalation(escalation_id),
        }

    def resolve_escalation(self, escalation_id, data=None):
        data = dict(data or {})
        actor_id, actor_role = self._actor_fields(data)
        resolution_code = str(data.get("resolution_code") or "").strip().upper()
        if resolution_code not in RESOLUTION_CODES:
            raise DomainError("unsupported resolution_code")
        resolution_note = str(data.get("resolution_note") or "").strip() or None
        event_id = str(data.get("event_id") or new_id("evt"))
        clock = now_utc()
        occurred_at = parse_datetime(data["occurred_at"]) if data.get("occurred_at") else clock
        duplicate = False
        with self.storage.transaction() as connection:
            existing_event = connection.execute(
                "SELECT event_type FROM medication_event_log WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if existing_event is not None:
                if existing_event["event_type"] != "medication.escalation.resolved":
                    raise DomainError("event_id is already used by another event", 409)
                duplicate = True
            else:
                escalation = connection.execute(
                    "SELECT * FROM medication_escalation WHERE escalation_id=?",
                    (escalation_id,),
                ).fetchone()
                if escalation is None:
                    raise DomainError("escalation not found", 404)
                if escalation["status"] != STATUS_ACKNOWLEDGED:
                    raise DomainError("only ACKNOWLEDGED escalations can be resolved", 409)
                timestamp = iso(clock)
                cursor = connection.execute(
                    """UPDATE medication_escalation
                       SET status=?, resolved_at=?, resolved_by=?,
                           resolution_code=?, resolution_note=?,
                           next_escalation_at=NULL, resolution_deadline_at=NULL,
                           updated_at=?
                       WHERE escalation_id=? AND status=?""",
                    (
                        STATUS_RESOLVED, timestamp, actor_id, resolution_code,
                        resolution_note, timestamp, escalation_id, STATUS_ACKNOWLEDGED,
                    ),
                )
                if cursor.rowcount != 1:
                    raise DomainError("escalation state changed concurrently", 409)
                payload = {
                    "escalation_id": escalation_id,
                    "elder_id": escalation["elder_id"],
                    "occurrence_id": escalation["occurrence_id"],
                    "plan_id": escalation["plan_id"],
                    "level": escalation["current_level"],
                    "reason": escalation["reason"],
                    "actor_id": actor_id,
                    "actor_role": actor_role,
                    "resolution_code": resolution_code,
                    "resolution_note": resolution_note,
                    "trace_id": "escalation:%s" % escalation_id,
                }
                event = self._event(
                    "medication.escalation.resolved",
                    "m6_escalation",
                    elder_id=escalation["elder_id"],
                    plan_id=escalation["plan_id"],
                    occurrence_id=escalation["occurrence_id"],
                    payload=payload,
                    occurred_at=occurred_at,
                    event_id=event_id,
                    trace_id=payload["trace_id"],
                )
                self._audit_and_enqueue(
                    connection, event,
                    "escalation_resolve:%s:%s" % (escalation_id, event_id),
                )
                connection.execute(
                    """UPDATE medication_escalation_step
                       SET status='resolved'
                       WHERE escalation_id=? AND level=?
                         AND status IN ('queued', 'dispatched', 'simulated', 'delivered', 'acknowledged')""",
                    (escalation_id, escalation["current_level"]),
                )
        return {
            "duplicate": duplicate,
            "escalation": self.get_escalation(escalation_id),
        }

    def cancel_escalation(self, escalation_id, data=None):
        data = dict(data or {})
        actor_id, actor_role = self._actor_fields(data)
        cancel_reason = str(data.get("cancel_reason") or data.get("reason") or "").strip()
        if not cancel_reason:
            raise DomainError("cancel_reason is required")
        event_id = str(data.get("event_id") or new_id("evt"))
        clock = now_utc()
        occurred_at = parse_datetime(data["occurred_at"]) if data.get("occurred_at") else clock
        duplicate = False
        with self.storage.transaction() as connection:
            existing_event = connection.execute(
                "SELECT event_type FROM medication_event_log WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if existing_event is not None:
                if existing_event["event_type"] != "medication.escalation.cancelled":
                    raise DomainError("event_id is already used by another event", 409)
                duplicate = True
            else:
                escalation = connection.execute(
                    "SELECT * FROM medication_escalation WHERE escalation_id=?",
                    (escalation_id,),
                ).fetchone()
                if escalation is None:
                    raise DomainError("escalation not found", 404)
                if escalation["status"] not in (STATUS_OPEN, STATUS_ACKNOWLEDGED):
                    raise DomainError("only OPEN or ACKNOWLEDGED escalations can be cancelled", 409)
                timestamp = iso(clock)
                cursor = connection.execute(
                    """UPDATE medication_escalation
                       SET status=?, next_escalation_at=NULL,
                           resolution_deadline_at=NULL, updated_at=?
                       WHERE escalation_id=? AND status IN (?, ?)""",
                    (
                        STATUS_CANCELLED, timestamp, escalation_id,
                        STATUS_OPEN, STATUS_ACKNOWLEDGED,
                    ),
                )
                if cursor.rowcount != 1:
                    raise DomainError("escalation state changed concurrently", 409)
                payload = {
                    "escalation_id": escalation_id,
                    "elder_id": escalation["elder_id"],
                    "occurrence_id": escalation["occurrence_id"],
                    "plan_id": escalation["plan_id"],
                    "level": escalation["current_level"],
                    "reason": escalation["reason"],
                    "cancel_reason": cancel_reason,
                    "actor_id": actor_id,
                    "actor_role": actor_role,
                    "trace_id": "escalation:%s" % escalation_id,
                }
                event = self._event(
                    "medication.escalation.cancelled",
                    "m6_escalation",
                    elder_id=escalation["elder_id"],
                    plan_id=escalation["plan_id"],
                    occurrence_id=escalation["occurrence_id"],
                    payload=payload,
                    occurred_at=occurred_at,
                    event_id=event_id,
                    trace_id=payload["trace_id"],
                )
                self._audit_and_enqueue(
                    connection, event,
                    "escalation_cancel:%s:%s" % (escalation_id, event_id),
                )
                connection.execute(
                    """UPDATE medication_escalation_step SET status='cancelled'
                       WHERE escalation_id=?
                         AND status IN ('queued', 'dispatched', 'simulated', 'delivered', 'acknowledged')""",
                    (escalation_id,),
                )
        return {
            "duplicate": duplicate,
            "escalation": self.get_escalation(escalation_id),
        }

    # ---------- queries ----------
    def record_manual_confirmation(self, occurrence_id, data=None):
        """Record an operator report through M5, including late verification."""

        data = dict(data or {})
        actor_id, actor_role = self._actor_fields(data)
        event_id = str(data.get("event_id") or new_id("evt"))
        occurred_at = parse_datetime(data["occurred_at"]) if data.get("occurred_at") else now_utc()
        value = {
            "confirmation_method": str(
                data.get("confirmation_method") or "manual"
            ).strip() or "manual",
            "late_verified_source": str(
                data.get("late_verified_source") or data.get("source") or actor_role
            ).strip(),
            "late_verified_note": str(
                data.get("late_verified_note") or data.get("note") or ""
            ).strip() or None,
        }
        if isinstance(data.get("value"), dict):
            value.update(data["value"])
        with self.storage.transaction() as connection:
            current = connection.execute(
                "SELECT intake_status FROM medication_occurrence WHERE occurrence_id=?",
                (occurrence_id,),
            ).fetchone()
            if current is None:
                raise DomainError("occurrence not found", 404)
            existing_evidence = connection.execute(
                "SELECT evidence_id FROM medication_evidence WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if existing_evidence is None and current["intake_status"] not in (UNCONFIRMED, "closed_unconfirmed"):
                raise DomainError("occurrence is already in terminal state", 409)
            result = self._record_evidence_in_transaction(
                connection,
                dict(
                    data,
                    event_id=event_id,
                    occurrence_id=occurrence_id,
                    source_type="MANUAL_OPERATOR",
                    evidence_type="MANUAL_REPORTED_TAKEN",
                    observed_at=iso(occurred_at),
                    value=value,
                    actor_id=actor_id,
                    actor_role=actor_role,
                    trace_id=str(data.get("trace_id") or "occurrence:%s" % occurrence_id),
                ),
                assess=True,
                identity_trusted=False,
                source_trusted=False,
            )
        return {
            "duplicate": result["duplicate"],
            "evidence": self._hydrate_evidence(result["evidence"]),
            "assessment": self._hydrate_assessment(result["assessment"]),
            "late": bool(result["late"]),
            "occurrence": self.get_occurrence(occurrence_id),
        }

    # ---------- queries ----------

    def get_open_interactions(self, elder_id, now=None):
        """Return all unexpired interactions for one elder, newest first."""
        clock = now or now_utc()
        rows = self.storage.fetchall(
            """SELECT * FROM medication_interaction
               WHERE elder_id=? AND status='open' AND expires_at > ?
               ORDER BY opened_at DESC""",
            (elder_id, iso(clock)),
        )
        return [row_dict(row) for row in rows]

    def get_open_interaction(self, elder_id, now=None, interaction_id=None):
        """Return one unexpired interaction, optionally by trusted ID."""
        clock = now or now_utc()
        if interaction_id:
            row = self.storage.fetchone(
                """SELECT * FROM medication_interaction
                   WHERE elder_id=? AND interaction_id=?
                     AND status='open' AND expires_at > ?""",
                (elder_id, interaction_id, iso(clock)),
            )
        else:
            row = self.storage.fetchone(
                """SELECT * FROM medication_interaction
                   WHERE elder_id=? AND status='open' AND expires_at > ?
                   ORDER BY opened_at DESC LIMIT 1""",
                (elder_id, iso(clock)),
            )
        return row_dict(row)
    def get_interaction(self, elder_id, interaction_id):
        """Return an exact interaction binding, including closed late ones."""

        if not elder_id or not interaction_id:
            return None
        row = self.storage.fetchone(
            """SELECT i.*, o.intake_status, o.confirmation_deadline_at
               FROM medication_interaction i
               JOIN medication_occurrence o ON o.occurrence_id=i.occurrence_id
               WHERE i.elder_id=? AND i.interaction_id=?""",
            (elder_id, interaction_id),
        )
        return row_dict(row)


    def _related_occurrence_rows(self, table, occurrence_ids):
        """Fetch occurrence children in bounded batches to avoid N+1 queries."""
        if not occurrence_ids:
            return {}
        grouped = {occurrence_id: [] for occurrence_id in occurrence_ids}
        for offset in range(0, len(occurrence_ids), 500):
            batch = occurrence_ids[offset:offset + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = self.storage.fetchall(
                "SELECT * FROM %s WHERE occurrence_id IN (%s) ORDER BY created_at" % (
                    table, placeholders
                ),
                batch,
            )
            for row in rows:
                grouped[row["occurrence_id"]].append(row_dict(row))
        return grouped

    def _confirmation_summaries(self, occurrence_ids):
        if not occurrence_ids:
            return {}
        summaries = {
            occurrence_id: {
                "confirmation_basis": None,
                "confirmation_result": None,
                "evidence_count": 0,
                "evidence_conflict": False,
                "review_required": False,
            }
            for occurrence_id in occurrence_ids
        }
        for offset in range(0, len(occurrence_ids), 500):
            batch = occurrence_ids[offset:offset + 500]
            placeholders = ",".join("?" for _ in batch)
            evidence_rows = self.storage.fetchall(
                "SELECT occurrence_id, COUNT(*) AS evidence_count "
                "FROM medication_evidence WHERE occurrence_id IN (%s) "
                "GROUP BY occurrence_id" % placeholders,
                batch,
            )
            for row in evidence_rows:
                summaries[row["occurrence_id"]]["evidence_count"] = int(row["evidence_count"])
            assessment_rows = self.storage.fetchall(
                "SELECT * FROM medication_confirmation_assessment "
                "WHERE occurrence_id IN (%s) ORDER BY assessed_at, created_at, assessment_id"
                % placeholders,
                batch,
            )
            for row in assessment_rows:
                latest = self._hydrate_assessment(row)
                summaries[row["occurrence_id"]].update({
                    "confirmation_basis": latest.get("basis"),
                    "confirmation_result": latest.get("result"),
                    "evidence_conflict": bool(latest.get("conflict_detected")),
                    "review_required": bool(latest.get("review_required")),
                })
        return summaries

    def _occurrences_with_details(self, rows):
        rows = list(rows)
        occurrence_ids = [row["occurrence_id"] for row in rows]
        attempts = self._related_occurrence_rows("reminder_attempt", occurrence_ids)
        interactions = self._related_occurrence_rows("medication_interaction", occurrence_ids)
        confirmations = self._confirmation_summaries(occurrence_ids)
        result = []
        for row in rows:
            item = row_dict(row)
            occurrence_id = item["occurrence_id"]
            item["reminder_attempts"] = attempts.get(occurrence_id, [])
            item["interactions"] = interactions.get(occurrence_id, [])
            item["late_verified_taken"] = bool(item.get("late_verified_taken_at"))
            snapshot = item.get("schedule_snapshot_json")
            item.update(confirmations.get(occurrence_id, {}))
            if snapshot:
                try:
                    item["schedule_snapshot"] = json.loads(snapshot)
                except (TypeError, ValueError, json.JSONDecodeError):
                    item["schedule_snapshot"] = {}
            else:
                item["schedule_snapshot"] = {}
            result.append(item)
        return result

    def get_occurrence(self, occurrence_id):
        row = self.storage.fetchone(
            "SELECT * FROM medication_occurrence WHERE occurrence_id=?", (occurrence_id,)
        )
        if row is None:
            raise DomainError("occurrence not found", 404)
        return self._occurrences_with_details([row])[0]

    def get_today(self, elder_id, day=None):
        if not elder_id:
            raise DomainError("elder_id is required")
        target = parse_date(day, "date") if day else now_utc().astimezone(SHANGHAI).date()
        start = datetime.combine(target, time.min, tzinfo=SHANGHAI).astimezone(UTC)
        end = start + timedelta(days=1)
        rows = self.storage.fetchall(
            """SELECT * FROM medication_occurrence
               WHERE elder_id=? AND scheduled_at>=? AND scheduled_at<?
               ORDER BY scheduled_at""",
            (elder_id, iso(start), iso(end)),
        )
        return self._occurrences_with_details(rows)

    def get_dashboard(self, elder_id, day=None, event_limit=30):
        """Run independent read models concurrently for the management UI."""
        if not elder_id:
            raise DomainError("elder_id is required")
        futures = {
            "today": self.read_executor.submit(self.get_today, elder_id, day),
            "plans": self.read_executor.submit(self.list_plans, elder_id),
            "events": self.read_executor.submit(
                self.list_event_log, None, None, event_limit, elder_id
            ),
            "notifications": self.read_executor.submit(
                self.list_active_notifications, elder_id
            ),
            "escalations": self.read_executor.submit(
                self.list_escalations, elder_id
            ),
        }
        return {name: {"items": future.result()} for name, future in futures.items()}

    def list_active_notifications(self, elder_id, now=None):
        """Return open, unexpired reminder interactions for the Web channel."""
        if not elder_id:
            raise DomainError("elder_id is required")
        clock = now or now_utc()
        rows = self.storage.fetchall(
            """SELECT i.interaction_id, i.elder_id, i.device_sn, i.occurrence_id,
                      i.opened_at, i.expires_at, i.status,
                      o.plan_id, o.plan_version, o.scheduled_at,
                      o.drug_name_snapshot, o.dosage_snapshot,
                      o.relation_to_meal_snapshot, o.intake_status
               FROM medication_interaction i
               JOIN medication_occurrence o ON o.occurrence_id=i.occurrence_id
               WHERE i.elder_id=? AND i.status='open' AND i.expires_at > ?
                 AND o.intake_status='unconfirmed'
               ORDER BY i.opened_at ASC""",
            (elder_id, iso(clock)),
        )
        result = []
        for row in rows:
            item = row_dict(row)
            item["notification_id"] = item["interaction_id"]
            item["text"] = self._reminder_text(row)
            result.append(item)
        return result

    def list_event_log(self, event_type=None, occurrence_id=None, limit=100, elder_id=None):
        clauses = []
        parameters = []
        if event_type:
            clauses.append("event_type=?")
            parameters.append(event_type)
        if occurrence_id:
            clauses.append("occurrence_id=?")
            parameters.append(occurrence_id)
        if elder_id:
            clauses.append("elder_id=?")
            parameters.append(elder_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        parameters.append(min(max(int(limit), 1), 1000))
        rows = self.storage.fetchall(
            "SELECT * FROM medication_event_log%s ORDER BY log_id DESC LIMIT ?" % where,
            parameters,
        )
        result = []
        for row in rows:
            item = row_dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def list_outbox(self, status=None, limit=100):
        if status:
            rows = self.storage.fetchall(
                "SELECT * FROM domain_outbox WHERE status=? ORDER BY created_at LIMIT ?",
                (status, int(limit)),
            )
        else:
            rows = self.storage.fetchall(
                "SELECT * FROM domain_outbox ORDER BY created_at LIMIT ?", (int(limit),)
            )
        result = []
        for row in rows:
            item = row_dict(row)
            item["event"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

