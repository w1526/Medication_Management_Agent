"""Deterministic M5 medication confirmation policy.

This module deliberately contains no model calls and no medication-risk
inference.  It only turns an immutable set of in-window evidence records into
an auditable assessment.  The application service owns persistence and the
transaction that applies the assessment to an occurrence.
"""

import hashlib
import json


POLICY_VERSION = "confirmation-policy-v1"

SOURCE_TYPES = (
    "USER_VOICE",
    "USER_BUTTON",
    "MANUAL_OPERATOR",
    "DEVICE",
    "SENSOR",
    "CAREGIVER",
    "FAMILY",
)

EVIDENCE_TYPES = (
    "SELF_REPORTED_TAKEN",
    "SELF_REPORTED_NOT_TAKEN",
    "BUTTON_CONFIRMED",
    "MANUAL_REPORTED_TAKEN",
    "BOX_OPENED",
    "BOX_CLOSED",
    "WEIGHT_OBSERVATION",
    "WEIGHT_DECREASE_OBSERVED",
    "NO_WEIGHT_CHANGE",
    "EXCESS_REMOVAL_SUSPECTED",
    "DEVICE_ERROR",
)

ASSESSMENT_RESULTS = ("CONFIRMED", "UNCONFIRMED", "CONFLICTED", "REVIEW_REQUIRED")

TAKEN_EVIDENCE_TYPES = (
    "SELF_REPORTED_TAKEN",
    "BUTTON_CONFIRMED",
    "MANUAL_REPORTED_TAKEN",
)


def policy_fingerprint():
    """Return a stable fingerprint for the shipped deterministic rules."""

    material = {
        "version": POLICY_VERSION,
        "source_types": SOURCE_TYPES,
        "evidence_types": EVIDENCE_TYPES,
        "taken_evidence_types": TAKEN_EVIDENCE_TYPES,
        "rules": [
            "taken evidence confirms",
            "box and weight observations never confirm alone",
            "taken plus no-weight-change conflicts unless device error is present",
            "excess removal requires review and never means overdose",
        ],
    }
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _basis(strong_types):
    if len(strong_types) > 1:
        return "MULTI_EVIDENCE"
    evidence_type = strong_types[0] if strong_types else None
    return {
        "SELF_REPORTED_TAKEN": "SELF_REPORT",
        "BUTTON_CONFIRMED": "BUTTON_SELF_REPORT",
        "MANUAL_REPORTED_TAKEN": "MANUAL_REPORT",
    }.get(evidence_type, "NONE")


def assess(evidence):
    """Assess in-window evidence using confirmation-policy-v1.

    ``evidence`` is expected to contain dictionaries returned by the storage
    layer.  Out-of-window or invalid evidence must be filtered by the caller;
    this function defensively ignores those rows as well.
    """

    usable = [
        item for item in evidence
        if not bool(item.get("out_of_window")) and not bool(item.get("invalid"))
    ]
    strong_types = []
    for item in usable:
        evidence_type = str(item.get("evidence_type") or "")
        if evidence_type in TAKEN_EVIDENCE_TYPES and evidence_type not in strong_types:
            strong_types.append(evidence_type)

    has_taken = bool(strong_types)
    has_no_weight_change = any(
        item.get("evidence_type") == "NO_WEIGHT_CHANGE" for item in usable
    )
    has_device_error = any(
        item.get("evidence_type") == "DEVICE_ERROR" for item in usable
    )
    has_excess_removal = any(
        item.get("evidence_type") == "EXCESS_REMOVAL_SUSPECTED" for item in usable
    )

    # DEVICE_ERROR explains why a sensor observation is not reliable; it is not
    # itself a contradiction.  A no-weight observation still conflicts with a
    # taken report when no such device error is present.
    conflict_detected = bool(has_taken and has_no_weight_change and not has_device_error)
    review_required = bool(has_excess_removal or conflict_detected)

    if has_excess_removal:
        result = "REVIEW_REQUIRED"
    elif has_taken:
        result = "CONFIRMED"
    else:
        result = "UNCONFIRMED"

    return {
        "result": result,
        "basis": _basis(strong_types),
        "evidence_ids": [item["evidence_id"] for item in usable],
        "conflict_detected": conflict_detected,
        "review_required": review_required,
        "has_device_error": has_device_error,
        "has_excess_removal": has_excess_removal,
    }

