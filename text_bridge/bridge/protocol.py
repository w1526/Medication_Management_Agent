"""JSON text protocol V1 shared conceptually by Bridge and LiveKit."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import uuid

VERSION = "1"
MAX_MESSAGE_BYTES = 32 * 1024

MESSAGE_TYPES = frozenset(
    {
        "session.register",
        "session.ready",
        "interaction.bind",
        "user_text",
        "turn.result",
        "speak",
        "message.ack",
        "playback_status",
        "interaction.clear",
        "interaction.release",
        "session.close",
        "error",
    }
)
RELATED_TYPES = frozenset(
    {
        "interaction.bind",
        "user_text",
        "turn.result",
        "speak",
        "playback_status",
        "interaction.clear",
        "interaction.release",
    }
)


class ProtocolError(ValueError):
    """A protocol or message-size violation."""

    def __init__(self, message: str, code: str = "protocol_error") -> None:
        super().__init__(message)
        self.code = code


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _check_datetime(value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError("sent_at must be an ISO 8601 string")
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ProtocolError("sent_at is not valid ISO 8601", "invalid_sent_at") from exc
    if parsed.tzinfo is None:
        raise ProtocolError("sent_at must include a timezone", "invalid_sent_at")


def _required_text(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"{key} is required")
    return value.strip()


def _validate_related(data: dict) -> None:
    if data["type"] in RELATED_TYPES:
        _required_text(data, "interaction_id")
        revision = data.get("binding_revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise ProtocolError("binding_revision must be a positive integer")


def validate_message(data: object, max_bytes: int = MAX_MESSAGE_BYTES) -> dict:
    if not isinstance(data, dict):
        raise ProtocolError("message must be a JSON object")
    try:
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError("message is not JSON serializable") from exc
    if len(encoded) > max_bytes:
        raise ProtocolError("message exceeds 32 KiB", "message_too_large")
    if data.get("version") != VERSION:
        raise ProtocolError("unsupported protocol version", "unsupported_version")
    message_type = _required_text(data, "type")
    if message_type not in MESSAGE_TYPES:
        raise ProtocolError("unsupported message type", "unsupported_type")
    _required_text(data, "message_id")
    _required_text(data, "session_id")
    _check_datetime(data.get("sent_at"))
    if not isinstance(data.get("payload"), dict):
        raise ProtocolError("payload must be an object")
    _validate_related(data)
    if message_type == "session.register":
        for key in ("tenant_id", "elder_id", "device_sn", "room_name"):
            _required_text(data["payload"], key)
    elif message_type == "user_text":
        _required_text(data["payload"], "turn_id")
        if not isinstance(data["payload"].get("text"), str):
            raise ProtocolError("user_text.payload.text is required")
    elif message_type == "playback_status":
        _required_text(data["payload"], "playback_id")
        if data["payload"].get("status") not in {
            "started",
            "completed",
            "interrupted",
            "failed",
        }:
            raise ProtocolError("unsupported playback status")
    elif message_type == "speak":
        _required_text(data["payload"], "playback_id")
        _required_text(data["payload"], "text")
        if data["payload"].get("purpose") not in {"reminder", "clarify", "result"}:
            raise ProtocolError("unsupported speak purpose")
    elif message_type == "turn.result":
        if data["payload"].get("decision") not in {
            "handled",
            "clarify",
            "pending",
            "error",
        }:
            raise ProtocolError("unsupported turn decision")
    elif message_type == "message.ack":
        _required_text(data, "reply_to")
        _required_text(data["payload"], "status")
    elif message_type == "interaction.bind":
        _required_text(data["payload"], "expires_at")
    elif message_type == "interaction.clear":
        _required_text(data["payload"], "reason")
    return dict(data)


def decode_message(raw: bytes | str, max_bytes: int = MAX_MESSAGE_BYTES) -> dict:
    if isinstance(raw, str):
        raw_bytes = raw.encode("utf-8")
    else:
        raw_bytes = bytes(raw)
    if len(raw_bytes) > max_bytes:
        raise ProtocolError("message exceeds 32 KiB", "message_too_large")
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid UTF-8 JSON", "invalid_json") from exc
    return validate_message(data, max_bytes=max_bytes)


def encode_message(data: dict, max_bytes: int = MAX_MESSAGE_BYTES) -> str:
    validated = validate_message(data, max_bytes=max_bytes)
    encoded = json.dumps(validated, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ProtocolError("message exceeds 32 KiB", "message_too_large")
    return encoded


def make_message(
    message_type: str,
    session_id: str,
    payload: dict,
    *,
    interaction_id: str | None = None,
    binding_revision: int | None = None,
    reply_to: str | None = None,
    message_id: str | None = None,
) -> dict:
    message = {
        "version": VERSION,
        "type": message_type,
        "message_id": message_id or f"msg_{uuid.uuid4().hex}",
        "session_id": session_id,
        "sent_at": now_iso(),
        "payload": payload,
    }
    if reply_to is not None:
        message["reply_to"] = reply_to
    if interaction_id is not None:
        message["interaction_id"] = interaction_id
    if binding_revision is not None:
        message["binding_revision"] = binding_revision
    return validate_message(message)


def message_digest(data: dict) -> str:
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
