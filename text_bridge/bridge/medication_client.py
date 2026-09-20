"""Small HTTP/JSON client for the public Medication Service API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class MedicationClientError(RuntimeError):
    """A transport, timeout, or unexpected service response."""

    def __init__(self, message: str, *, status: int | None = None, transient: bool = True):
        super().__init__(message)
        self.status = status
        self.transient = transient


class MedicationBusinessError(MedicationClientError):
    """A deterministic 4xx/domain response which must not be blindly retried."""

    def __init__(self, message: str, *, status: int = 400, payload: dict | None = None):
        super().__init__(message, status=status, transient=False)
        self.payload = payload or {}


@dataclass
class MedicationClient:
    base_url: str = "http://127.0.0.1:18080"
    timeout_seconds: float = 8.0

    async def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: dict | None = None,
    ) -> dict:
        return await asyncio.to_thread(self._request_sync, method, path, query, body)

    def _request_sync(
        self,
        method: str,
        path: str,
        query: dict[str, str] | None,
        body: dict | None,
    ) -> dict:
        url = self.base_url.rstrip("/") + path
        if query:
            url += "?" + urlencode(query)
        raw_body = None
        headers = {"Accept": "application/json"}
        if body is not None:
            raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = Request(url, data=raw_body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                status = int(response.status)
        except HTTPError as exc:
            raw = exc.read()
            status = int(exc.code)
            payload = self._decode_payload(raw)
            if 400 <= status < 500:
                raise MedicationBusinessError(
                    str(payload.get("error") or "medication service rejected request"),
                    status=status,
                    payload=payload,
                ) from exc
            raise MedicationClientError(
                f"medication service HTTP {status}", status=status, transient=True
            ) from exc
        except (TimeoutError, OSError, URLError) as exc:
            raise MedicationClientError("medication service transport failure") from exc
        payload = self._decode_payload(raw)
        if status < 200 or status >= 300:
            raise MedicationClientError(
                f"unexpected medication service HTTP {status}",
                status=status,
                transient=status >= 500,
            )
        if not isinstance(payload, dict):
            raise MedicationClientError("medication service returned a non-object")
        return payload

    @staticmethod
    def _decode_payload(raw: bytes) -> dict:
        try:
            value = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MedicationClientError("invalid JSON from medication service") from exc
        return value if isinstance(value, dict) else {}

    async def notifications(self, elder_id: str) -> list[dict]:
        payload = await self._request(
            "GET", "/api/v1/medication/notifications", query={"elder_id": elder_id}
        )
        items = payload.get("items")
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    async def occurrence(self, occurrence_id: str) -> dict:
        return await self._request("GET", f"/api/v1/medication/occurrences/{occurrence_id}")

    async def plans(self, plan_id: str) -> list[dict]:
        payload = await self._request("GET", f"/api/v1/medication/plans/{plan_id}")
        items = payload.get("items")
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    async def response(self, data: dict) -> dict:
        return await self._request("POST", "/api/v1/medication/responses", body=data)

    async def device_event(self, data: dict) -> dict:
        return await self._request("POST", "/api/v1/medication/device-events", body=data)
