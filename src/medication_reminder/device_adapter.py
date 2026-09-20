"""Reminder delivery adapters.

The medication domain emits a durable ``device.interaction.request`` event.
Adapters only deliver that request; they never update medication intake state.
The service marks the reminder attempt as dispatched after ``dispatch``
returns successfully.
"""

import json
import os
import socket
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import load_local_env


class DeviceAdapterError(Exception):
    """An error while delivering a reminder to a device or Chat Agent."""

    def __init__(self, message, retryable=True, status_code=None, kind=None):
        super().__init__(message)
        self.message = message
        self.retryable = bool(retryable)
        self.status_code = status_code
        self.kind = kind or ("transient" if self.retryable else "permanent")


class DeviceAdapter:
    """Small adapter protocol used by ``MedicationService``."""

    def dispatch(self, event):  # pragma: no cover - interface documentation
        raise NotImplementedError

    def close(self):
        return None


class LocalDeviceAdapter(DeviceAdapter):
    """Offline adapter used by the Web MVP and unit tests."""

    def dispatch(self, event):
        return {
            "accepted": True,
            "adapter": "local",
            "event_id": event.get("event_id"),
        }


class ChatAgentHttpAdapter(DeviceAdapter):
    """Deliver reminder requests to an external Chat Agent over HTTP."""

    DEFAULT_REMINDER_PATH = "/api/v1/chat-agent/reminders"

    def __init__(self, base_url=None, reminder_path=None, timeout_seconds=None,
                 api_token=None, opener=None):
        load_local_env()
        self.base_url = str(
            base_url if base_url is not None else os.environ.get("CHAT_AGENT_BASE_URL", "")
        ).strip().rstrip("/")
        self.reminder_path = str(
            reminder_path
            if reminder_path is not None
            else os.environ.get("CHAT_AGENT_REMINDER_PATH", self.DEFAULT_REMINDER_PATH)
        ).strip()
        if not self.reminder_path:
            self.reminder_path = self.DEFAULT_REMINDER_PATH
        if not self.reminder_path.startswith("/"):
            self.reminder_path = "/" + self.reminder_path
        try:
            self.timeout_seconds = float(
                timeout_seconds
                if timeout_seconds is not None
                else os.environ.get("CHAT_AGENT_TIMEOUT_SECONDS", "5")
            )
        except (TypeError, ValueError) as exc:
            raise DeviceAdapterError(
                "CHAT_AGENT_TIMEOUT_SECONDS must be a positive number",
                retryable=False,
                kind="invalid_config",
            ) from exc
        if self.timeout_seconds <= 0:
            raise DeviceAdapterError(
                "CHAT_AGENT_TIMEOUT_SECONDS must be a positive number",
                retryable=False,
                kind="invalid_config",
            )
        self.api_token = str(
            api_token if api_token is not None else os.environ.get("CHAT_AGENT_API_TOKEN", "")
        ).strip()
        self.opener = opener or urlopen

    def _url(self):
        if not self.base_url:
            raise DeviceAdapterError(
                "CHAT_AGENT_BASE_URL is required when MEDICATION_DEVICE_ADAPTER=http",
                retryable=False,
                kind="invalid_config",
            )
        return self.base_url + self.reminder_path

    @staticmethod
    def request_payload(event):
        """Convert the internal event envelope to the public HTTP contract."""

        source = dict(event.get("payload") or {})
        medication = source.get("medication") or {
            "name": source.get("drug_name"),
            "dose": source.get("dosage"),
            "instruction": source.get("relation_to_meal"),
        }
        payload = {
            "event_id": event.get("event_id"),
            "event_type": event.get("event_type"),
            "trace_id": source.get("trace_id") or event.get("trace_id")
            or "interaction:%s" % source.get("interaction_id", "unknown"),
            "elder_id": event.get("elder_id"),
            "interaction_id": source.get("interaction_id"),
            "occurrence_id": source.get("occurrence_id") or event.get("occurrence_id"),
            "plan_id": event.get("plan_id") or source.get("plan_id"),
            "medication": medication,
            "scheduled_at": source.get("scheduled_at"),
            "expires_at": source.get("expires_at"),
            "reminder_text": source.get("reminder_text") or source.get("text"),
        }
        required = (
            "event_id", "event_type", "elder_id", "interaction_id",
            "occurrence_id", "plan_id", "reminder_text",
        )
        missing = [field for field in required if not payload.get(field)]
        if missing:
            raise DeviceAdapterError(
                "device.interaction.request is missing fields: %s" % ", ".join(missing),
                retryable=False,
                kind="invalid_contract",
            )
        return payload

    def dispatch(self, event):
        payload = self.request_payload(event)
        raw_request = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
        }
        if self.api_token:
            headers["Authorization"] = "Bearer " + self.api_token
        request = Request(self._url(), data=raw_request, headers=headers, method="POST")
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                status_code = int(getattr(response, "status", None) or response.getcode())
                raw_response = response.read() or b""
        except HTTPError as exc:
            retryable = int(exc.code) >= 500
            raise DeviceAdapterError(
                "Chat Agent returned HTTP %s" % exc.code,
                retryable=retryable,
                status_code=exc.code,
                kind="http_%sxx" % (int(exc.code) // 100),
            ) from exc
        except (URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise DeviceAdapterError(
                "Chat Agent request failed: %s" % exc,
                retryable=True,
                kind="network",
            ) from exc

        if not 200 <= status_code < 300:
            raise DeviceAdapterError(
                "Chat Agent returned HTTP %s" % status_code,
                retryable=status_code >= 500,
                status_code=status_code,
                kind="http_%sxx" % (status_code // 100),
            )
        if raw_response:
            try:
                decoded = json.loads(raw_response.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DeviceAdapterError(
                    "Chat Agent returned invalid JSON",
                    retryable=False,
                    status_code=status_code,
                    kind="invalid_response",
                ) from exc
            if not isinstance(decoded, dict):
                raise DeviceAdapterError(
                    "Chat Agent response must be a JSON object",
                    retryable=False,
                    status_code=status_code,
                    kind="invalid_response",
                )
        else:
            decoded = {}
        return {"accepted": True, "status_code": status_code, "response": decoded}


def build_device_adapter(config=None):
    """Build the configured adapter, defaulting safely to local mode."""

    config = config or {}
    mode = str(
        config.get("device_adapter")
        or os.environ.get("MEDICATION_DEVICE_ADAPTER", "local")
    ).strip().lower()
    if mode == "local":
        return LocalDeviceAdapter()
    if mode == "http":
        return ChatAgentHttpAdapter(
            base_url=config.get("chat_agent_base_url"),
            reminder_path=config.get("chat_agent_reminder_path"),
            timeout_seconds=config.get("chat_agent_timeout_seconds"),
            api_token=config.get("chat_agent_api_token"),
        )
    raise DeviceAdapterError(
        "MEDICATION_DEVICE_ADAPTER must be local or http",
        retryable=False,
        kind="invalid_config",
    )

