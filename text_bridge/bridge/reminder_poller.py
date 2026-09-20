"""Poll the existing notification read API and feed registered sessions."""

from __future__ import annotations

import asyncio
import logging

from .medication_client import MedicationClient, MedicationClientError

logger = logging.getLogger(__name__)


class ReminderPoller:
    def __init__(self, registry, medication: MedicationClient, interval: float = 2.0) -> None:
        self.registry = registry
        self.medication = medication
        self.interval = max(float(interval), 0.1)
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self.run(), name="text-bridge-reminder-poller")
        return self._task

    async def run(self) -> None:
        while not self._stop.is_set():
            sessions = await self.registry.snapshot()
            await asyncio.gather(
                *(self._poll_session(session) for session in sessions),
                return_exceptions=True,
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                continue

    async def _poll_session(self, session) -> None:
        try:
            notifications = await self.medication.notifications(session.elder_id)
        except MedicationClientError as exc:
            logger.warning("notification poll failed for elder=%s: %s", session.elder_id, exc)
            return
        for notification in notifications:
            if str(notification.get("device_sn") or session.device_sn) != session.device_sn:
                continue
            try:
                await session.state_machine.enqueue(notification)
            except Exception:  # pragma: no cover - defensive poll isolation
                logger.exception("notification enqueue failed for session=%s", session.session_id)
        await session.state_machine.reconcile()

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None
