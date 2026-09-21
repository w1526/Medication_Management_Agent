"""Minimal, explicit elder routine anchors used by deterministic schedules.

The routine is intentionally small.  It is context supplied by a caregiver or
clinician; it is never inferred from a language model and it is never filled
with product defaults such as ``breakfast=08:00``.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timezone, timedelta
import re


SUPPORTED_TIMEZONE = "Asia/Shanghai"
SHANGHAI = timezone(timedelta(hours=8), SUPPORTED_TIMEZONE)


class RoutineError(ValueError):
    """Raised when an explicit routine value is malformed."""


def normalize_clock_time(value, field_name="time"):
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    match = re.fullmatch(r"(\d{2}):(\d{2})", text)
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise RoutineError("%s must be HH:MM" % field_name)
    return text


@dataclass(frozen=True)
class ElderRoutine:
    elder_id: str
    breakfast_time: str = None
    lunch_time: str = None
    dinner_time: str = None
    bedtime: str = None
    timezone: str = SUPPORTED_TIMEZONE
    updated_at: str = None
    routine_version: int = 1

    def __post_init__(self):
        if not str(self.elder_id or "").strip():
            raise RoutineError("elder_id is required")
        if self.timezone != SUPPORTED_TIMEZONE:
            raise RoutineError("only timezone=%s is supported" % SUPPORTED_TIMEZONE)
        try:
            version = int(self.routine_version or 1)
        except (TypeError, ValueError) as exc:
            raise RoutineError("routine_version must be an integer") from exc
        if version < 1:
            raise RoutineError("routine_version must be >= 1")
        object.__setattr__(self, "routine_version", version)
        for field_name in ("breakfast_time", "lunch_time", "dinner_time", "bedtime"):
            normalized = normalize_clock_time(getattr(self, field_name), field_name)
            object.__setattr__(self, field_name, normalized)

    @classmethod
    def from_mapping(cls, value, elder_id=None):
        if isinstance(value, cls):
            return value
        value = dict(value or {})
        return cls(
            elder_id=str(value.get("elder_id") or elder_id or "").strip(),
            breakfast_time=value.get("breakfast_time"),
            lunch_time=value.get("lunch_time"),
            dinner_time=value.get("dinner_time"),
            bedtime=value.get("bedtime"),
            timezone=value.get("timezone") or SUPPORTED_TIMEZONE,
            routine_version=value.get("routine_version", 1) or 1,
            updated_at=value.get("updated_at"),
        )

    def to_dict(self):
        return {
            "elder_id": self.elder_id,
            "breakfast_time": self.breakfast_time,
            "lunch_time": self.lunch_time,
            "dinner_time": self.dinner_time,
            "bedtime": self.bedtime,
            "timezone": self.timezone,
            "routine_version": self.routine_version,
            "updated_at": self.updated_at,
        }

    def anchor_clock(self, anchor):
        anchor = str(anchor or "").strip().upper()
        names = {
            "BREAKFAST": "breakfast_time",
            "LUNCH": "lunch_time",
            "DINNER": "dinner_time",
            "BEDTIME": "bedtime",
        }
        field_name = names.get(anchor)
        if field_name is None:
            raise RoutineError("unsupported routine anchor: %s" % anchor)
        value = getattr(self, field_name)
        if not value:
            raise RoutineError("routine anchor is missing: %s" % field_name)
        hour, minute = (int(item) for item in value.split(":"))
        return time(hour, minute)

    def anchor_at(self, anchor, on_date):
        if isinstance(on_date, datetime):
            on_date = on_date.date()
        if not isinstance(on_date, date):
            raise RoutineError("routine anchor date is invalid")
        return datetime.combine(on_date, self.anchor_clock(anchor), tzinfo=SHANGHAI)


__all__ = [
    "ElderRoutine",
    "RoutineError",
    "SUPPORTED_TIMEZONE",
    "SHANGHAI",
    "normalize_clock_time",
]
