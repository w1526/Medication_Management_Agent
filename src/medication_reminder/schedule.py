"""Deterministic medication schedule validation and occurrence expansion.

This module is deliberately independent from the application service.  A
semantic agent may produce the input mapping, but this module is the only
place that interprets schedule times and dates.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import json
import math
import re

from .routine import ElderRoutine, RoutineError, SHANGHAI, SUPPORTED_TIMEZONE


class ScheduleType:
    FIXED_TIME = "FIXED_TIME"
    MEAL_RELATION = "MEAL_RELATION"
    INTERVAL = "INTERVAL"
    WEEKLY = "WEEKLY"
    CYCLE = "CYCLE"
    PRN = "PRN"
    ROUTINE_RELATION = "ROUTINE_RELATION"


AUTO_SCHEDULE_TYPES = frozenset({
    ScheduleType.FIXED_TIME,
    ScheduleType.MEAL_RELATION,
    ScheduleType.INTERVAL,
    ScheduleType.WEEKLY,
    ScheduleType.CYCLE,
    ScheduleType.ROUTINE_RELATION,
})
SUPPORTED_SCHEDULE_TYPES = AUTO_SCHEDULE_TYPES | {ScheduleType.PRN}


class ScheduleError(ValueError):
    """A deterministic schedule validation or expansion failure."""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.details = dict(details or {})


def _invalid(message, details=None):
    return ScheduleError("SCHEDULE_INVALID", message, details)


def _context_missing(message, details=None):
    return ScheduleError("SCHEDULE_CONTEXT_MISSING", message, details)


def _unsupported(message, details=None):
    return ScheduleError("SCHEDULE_UNSUPPORTED", message, details)


def _timezone_for(value):
    if value is None:
        return SHANGHAI
    if hasattr(value, "utcoffset"):
        return value
    if str(value) == SUPPORTED_TIMEZONE:
        return SHANGHAI
    raise _invalid("only timezone=%s is supported" % SUPPORTED_TIMEZONE,
                   {"timezone": value})


def _clock(value, field_name="time"):
    if value is None or str(value).strip() == "":
        raise _invalid("%s is required" % field_name, {"field": field_name})
    text = str(value).strip()
    match = re.fullmatch(r"(\d{2}):(\d{2})", text)
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise _invalid("%s must be HH:MM" % field_name, {field_name: value})
    return text


def _sorted_times(value, field_name="times"):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)) or not value:
        raise _invalid("%s must be a non-empty array" % field_name,
                       {"field": field_name})
    normalized = [_clock(item, field_name) for item in value]
    if len(set(normalized)) != len(normalized):
        raise _invalid("%s must contain unique times" % field_name,
                       {"field": field_name})
    return sorted(normalized)


def _integer(value, field_name, minimum=None, maximum=None):
    if isinstance(value, bool):
        raise _invalid("%s must be an integer" % field_name, {"field": field_name})
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise _invalid("%s must be an integer" % field_name,
                       {"field": field_name}) from exc
    if minimum is not None and result < minimum:
        raise _invalid("%s must be >= %s" % (field_name, minimum),
                       {"field": field_name, "minimum": minimum})
    if maximum is not None and result > maximum:
        raise _invalid("%s must be <= %s" % (field_name, maximum),
                       {"field": field_name, "maximum": maximum})
    return result


def _iso_date(value, field_name):
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise _invalid("%s must be YYYY-MM-DD" % field_name,
                       {"field": field_name}) from exc


def _parse_datetime(value, tz, field_name="anchor_at"):
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise _invalid("%s must be an ISO datetime" % field_name,
                           {"field": field_name}) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _normalize_type(value):
    text = str(value or "").strip().upper()
    aliases = {
        "DAILY": ScheduleType.FIXED_TIME,
        "FIXED": ScheduleType.FIXED_TIME,
        "FIXEDTIME": ScheduleType.FIXED_TIME,
        "MEAL": ScheduleType.MEAL_RELATION,
        "ROUTINE": ScheduleType.ROUTINE_RELATION,
    }
    return aliases.get(text, text)


class ScheduleValidator:
    """Validate and canonicalize the single schedule contract."""

    @classmethod
    def validate(cls, schedule_type=None, schedule_config=None, **fields):
        config = dict(schedule_config or {})
        if schedule_type is None:
            schedule_type = config.pop("type", None) or fields.pop("type", None)
        else:
            config.pop("type", None)
        schedule_type = _normalize_type(schedule_type or ScheduleType.FIXED_TIME)
        if schedule_type not in SUPPORTED_SCHEDULE_TYPES:
            raise _unsupported("unsupported schedule type: %s" % schedule_type,
                               {"schedule_type": schedule_type})
        for key, value in fields.items():
            if value is not None and key not in config:
                config[key] = value

        if schedule_type == ScheduleType.FIXED_TIME:
            if "times" not in config and config.get("time") is not None:
                config["times"] = config.pop("time")
            return schedule_type, {"times": _sorted_times(config.get("times"))}

        if schedule_type == ScheduleType.MEAL_RELATION:
            meal = str(config.get("meal") or "").strip().upper()
            if meal not in ("BREAKFAST", "LUNCH", "DINNER"):
                raise _invalid("meal must be BREAKFAST, LUNCH or DINNER",
                               {"field": "meal"})
            relation = str(config.get("relation") or "").strip().upper()
            if relation not in ("BEFORE", "AFTER"):
                raise _invalid("relation must be BEFORE or AFTER",
                               {"field": "relation"})
            offset = _integer(config.get("offset_minutes"), "offset_minutes", 0)
            return schedule_type, {
                "meal": meal,
                "relation": relation,
                "offset_minutes": offset,
            }

        if schedule_type == ScheduleType.ROUTINE_RELATION:
            anchor = str(config.get("anchor") or "").strip().upper()
            if anchor != "BEDTIME":
                raise _invalid("routine anchor must be BEDTIME", {"field": "anchor"})
            relation = str(config.get("relation") or "").strip().upper()
            if relation not in ("BEFORE", "AFTER"):
                raise _invalid("relation must be BEFORE or AFTER",
                               {"field": "relation"})
            offset = _integer(config.get("offset_minutes"), "offset_minutes", 0)
            return schedule_type, {
                "anchor": anchor,
                "relation": relation,
                "offset_minutes": offset,
            }

        if schedule_type == ScheduleType.INTERVAL:
            interval = config.get("interval_hours")
            if interval is None:
                interval = config.get("hours")
            interval = _integer(interval, "interval_hours", 1, 168)
            anchor = config.get("anchor_at")
            if anchor is None or str(anchor).strip() == "":
                raise _invalid("anchor_at is required for INTERVAL",
                               {"field": "anchor_at"})
            anchor_text = str(anchor).strip()
            if re.fullmatch(r"\d{2}:\d{2}", anchor_text):
                _clock(anchor_text, "anchor_at")
                canonical_anchor = anchor_text
            else:
                canonical_anchor = _parse_datetime(anchor, SHANGHAI, "anchor_at").isoformat()
            return schedule_type, {
                "interval_hours": interval,
                "anchor_at": canonical_anchor,
            }

        if schedule_type == ScheduleType.WEEKLY:
            weekdays = config.get("weekdays")
            if not isinstance(weekdays, (list, tuple)) or not weekdays:
                raise _invalid("weekdays must be a non-empty array",
                               {"field": "weekdays"})
            normalized_days = [_integer(item, "weekday", 1, 7) for item in weekdays]
            if len(set(normalized_days)) != len(normalized_days):
                raise _invalid("weekdays must be unique", {"field": "weekdays"})
            return schedule_type, {
                "weekdays": sorted(normalized_days),
                "times": _sorted_times(config.get("times")),
            }

        if schedule_type == ScheduleType.CYCLE:
            start = config.get("cycle_start_date")
            if not start:
                raise _invalid("cycle_start_date is required", {"field": "cycle_start_date"})
            return schedule_type, {
                "cycle_start_date": _iso_date(start, "cycle_start_date").isoformat(),
                "days_on": _integer(config.get("days_on"), "days_on", 1),
                "days_off": _integer(config.get("days_off"), "days_off", 0),
                "times": _sorted_times(config.get("times")),
            }

        condition = str(config.get("condition_text") or "").strip()
        if not condition:
            raise _invalid("condition_text is required for PRN",
                           {"field": "condition_text"})
        return schedule_type, {"condition_text": condition}

    @classmethod
    def validate_plan(cls, data):
        data = dict(data or {})
        raw_config = data.get("schedule_config")
        if raw_config is None:
            raw_config = data.get("schedule")
        if raw_config is None:
            raw_config = {}
        if not isinstance(raw_config, dict):
            raise _invalid("schedule_config must be an object", {"field": "schedule_config"})
        config = dict(raw_config)
        schedule_type = data.get("schedule_type") or config.get("type")
        # Legacy payloads use schedule_time or time.  They are deliberately
        # normalized instead of rejected or silently assigned two times.
        if schedule_type is None:
            schedule_type = ScheduleType.FIXED_TIME
        if _normalize_type(schedule_type) == ScheduleType.FIXED_TIME:
            if "times" not in config:
                legacy_time = data.get("schedule_time", data.get("time"))
                if legacy_time is not None and str(legacy_time).strip():
                    config["times"] = [legacy_time]
        fields = {
            key: data.get(key)
            for key in (
                "times", "meal", "relation", "offset_minutes", "interval_hours",
                "hours", "anchor_at", "weekdays", "cycle_start_date", "days_on",
                "days_off", "condition_text", "anchor",
            )
            if data.get(key) is not None
        }
        normalized_type, normalized_config = cls.validate(
            schedule_type, config, **fields
        )
        return normalized_type, normalized_config


@dataclass(frozen=True)
class OccurrenceSpec:
    scheduled_at: datetime
    schedule_type: str
    schedule_snapshot: dict
    schedule_source: str
    timezone: str = SUPPORTED_TIMEZONE

    def __getitem__(self, key):
        return self.to_dict()[key]

    def to_dict(self):
        return {
            "scheduled_at": self.scheduled_at,
            "schedule_type": self.schedule_type,
            "schedule_snapshot": dict(self.schedule_snapshot),
            "schedule_source": self.schedule_source,
            "timezone": self.timezone,
        }


def _date_from_start(value, tz):
    if isinstance(value, datetime):
        return value.astimezone(tz).date(), False
    if isinstance(value, date):
        return value, True
    text = str(value or "").strip()
    if not text:
        raise _invalid("expansion start is required", {"field": "start"})
    if "T" in text or " " in text:
        return _parse_datetime(text, tz, "start").date(), False
    return _iso_date(text, "start"), True


class ScheduleExpander:
    """Expand a canonical schedule into timezone-aware occurrence specs.

    The requested expansion window and the plan effective range are both
    half-open intervals.  A plan's ``effective_from`` is preferred when it is
    present; legacy/date-only plans use ``start_date`` at local midnight.  A
    persisted ``end_date`` remains inclusive for compatibility and is mapped
    to the following local midnight as an exclusive ``effective_until``.
    """

    def __init__(self, default_timezone=SUPPORTED_TIMEZONE):
        self.default_timezone = default_timezone

    @staticmethod
    def _bound_datetime(value, tz, field_name):
        if value is None or str(value).strip() == "":
            return None
        if isinstance(value, datetime):
            parsed = value
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=tz)
            return parsed.astimezone(tz)
        text = str(value).strip()
        if "T" in text or " " in text:
            return _parse_datetime(text, tz, field_name)
        return datetime.combine(_iso_date(text, field_name), time.min, tzinfo=tz)

    @classmethod
    def _effective_bounds(cls, plan, tz):
        if not plan:
            return None, None
        effective_from = plan.get("effective_from") or plan.get("start_date")
        lower = cls._bound_datetime(effective_from, tz, "effective_from")

        explicit_until = plan.get("effective_until")
        if explicit_until:
            upper = cls._bound_datetime(explicit_until, tz, "effective_until")
        elif plan.get("end_date"):
            # Existing Plan API defines end_date as an inclusive calendar date.
            # Convert it once to the half-open upper bound used by expansion.
            end_date = _iso_date(plan["end_date"], "end_date")
            upper = datetime.combine(
                end_date + timedelta(days=1), time.min, tzinfo=tz
            )
        else:
            upper = None
        return lower, upper

    def expand(self, schedule=None, start=None, end=None, routine=None,
               timezone_value=None, plan=None, **kwargs):
        if schedule is None:
            schedule = {
                "schedule_type": kwargs.get("schedule_type"),
                "schedule_config": kwargs.get("schedule_config")
                    or kwargs.get("schedule"),
            }
        if plan is not None:
            plan = dict(plan)
            schedule = {
                "schedule_type": plan.get("schedule_type"),
                "schedule_config": plan.get("schedule_config")
                    or plan.get("schedule"),
            }
            if start is None:
                start = plan.get("start_date")
            if end is None and plan.get("end_date"):
                end = plan.get("end_date")
            if routine is None:
                routine = plan.get("routine")
            timezone_value = timezone_value or plan.get("timezone")
        tz = _timezone_for(
            timezone_value or kwargs.get("timezone") or self.default_timezone
        )
        if isinstance(schedule, dict):
            schedule_type = schedule.get("schedule_type") or schedule.get("type")
            config = schedule.get("schedule_config")
            if config is None:
                config = schedule.get("schedule")
            if config is None:
                config = schedule
        else:
            schedule_type, config = None, schedule
        schedule_type, config = ScheduleValidator.validate(schedule_type, config)
        start_value = start
        if start_value is None:
            start_value = kwargs.get("start_at") or kwargs.get("start_date")
        if start_value is None:
            raise _invalid("expansion start is required", {"field": "start"})

        if isinstance(start_value, datetime):
            window_start = start_value
            if window_start.tzinfo is None:
                window_start = window_start.replace(tzinfo=tz)
            window_start = window_start.astimezone(tz)
            date_mode = False
        else:
            start_date, date_mode = _date_from_start(start_value, tz)
            window_start = datetime.combine(start_date, time.min, tzinfo=tz)

        if end is None:
            end = kwargs.get("end_at") or kwargs.get("to")
        if end is None:
            horizon_days = kwargs.get("horizon_days")
            if horizon_days is None:
                horizon_days = 7
            horizon_days = _integer(horizon_days, "horizon_days", 1, 366)
            window_end = window_start + timedelta(days=horizon_days)
        elif isinstance(end, datetime):
            window_end = end
            if window_end.tzinfo is None:
                window_end = window_end.replace(tzinfo=tz)
            window_end = window_end.astimezone(tz)
        else:
            end_date = _iso_date(end, "end")
            window_end = datetime.combine(
                end_date + timedelta(days=1), time.min, tzinfo=tz
            )
        if window_end <= window_start:
            raise _invalid("expansion end must be after start")

        effective_from, effective_until = self._effective_bounds(plan, tz)
        if effective_from is not None:
            window_start = max(window_start, effective_from)
        if effective_until is not None:
            window_end = min(window_end, effective_until)
        if window_end <= window_start:
            return []

        routine_obj = None
        if routine is not None:
            try:
                routine_obj = ElderRoutine.from_mapping(routine)
            except RoutineError as exc:
                raise _context_missing(str(exc)) from exc

        # Date candidates include adjacent days for relation schedules.  The
        # final effective-range filter below is authoritative, so a candidate
        # that crosses before effective_from is generated only long enough to
        # be rejected consistently with every other schedule type.
        local_start_date = window_start.date()
        local_end_date = (window_end - timedelta(microseconds=1)).date()
        extra_day = schedule_type in (
            ScheduleType.MEAL_RELATION, ScheduleType.ROUTINE_RELATION
        )
        relation_days = 0
        if extra_day:
            relation_days = max(
                1, int(math.ceil(config.get("offset_minutes", 0) / 1440.0))
            )
        if date_mode:
            candidate_start = local_start_date
            candidate_end = local_end_date
        else:
            candidate_start = local_start_date - timedelta(days=relation_days)
            candidate_end = local_end_date + timedelta(days=relation_days)
        specs = []

        def add(value, resolved_context=None):
            if value.tzinfo is None:
                value = value.replace(tzinfo=tz)
            value = value.astimezone(tz)
            lower = window_start
            if date_mode and extra_day:
                # A date-based expansion names complete anchor dates.  Keep a
                # negative-offset result here for compatibility; the plan
                # effective-range filter below decides whether it is legal.
                lower = window_start - timedelta(days=max(1, relation_days))
            if value < lower or value >= window_end:
                return
            snapshot = {
                "type": schedule_type,
                "schedule_type": schedule_type,
                **dict(config),
            }
            if resolved_context:
                snapshot.update(resolved_context)
            snapshot.setdefault("resolved_local_datetime", value.isoformat())
            specs.append(OccurrenceSpec(
                scheduled_at=value,
                schedule_type=schedule_type,
                schedule_snapshot=snapshot,
                schedule_source=self._source(schedule_type, config),
                timezone=SUPPORTED_TIMEZONE,
            ))

        if schedule_type == ScheduleType.PRN:
            return []

        if schedule_type == ScheduleType.INTERVAL:
            anchor = config["anchor_at"]
            if re.fullmatch(r"\d{2}:\d{2}", anchor):
                hour, minute = (int(part) for part in anchor.split(":"))
                anchor_date = local_start_date
                if plan is not None and plan.get("start_date"):
                    anchor_date = _iso_date(plan["start_date"], "start_date")
                anchor_dt = datetime.combine(
                    anchor_date, time(hour, minute), tzinfo=tz
                )
            else:
                anchor_dt = _parse_datetime(anchor, tz)
            step = timedelta(hours=config["interval_hours"])
            if anchor_dt < window_start:
                elapsed = window_start - anchor_dt
                interval_index = int(math.ceil(
                    elapsed.total_seconds() / step.total_seconds()
                ))
            else:
                interval_index = 0
            current = anchor_dt + interval_index * step
            while current < window_end:
                add(current, {
                    "anchor_at": anchor,
                    "interval_hours": config["interval_hours"],
                    "interval_index": interval_index,
                })
                interval_index += 1
                current += step
            return self._filter_effective_range(
                self._unique_sorted(specs), effective_from, effective_until
            )

        times = config.get("times")
        for current_date in self._dates(candidate_start, candidate_end):
            if (
                schedule_type == ScheduleType.WEEKLY
                and current_date.isoweekday() not in config["weekdays"]
            ):
                continue
            if schedule_type == ScheduleType.CYCLE:
                cycle_start = _iso_date(config["cycle_start_date"], "cycle_start_date")
                days_since = (current_date - cycle_start).days
                cycle_length = config["days_on"] + config["days_off"]
                if days_since < 0 or days_since % cycle_length >= config["days_on"]:
                    continue
            if schedule_type in (
                ScheduleType.MEAL_RELATION, ScheduleType.ROUTINE_RELATION
            ):
                if routine_obj is None:
                    anchor = config.get("meal") or config.get("anchor")
                    raise _context_missing(
                        "routine anchor is required for %s" % anchor,
                        {"anchor": anchor},
                    )
                anchor_name = config.get("meal") or config.get("anchor")
                try:
                    anchor_clock = routine_obj.anchor_clock(anchor_name)
                    anchor_dt = routine_obj.anchor_at(
                        anchor_name, current_date
                    ).astimezone(tz)
                except RoutineError as exc:
                    raise _context_missing(str(exc), {
                        "anchor": anchor_name,
                    }) from exc
                offset = timedelta(minutes=config["offset_minutes"])
                value = (
                    anchor_dt - offset
                    if config["relation"] == "BEFORE"
                    else anchor_dt + offset
                )
                add(value, {
                    "resolved_anchor_type": str(anchor_name).upper(),
                    "resolved_anchor_time": anchor_clock.strftime("%H:%M"),
                    "resolved_anchor_local_datetime": anchor_dt.isoformat(),
                    "routine_version": routine_obj.routine_version,
                })
                continue
            for clock in times or []:
                hour, minute = (int(part) for part in clock.split(":"))
                value = datetime.combine(
                    current_date, time(hour, minute), tzinfo=tz
                )
                context = {"selected_time": clock}
                if schedule_type == ScheduleType.WEEKLY:
                    context["selected_weekday"] = current_date.isoweekday()
                elif schedule_type == ScheduleType.CYCLE:
                    cycle_start = _iso_date(
                        config["cycle_start_date"], "cycle_start_date"
                    )
                    cycle_length = config["days_on"] + config["days_off"]
                    cycle_day_index = (current_date - cycle_start).days % cycle_length
                    context.update({
                        "cycle_day": cycle_day_index + 1,
                        "cycle_day_index": cycle_day_index,
                    })
                add(value, context)
        return self._filter_effective_range(
            self._unique_sorted(specs), effective_from, effective_until
        )

    @staticmethod
    def _filter_effective_range(specs, effective_from, effective_until):
        """Apply one authoritative half-open Plan range to all candidates."""

        return [
            spec for spec in specs
            if (effective_from is None or spec.scheduled_at >= effective_from)
            and (effective_until is None or spec.scheduled_at < effective_until)
        ]

    @staticmethod
    def _dates(start_date, end_date):
        current = start_date
        while current <= end_date:
            yield current
            current += timedelta(days=1)

    @staticmethod
    def _source(schedule_type, config):
        return "%s:%s" % (
            schedule_type,
            json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )

    @staticmethod
    def _unique_sorted(specs):
        unique = {}
        for spec in specs:
            unique[(spec.scheduled_at.isoformat(), spec.schedule_type)] = spec
        return [unique[key] for key in sorted(unique)]

    def expand_plan(self, plan, start_at, end_at=None, routine=None):
        return self.expand(plan=plan, start=start_at, end=end_at, routine=routine)


__all__ = [
    "AUTO_SCHEDULE_TYPES",
    "OccurrenceSpec",
    "ScheduleError",
    "ScheduleExpander",
    "ScheduleType",
    "ScheduleValidator",
    "SUPPORTED_SCHEDULE_TYPES",
]
