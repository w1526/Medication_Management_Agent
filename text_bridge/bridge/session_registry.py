"""Trusted LiveKit session registration and one-session-per-elder enforcement."""

from __future__ import annotations

from dataclasses import dataclass
import asyncio

from .config import TestMapping


class SessionRegistryError(RuntimeError):
    def __init__(self, message: str, code: str = "session_conflict") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class RegisteredIdentity:
    session_id: str
    tenant_id: str
    elder_id: str
    device_sn: str
    room_name: str


class SessionRegistry:
    def __init__(self, mapping: TestMapping) -> None:
        self.mapping = mapping
        self._lock = asyncio.Lock()
        self._by_session: dict[str, RegisteredIdentity] = {}
        self._session_by_elder: dict[str, str] = {}

    async def register(self, session_id: str, payload: dict) -> RegisteredIdentity:
        async with self._lock:
            if not self.mapping.matches(payload):
                raise SessionRegistryError("session identity is not in the test mapping", "identity_mismatch")
            if session_id in self._by_session:
                raise SessionRegistryError("session is already registered")
            elder_id = self.mapping.elder_id
            if elder_id in self._session_by_elder:
                raise SessionRegistryError("elder already has an active bridge session")
            identity = RegisteredIdentity(
                session_id=session_id,
                tenant_id=self.mapping.tenant_id,
                elder_id=elder_id,
                device_sn=self.mapping.device_sn,
                room_name=str(payload["room_name"]).strip(),
            )
            self._by_session[session_id] = identity
            self._session_by_elder[elder_id] = session_id
            return identity

    async def remove(self, session_id: str) -> RegisteredIdentity | None:
        async with self._lock:
            identity = self._by_session.pop(session_id, None)
            if identity is not None and self._session_by_elder.get(identity.elder_id) == session_id:
                self._session_by_elder.pop(identity.elder_id, None)
            return identity

    async def get(self, session_id: str) -> RegisteredIdentity | None:
        async with self._lock:
            return self._by_session.get(session_id)

    async def snapshot(self) -> list[RegisteredIdentity]:
        async with self._lock:
            return list(self._by_session.values())
