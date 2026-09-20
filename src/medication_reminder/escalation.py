"""Deterministic M6 escalation policy and state-machine constants.

This module deliberately contains no medication-risk inference.  An
escalation is opened only from an explicit service fact such as a closed
unconfirmed occurrence (or a future trusted internal trigger).
"""


LEVEL_CAREGIVER = "CAREGIVER"
LEVEL_FAMILY = "FAMILY"
LEVEL_MANUAL_REVIEW = "MANUAL_REVIEW"
LEVEL_EMERGENCY = "EMERGENCY"  # Reserved for a future trusted P0 contract.

AUTOMATIC_LEVELS = (LEVEL_CAREGIVER, LEVEL_FAMILY, LEVEL_MANUAL_REVIEW)
ALL_LEVELS = AUTOMATIC_LEVELS + (LEVEL_EMERGENCY,)

STATUS_OPEN = "OPEN"
STATUS_ACKNOWLEDGED = "ACKNOWLEDGED"
STATUS_RESOLVED = "RESOLVED"
STATUS_CANCELLED = "CANCELLED"
STATUS_EXHAUSTED = "EXHAUSTED"

ALL_STATUSES = (
    STATUS_OPEN,
    STATUS_ACKNOWLEDGED,
    STATUS_RESOLVED,
    STATUS_CANCELLED,
    STATUS_EXHAUSTED,
)

RESOLUTION_CODES = (
    "TAKEN_VERIFIED",
    "NOT_TAKEN",
    "REFUSED",
    "NOT_FOUND",
    "DEVICE_ERROR",
    "OTHER",
)


def next_level(level):
    """Return the next automatic level, or ``None`` at manual review."""

    try:
        index = AUTOMATIC_LEVELS.index(level)
    except ValueError:
        return None
    if index + 1 >= len(AUTOMATIC_LEVELS):
        return None
    return AUTOMATIC_LEVELS[index + 1]


def initial_level(missed_count, repeat_threshold):
    """Choose the initial level from a count of recent missed facts.

    The policy is intentionally behavioral and medication-agnostic.  A
    threshold of zero or less disables the repeat shortcut.
    """

    if repeat_threshold and int(missed_count) >= int(repeat_threshold):
        return LEVEL_FAMILY
    return LEVEL_CAREGIVER


def target_role(level):
    return {
        LEVEL_CAREGIVER: "caregiver",
        LEVEL_FAMILY: "family",
        LEVEL_MANUAL_REVIEW: "manual_review",
        LEVEL_EMERGENCY: "emergency",
    }.get(level, str(level).lower())


def notification_event_type(level):
    return {
        LEVEL_CAREGIVER: "caregiver.task.assign",
        LEVEL_FAMILY: "family_notify.request",
        LEVEL_MANUAL_REVIEW: "manual_review.request",
        LEVEL_EMERGENCY: "emergency_alert.trigger",
    }.get(level)


def is_terminal_status(status):
    return status in (STATUS_RESOLVED, STATUS_CANCELLED)

