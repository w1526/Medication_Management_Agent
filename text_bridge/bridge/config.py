"""Configuration for the single-instance V1 text bridge."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return float(value)


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)


@dataclass(frozen=True)
class TestMapping:
    """The server-side test identity allow-list.

    The values are deliberately not taken from an untrusted WebSocket message.
    A registration is accepted only when all configured fields match this
    mapping.
    """

    tenant_id: str
    elder_id: str
    device_sn: str
    room_name: str = ""

    @classmethod
    def from_value(cls, value: str | dict | None) -> "TestMapping":
        if isinstance(value, dict):
            data = value
        else:
            raw = str(value or "").strip()
            if not raw:
                data = {}
            else:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    data = {}
                    for part in raw.split(","):
                        key, separator, item = part.partition("=")
                        if separator:
                            data[key.strip()] = item.strip()
        return cls(
            tenant_id=str(data.get("tenant_id") or "test_tenant").strip(),
            elder_id=str(data.get("elder_id") or "E001").strip(),
            device_sn=str(data.get("device_sn") or "test_device_001").strip(),
            room_name=str(data.get("room_name") or "").strip(),
        )

    def matches(self, payload: dict) -> bool:
        required = (
            ("tenant_id", self.tenant_id),
            ("elder_id", self.elder_id),
            ("device_sn", self.device_sn),
        )
        if any(str(payload.get(key) or "").strip() != expected for key, expected in required):
            return False
        return not self.room_name or str(payload.get("room_name") or "").strip() == self.room_name


@dataclass(frozen=True)
class BridgeConfig:
    enabled: bool = True
    bind_host: str = "127.0.0.1"
    bind_port: int = 18765
    medication_service_url: str = "http://127.0.0.1:18080"
    token: str = ""
    poll_seconds: float = 2.0
    journal_path: str = "text_bridge/bridge.sqlite3"
    test_mapping: TestMapping = TestMapping("test_tenant", "E001", "test_device_001")
    request_timeout_seconds: float = 8.0
    max_message_bytes: int = 32 * 1024

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        return cls(
            enabled=_bool_env("TEXT_BRIDGE_ENABLED", True),
            bind_host=os.getenv("TEXT_BRIDGE_BIND_HOST", "127.0.0.1").strip()
            or "127.0.0.1",
            bind_port=_int_env("TEXT_BRIDGE_BIND_PORT", 18765),
            medication_service_url=(
                os.getenv("MEDICATION_SERVICE_URL", "http://127.0.0.1:18080").strip()
                or "http://127.0.0.1:18080"
            ).rstrip("/"),
            token=os.getenv("TEXT_BRIDGE_TOKEN", ""),
            poll_seconds=max(_float_env("TEXT_BRIDGE_POLL_SECONDS", 2.0), 0.1),
            journal_path=(
                os.getenv("TEXT_BRIDGE_JOURNAL_PATH", "text_bridge/bridge.sqlite3").strip()
                or "text_bridge/bridge.sqlite3"
            ),
            test_mapping=TestMapping.from_value(os.getenv("TEXT_BRIDGE_TEST_MAPPING")),
            request_timeout_seconds=max(
                _float_env("TEXT_BRIDGE_REQUEST_TIMEOUT_SECONDS", 8.0), 0.1
            ),
            max_message_bytes=32 * 1024,
        )

    def journal_file(self) -> Path:
        return Path(self.journal_path)
