"""Deterministic, session-scoped medication interaction orchestration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import uuid
from typing import Awaitable, Callable

from .delivery_journal import DeliveryJournal
from .medication_client import (
    MedicationBusinessError,
    MedicationClient,
    MedicationClientError,
)
from .protocol import make_message
from .turn_router import CLARIFY_TEXT, TurnDecision, route_user_text


class BridgeState(str, Enum):
    IDLE = "IDLE"
    REMINDER_PENDING = "REMINDER_PENDING"
    PLAYING = "PLAYING"
    AWAITING_RESPONSE = "AWAITING_RESPONSE"
    PROCESSING_RESPONSE = "PROCESSING_RESPONSE"


@dataclass
class Playback:
    playback_id: str
    message_id: str
    purpose: str
    text: str
    attempt_id: str | None
    started: bool = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        raw = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class InteractionStateMachine:
    """One elder/session queue.

    State mutations are serialized by a session-local actor. Network calls are
    awaited by actor operations without holding an asyncio lock; every
    response is applied only to the current interaction and revision.
    """

    def __init__(
        self,
        *,
        session_id: str,
        elder_id: str,
        device_sn: str,
        medication: MedicationClient,
        retry_delays: tuple[float, ...] = (0.0, 1.0, 2.0, 4.0),
        journal: DeliveryJournal,
        send: Callable[[dict], Awaitable[None]],
        wait_ack: Callable[[dict], Awaitable[bool]],
        ack_timeout: float = 8.0,
    ) -> None:
        self.session_id = session_id
        self.elder_id = elder_id
        self.device_sn = device_sn
        self.medication = medication
        self.journal = journal
        self._send = send
        self._wait_ack = wait_ack
        self.ack_timeout = ack_timeout
        self._command_queue: asyncio.Queue | None = None
        self._executor_task: asyncio.Task | None = None
        self.retry_delays = retry_delays or (0.0,)
        self.state = BridgeState.IDLE
        self.binding_revision = 0
        self.current_interaction_id: str | None = None
        self.current_playback_id: str | None = None
        self.current_turn_id: str | None = None
        self._current_notification: dict | None = None
        self._current_playback: Playback | None = None
        self._playback_binding_revision: int | None = None
        self._interaction_binding_revision: int | None = None
        self._turn_message_id: str | None = None
        self._last_result_message_id: str | None = None
        self._pending_user: dict | None = None
        self._pending_response_payload: dict | None = None
        self._pending_response_message: dict | None = None
        self._pending_response_decision: TurnDecision | None = None
        self._pending_response_event_id: str | None = None
        self._pending_result_text: str | None = None
        self._pending_result_revision: int | None = None
        self._binding_installed = False
        self._queue: list[dict] = []
        self._released: set[str] = set()
        self._recovery_blocked = False
        self._recovery_reason: str | None = None
        self._stopped = False
        self._load_runtime()

    def _load_runtime(self) -> None:
        runtime = self.journal.load_runtime(self.session_id) or {}
        try:
            self.binding_revision = int(runtime.get("binding_revision") or 0)
        except (TypeError, ValueError):
            self.binding_revision = 0
        # A process restart never blindly restores an active binding. The
        # poller will query the service and establish a fresh revision.
        self._released = set(str(item) for item in runtime.get("released", []) if item)
        self._recovery_blocked = bool(runtime.get("recovery_blocked"))

        recovery_reason = runtime.get("recovery_reason")
        self._recovery_reason = str(recovery_reason) if recovery_reason else None
        recovery_request = self.journal.load_recovery_request(self.session_id)
        if recovery_request is not None and self._recovery_reason in {
            None,
            "response_in_flight",
            "response_unknown",
        }:
            event_id = str(recovery_request.get("event_id") or "")
            interaction_id = str(recovery_request.get("interaction_id") or "")
            action = str(recovery_request.get("action") or "")
            if event_id and interaction_id and action:
                self._pending_response_payload = {
                    key: value for key, value in recovery_request.items()
                    if key not in {"turn_id", "reply_to", "binding_revision"}
                }
                self.current_turn_id = str(recovery_request.get("turn_id") or "") or None
                self._pending_response_event_id = event_id
                self._pending_response_decision = TurnDecision(
                    action=action,
                    delay_minutes=recovery_request.get("delay_minutes"),
                )
                self._pending_response_message = {
                    "message_id": f"recovery:{event_id}",
                    "session_id": self.session_id,
                    "interaction_id": interaction_id,
                    "binding_revision": self.binding_revision,
                }
                self._recovery_blocked = True
                self._recovery_reason = "response_unknown"

    def snapshot(self) -> dict:
        return {
            "state": self.state.value,
            "binding_revision": self.binding_revision,
            "current_interaction_id": self.current_interaction_id,
            "current_playback_id": self.current_playback_id,
            "current_turn_id": self.current_turn_id,
            "queue_interaction_ids": [item.get("interaction_id") for item in self._queue],
            "playback_binding_revision": self._playback_binding_revision,
            "interaction_binding_revision": self._interaction_binding_revision,
            "released": sorted(self._released),
            "recovery_blocked": self._recovery_blocked,
            "recovery_reason": self._recovery_reason,
        }

    def _persist(self) -> None:
        self.journal.save_runtime(self.session_id, self.snapshot())

    def _same_session(self, message: dict) -> bool:
        return message.get("session_id") == self.session_id

    async def enqueue(self, notification: dict) -> bool:
        return bool(await self._run_serial(lambda: self._enqueue_serial(notification)))

    async def _run_serial(self, operation, *, allow_stopped: bool = False):
        if self._stopped and not allow_stopped:
            return None
        if self._executor_task is None or self._executor_task.done():
            self._command_queue = asyncio.Queue()
            self._executor_task = asyncio.create_task(self._executor_loop())
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        await self._command_queue.put((operation, future))
        return await future

    async def _executor_loop(self) -> None:
        while True:
            operation, future = await self._command_queue.get()
            try:
                result = await operation()
                if not future.done():
                    future.set_result(result)
            except asyncio.CancelledError:
                if not future.done():
                    future.cancel()
                raise
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)

    async def _enqueue_serial(self, notification: dict) -> bool:
        """Queue one service notification and start it if the session is idle."""
        interaction_id = str(notification.get("interaction_id") or "")
        if not interaction_id or str(notification.get("elder_id") or "") != self.elder_id:
            return False
        if str(notification.get("device_sn") or self.device_sn) != self.device_sn:
            return False
        if self._stopped or interaction_id in self._released:
            return False
        existing = {item.get("interaction_id") for item in self._queue}
        if interaction_id == self.current_interaction_id or interaction_id in existing:
            return False
        self._queue.append(dict(notification))
        self._queue.sort(
            key=lambda item: (str(item.get("opened_at") or ""), str(item.get("interaction_id") or ""))
        )
        self._persist()
        if self.state == BridgeState.IDLE and not self._recovery_blocked:
            await self._start_next_locked()
        return True

    async def _start_next_locked(self) -> None:
        while self.state == BridgeState.IDLE and self._queue and not self._recovery_blocked:
            notification = self._queue.pop(0)
            interaction_id = str(notification.get("interaction_id") or "")
            expiry = _parse_time(notification.get("expires_at"))
            if expiry is not None and expiry <= _now():
                self._released.add(interaction_id)
                self._persist()
                continue
            self.current_interaction_id = interaction_id
            self._current_notification = notification
            self.current_playback_id = _new_id("playback")
            self.current_turn_id = None
            self._current_playback = None
            self._playback_binding_revision = None
            self._binding_installed = False
            self.state = BridgeState.REMINDER_PENDING
            self.binding_revision += 1
            revision = self.binding_revision
            self._persist()
            self._interaction_binding_revision = revision

            if not await self._service_recheck_locked(notification):
                self._reset_current_locked()
                continue
            bind = make_message(
                "interaction.bind",
                self.session_id,
                {"expires_at": notification["expires_at"]},
                interaction_id=interaction_id,
                binding_revision=revision,
            )
            try:
                await self._send(bind)
                accepted = await asyncio.wait_for(self._wait_ack(bind), self.ack_timeout)
            except (asyncio.TimeoutError, OSError, RuntimeError):
                accepted = False
            if not accepted:
                # No binding ACK means LiveKit cannot receive a speak safely.
                self._reset_current_locked()
                return
            self._binding_installed = True
            self._persist()
            text = str(notification.get("text") or "").strip()
            if not text:
                await self._terminate_invalid_locked("missing_text")
                return
            try:
                await self._send_speak_locked(
                    purpose="reminder",
                    text=text,
                    attempt_id=str(notification.get("attempt_id") or "") or None,
                )
            except (OSError, RuntimeError):
                await self._terminate_invalid_locked("speak_send_failed")
            return

    async def _service_recheck_locked(self, notification: dict) -> bool:
        """Recheck occurrence and plan facts immediately before binding."""
        try:
            occurrence = await self.medication.occurrence(str(notification["occurrence_id"]))
            plans = await self.medication.plans(str(occurrence.get("plan_id") or notification.get("plan_id")))
        except MedicationClientError:
            return False
        if str(occurrence.get("elder_id") or "") != self.elder_id:
            return False
        if occurrence.get("intake_status") != "unconfirmed":
            return False
        interactions = occurrence.get("interactions") or []
        interaction = next(
            (item for item in interactions if item.get("interaction_id") == self.current_interaction_id),
            None,
        )
        if not isinstance(interaction, dict) or interaction.get("status") != "open":
            return False
        expires_at = str(interaction.get("expires_at") or notification.get("expires_at") or "")
        expiry = _parse_time(expires_at)
        if expiry is None or expiry <= _now():
            return False
        version = int(occurrence.get("plan_version") or 0)
        plan = next(
            (
                item
                for item in plans
                if str(item.get("plan_id")) == str(occurrence.get("plan_id"))
                and int(item.get("version") or 0) == version
            ),
            None,
        )
        if not isinstance(plan, dict) or plan.get("status") != "active":
            return False
        notification["expires_at"] = expires_at
        notification["occurrence_id"] = occurrence.get("occurrence_id", notification["occurrence_id"])
        return True

    async def _send_speak_locked(
        self,
        *,
        purpose: str,
        text: str,
        attempt_id: str | None,
        binding_revision: int | None = None,
    ) -> None:
        playback_revision = binding_revision or self.binding_revision
        playback_id = self.current_playback_id or _new_id("playback")
        self.current_playback_id = playback_id
        message = make_message(
            "speak",
            self.session_id,
            {
                "purpose": purpose,
                "playback_id": playback_id,
                "text": text,
                "expires_at": str(
                    (self._current_notification or {}).get("expires_at") or ""
                ),
            },
            interaction_id=str(self.current_interaction_id),
            binding_revision=playback_revision,
            reply_to=self._turn_message_id or self._last_result_message_id,
        )
        self._current_playback = Playback(
            playback_id=playback_id,
            message_id=message["message_id"],
            purpose=purpose,
            text=text,
            attempt_id=attempt_id,
        )
        self._playback_binding_revision = playback_revision
        self.journal.save_playback(
            self.session_id,
            playback_id,
            str(self.current_interaction_id),
            playback_revision,
            purpose,
            message["message_id"],
            "queued",
            attempt_id,
        )
        await self._send(message)
        self.state = BridgeState.REMINDER_PENDING if purpose == "reminder" else BridgeState.PLAYING
        self._persist()

    async def handle_user_text(self, message: dict) -> None:
        if not self._same_session(message):
            return
        await self._run_serial(lambda: self._handle_user_text_serial(message))

    async def _handle_user_text_serial(self, message: dict) -> None:
        interaction_id = message.get("interaction_id")
        if interaction_id != self.current_interaction_id or message.get("binding_revision") != self.binding_revision:
            await self._send_error_locked("stale_binding", "user text does not match current binding", message)
            return
        payload = message.get("payload") or {}
        turn_id = str(payload.get("turn_id") or "")
        text = str(payload.get("text") or "")
        if self.state == BridgeState.PROCESSING_RESPONSE:
            if turn_id == self.current_turn_id:
                await self._send_turn_result_locked("pending", reply_to=message["message_id"])
            else:
                await self._send_error_locked("busy", "another medication turn is processing", message)
            return
        if self.state in {BridgeState.REMINDER_PENDING, BridgeState.PLAYING} and self._current_playback is not None:
            if self._current_playback.purpose in {"reminder", "clarify"} and self._pending_user is None:
                self._pending_user = dict(message)
                self._persist()
            return
        if self.state != BridgeState.AWAITING_RESPONSE:
            await self._send_error_locked("busy", "medication interaction is not accepting a turn", message)
            return
        await self._begin_turn_locked(message, turn_id, text)

    async def _begin_turn_locked(self, message: dict, turn_id: str, text: str) -> None:
        if not turn_id:
            await self._send_error_locked("invalid_turn", "turn_id is required", message)
            return
        self.current_turn_id = turn_id
        self._turn_message_id = message["message_id"]
        event_id = f"bridge:{self.session_id}:{turn_id}"
        self.state = BridgeState.PROCESSING_RESPONSE
        self.journal.save_turn(
            self.session_id,
            turn_id,
            event_id,
            str(self.current_interaction_id),
            self.binding_revision,
            text,
            self.state.value,
        )
        self._persist()
        await self._process_turn_locked(message, text, event_id)

    async def _process_turn_locked(self, message: dict, text: str, event_id: str) -> None:
        decision = route_user_text(text)
        if decision.action is None:
            await self._send_turn_result_locked("clarify", reply_to=message["message_id"])
            self._save_turn_decision_locked("clarify", None)
            self.current_turn_id = None
            self._turn_message_id = None
            self.current_playback_id = _new_id("playback")
            await self._send_speak_locked(
                purpose="clarify", text=decision.clarification or CLARIFY_TEXT, attempt_id=None
            )
            return

        response_payload = {
            "event_id": event_id,
            "elder_id": self.elder_id,
            "interaction_id": self.current_interaction_id,
            "action": decision.action,
            "source": "text_bridge",
            "text": text,
        }
        if decision.delay_minutes is not None:
            response_payload["delay_minutes"] = decision.delay_minutes
        self.journal.save_recovery(
            self.session_id,
            self.current_interaction_id,
            self.current_turn_id,
            event_id,
            "response_in_flight",
            response_payload,
        )
        result, failure = await self._call_response_with_retry_locked(response_payload)
        if failure is not None:
            if isinstance(failure, MedicationBusinessError):
                await self._send_turn_result_locked("error", reply_to=message["message_id"])
                self._save_turn_decision_locked("error", None)
                self.journal.clear_recovery(self.session_id)
                await self._finish_with_result_locked(
                    "这条用药提醒已经失效了，请让家人或工作台查看。"
                )
            else:
                await self._send_turn_result_locked("pending", reply_to=message["message_id"])
                self.journal.save_recovery(
                    self.session_id,
                    self.current_interaction_id,
                    self.current_turn_id,
                    event_id,
                    "response_unknown",
                    response_payload,
                )
                self._recovery_blocked = True
                self._recovery_reason = "response_unknown"
                self._pending_response_payload = dict(response_payload)
                self._pending_response_message = dict(message)
                self._pending_response_decision = decision
                self._pending_response_event_id = event_id
                self._persist()
            return

        await self._complete_response_locked(message, decision, result)

    async def _complete_response_locked(
        self, message: dict, decision: TurnDecision, result: dict | None
    ) -> None:
        if self._recovery_reason == "response_unknown":
            self._recovery_blocked = False
            self._recovery_reason = None
            self._pending_response_payload = None
            self._pending_response_message = None
            self._pending_response_decision = None
            self._pending_response_event_id = None
            self.journal.clear_recovery(self.session_id)
        else:
            self.journal.clear_recovery(self.session_id)
        occurrence = result.get("occurrence") if isinstance(result, dict) else {}
        status = str((occurrence or {}).get("intake_status") or "")
        if decision.action == "REPEAT":
            await self._send_turn_result_locked(
                "handled", reply_to=message["message_id"], business_status="repeated"
            )
            self._save_turn_decision_locked("handled", "repeated")
            self.current_turn_id = None
            self._turn_message_id = None
            await self._repeat_locked()
            return

        await self._send_turn_result_locked(
            "handled", reply_to=message["message_id"], business_status=status or decision.action.lower()
        )
        self._save_turn_decision_locked("handled", status or decision.action.lower())
        await self._finish_with_result_locked(self._result_text(decision, status))

    async def _call_response_with_retry_locked(self, payload: dict) -> tuple[dict | None, Exception | None]:
        failure: Exception | None = None
        for delay in self.retry_delays:
            if delay:
                await asyncio.sleep(delay)
            try:
                return await self.medication.response(payload), None
            except MedicationBusinessError as exc:
                return None, exc
            except MedicationClientError as exc:
                failure = exc
                if not exc.transient:
                    return None, exc

        return None, failure or MedicationClientError("response outcome unknown")

    def _save_turn_decision_locked(self, decision: str, status: str | None) -> None:
        if self.current_turn_id is None:
            return
        row = self.journal.get_turn(self.session_id, self.current_turn_id)
        self.journal.save_turn(
            self.session_id,
            self.current_turn_id,
            str(row["event_id"] if row else f"bridge:{self.session_id}:{self.current_turn_id}"),
            str(self.current_interaction_id),
            self.binding_revision,
            "",
            self.state.value,
            decision,
            status,
        )

    async def _finish_with_result_locked(self, text: str) -> None:
        result_revision = self._interaction_binding_revision or self.binding_revision
        self.current_turn_id = None
        self._turn_message_id = None
        if not await self._clear_binding_locked("service_closed"):
            self._recovery_blocked = True
            self._recovery_reason = "clear_unknown_result"
            self._pending_result_text = text
            self._pending_result_revision = result_revision
            self.journal.save_recovery(
                self.session_id,
                self.current_interaction_id,
                None,
                None,
                "clear_unknown",
            )
            self._persist()
            return
        self.current_playback_id = _new_id("playback")
        await self._send_speak_locked(
            purpose="result",
            text=text,
            attempt_id=None,
            binding_revision=result_revision,
        )

    def _result_text(self, decision: TurnDecision, status: str) -> str:
        if status == "confirmed_taken" or decision.action == "CONFIRM_TAKEN":
            return "好的，已经记下您吃过了。"
        if status == "skipped" or decision.action == "SKIP":
            return "好的，这次先跳过，已经记下了。"
        return "好的，已经记下了，过一会儿再提醒您。"

    async def _repeat_locked(self) -> None:
        try:
            notifications = await self.medication.notifications(self.elder_id)
        except MedicationClientError:
            self._recovery_blocked = True
            self._recovery_reason = "repeat_unknown"
            self.journal.save_recovery(
                self.session_id, self.current_interaction_id, None, None, "repeat_unknown"
            )
            self._persist()
            return
        notification = next(
            (item for item in notifications if item.get("interaction_id") == self.current_interaction_id),
            None,
        )
        if not isinstance(notification, dict) or not str(notification.get("text") or "").strip():
            self._recovery_blocked = True
            self._recovery_reason = "repeat_unknown"
            self.journal.save_recovery(
                self.session_id, self.current_interaction_id, None, None, "repeat_interaction_missing"
            )
            self._persist()
            return
        self._recovery_blocked = False
        self._recovery_reason = None
        self.journal.clear_recovery(self.session_id)
        self._current_notification = notification
        self.state = BridgeState.REMINDER_PENDING
        self.current_playback_id = _new_id("playback")
        await self._send_speak_locked(
            purpose="reminder",
            text=str(notification["text"]),
            attempt_id=None,
        )

    async def _clear_binding_locked(self, reason: str) -> bool:
        if not self._binding_installed:
            return True
        self.binding_revision += 1
        clear = make_message(
            "interaction.clear",
            self.session_id,
            {"reason": reason},
            interaction_id=str(self.current_interaction_id),
            binding_revision=self.binding_revision,
        )
        try:
            await self._send(clear)
            accepted = await asyncio.wait_for(self._wait_ack(clear), self.ack_timeout)
        except (asyncio.TimeoutError, OSError, RuntimeError):
            accepted = False
        if accepted:
            self._binding_installed = False
            self._persist()
        return accepted

    async def handle_playback_status(self, message: dict) -> None:
        if not self._same_session(message):
            return
        await self._run_serial(lambda: self._handle_playback_status_serial(message))

    async def _handle_playback_status_serial(self, message: dict) -> None:
        if not self._stopped:
            playback = self._current_playback
            payload = message.get("payload") or {}
            if (
                playback is None
                or message.get("interaction_id") != self.current_interaction_id
                or message.get("binding_revision") != (self._playback_binding_revision or self.binding_revision)
                or payload.get("playback_id") != playback.playback_id
                or message.get("reply_to") != playback.message_id
            ):
                await self._send_error_locked("stale_playback", "playback does not match current output", message)
                return
            status = payload.get("status")
            if status == "started":
                if playback.started:
                    return
                playback.started = True
                self.state = BridgeState.PLAYING
                self.journal.save_playback(
                    self.session_id, playback.playback_id, str(self.current_interaction_id),
                    self._playback_binding_revision or self.binding_revision, playback.purpose, playback.message_id, "started",
                    playback.attempt_id,
                )
                await self._send_device_event_locked(playback, "started")
                self._persist()
                return
            self.journal.save_playback(
                self.session_id, playback.playback_id, str(self.current_interaction_id),
                self._playback_binding_revision or self.binding_revision, playback.purpose, playback.message_id, status,
                playback.attempt_id,
            )
            await self._send_device_event_locked(playback, status, str(payload.get("failure_reason") or "") or None)
            purpose = playback.purpose
            self.current_playback_id = None
            self._current_playback = None
            self._playback_binding_revision = None
            if status == "failed":
                if not await self._clear_binding_locked("playback_failed"):
                    self._recovery_blocked = True
                    self._recovery_reason = "clear_unknown_failure"
                    self.journal.save_recovery(
                        self.session_id,
                        self.current_interaction_id,
                        None,
                        None,
                        "clear_unknown",
                    )
                    self._persist()
                    return
                self._reset_current_locked()
                await self._start_next_locked()
                return
            if purpose == "result":
                self._reset_current_locked()
                await self._start_next_locked()
                return
            self.state = BridgeState.AWAITING_RESPONSE
            self._persist()
            pending = self._pending_user
            self._pending_user = None
            if pending is not None:
                payload = pending.get("payload") or {}
                await self._begin_turn_locked(
                    pending,
                    str(payload.get("turn_id") or ""),
                    str(payload.get("text") or ""),
                )

    async def _send_device_event_locked(self, playback: Playback, status: str, failure_reason: str | None = None) -> None:
        # Replays do not mutate the original reminder_attempt in V1.
        if playback.purpose != "reminder" or not playback.attempt_id:
            return
        event_id = f"bridge:playback:{self.session_id}:{playback.playback_id}:{status}"
        payload = {
            "event_id": event_id,
            "elder_id": self.elder_id,
            "attempt_id": playback.attempt_id,
            "interaction_id": self.current_interaction_id,
            "event_type": status,
            "source": "text_bridge",
            "failure_reason": failure_reason,
        }
        self.journal.save_device_event(
            self.session_id, event_id, self.current_interaction_id, playback.attempt_id,
            status, failure_reason, "pending",
        )
        result, failure = await self._call_device_event_with_retry_locked(payload)
        if failure is None:
            self.journal.save_device_event(
                self.session_id, event_id, self.current_interaction_id, playback.attempt_id,
                status, failure_reason, "sent",
            )
        elif isinstance(failure, MedicationBusinessError):
            self.journal.save_device_event(
                self.session_id, event_id, self.current_interaction_id, playback.attempt_id,
                status, failure_reason, "failed",
            )

    async def _call_device_event_with_retry_locked(self, payload: dict) -> tuple[dict | None, Exception | None]:
        failure: Exception | None = None
        for delay in self.retry_delays:
            if delay:
                await asyncio.sleep(delay)
            try:
                return await self.medication.device_event(payload), None
            except MedicationBusinessError as exc:
                return None, exc
            except MedicationClientError as exc:
                failure = exc
                if not exc.transient:
                    return None, exc
        return None, failure or MedicationClientError("device event outcome unknown")

    async def _recover_device_events_locked(self) -> None:
        for row in self.journal.pending_device_events(self.session_id):
            payload = {
                "event_id": row["event_id"],
                "elder_id": self.elder_id,
                "attempt_id": row["attempt_id"],
                "interaction_id": row.get("interaction_id"),
                "event_type": row["event_type"],
                "source": "text_bridge",
                "failure_reason": row.get("failure_reason"),
            }
            result, failure = await self._call_device_event_with_retry_locked(payload)
            if failure is None or isinstance(failure, MedicationBusinessError):
                self.journal.save_device_event(
                    self.session_id, row["event_id"], row.get("interaction_id"),
                    row["attempt_id"], row["event_type"], row.get("failure_reason"),
                    "sent" if failure is None else "failed",
                )

    async def handle_release(self, message: dict, reason: str = "local_release") -> None:
        if not self._same_session(message):
            return
        await self._run_serial(lambda: self._handle_release_serial(message, reason))

    async def _handle_release_serial(self, message: dict, reason: str) -> None:
        if not self._stopped:
            if message.get("interaction_id") != self.current_interaction_id:
                await self._send_error_locked("stale_binding", "release does not match current interaction", message)
                return
            if message.get("binding_revision") != self.binding_revision:
                await self._send_error_locked("stale_binding", "release uses an old revision", message)
                return
            interaction_id = str(self.current_interaction_id)
            self._released.add(interaction_id)
            self.journal.mark_released(self.session_id, interaction_id, reason)
            self._queue = [item for item in self._queue if item.get("interaction_id") != interaction_id]
            if not await self._clear_binding_locked(reason):
                self._recovery_blocked = True
                self._recovery_reason = "clear_unknown_release"
                self.journal.save_recovery(
                    self.session_id,
                    self.current_interaction_id,
                    None,
                    None,
                    "clear_unknown",
                )
                self._persist()
                return
            self._reset_current_locked()
            self._persist()

    def _clear_response_recovery_locked(self) -> None:
        self._recovery_blocked = False
        self._recovery_reason = None
        self._pending_response_payload = None
        self._pending_response_message = None
        self._pending_response_decision = None
        self._pending_response_event_id = None
        self.journal.clear_recovery(self.session_id)

    async def _recover_response_locked(self) -> None:
        payload = self._pending_response_payload
        message = self._pending_response_message
        decision = self._pending_response_decision
        if payload is None or message is None or decision is None:
            return
        result, failure = await self._call_response_with_retry_locked(payload)
        if self.current_interaction_id is None:
            if failure is None or isinstance(failure, MedicationBusinessError):
                self.current_turn_id = None
                self._turn_message_id = None
                self._clear_response_recovery_locked()
                self._persist()
            return
        if failure is not None:
            if not isinstance(failure, MedicationBusinessError):
                return
            await self._send_turn_result_locked("error", reply_to=message["message_id"])
            self._save_turn_decision_locked("error", None)
            self._clear_response_recovery_locked()
            await self._finish_with_result_locked(
                "这条用药提醒已经失效了，请让家人或工作台查看。"
            )
            return
        await self._complete_response_locked(message, decision, result)

    async def _recover_clear_locked(self) -> None:
        if self.current_interaction_id is None:
            self._recovery_blocked = False
            self._recovery_reason = None
            return
        if self._binding_installed and not await self._clear_binding_locked("recovery"):
            return
        result_text = self._pending_result_text
        result_revision = self._pending_result_revision
        self._recovery_blocked = False
        self._recovery_reason = None
        self.journal.clear_recovery(self.session_id)
        if result_text is not None:
            self.current_playback_id = _new_id("playback")
            self._pending_result_text = None
            self._pending_result_revision = None
            await self._send_speak_locked(
                purpose="result",
                text=result_text,
                attempt_id=None,
                binding_revision=result_revision or self._interaction_binding_revision,
            )
            return
        self._released.add(str(self.current_interaction_id))
        self._reset_current_locked()
        await self._start_next_locked()

    async def _terminate_invalid_locked(self, reason: str) -> None:
        if not await self._clear_binding_locked(reason):
            self._recovery_blocked = True
            self._recovery_reason = "clear_unknown_failure"
            self.journal.save_recovery(
                self.session_id,
                self.current_interaction_id,
                None,
                None,
                "clear_unknown",
            )
            self._persist()
            return
        self._released.add(str(self.current_interaction_id))
        self._reset_current_locked()
        self._persist()

    async def reconcile(self) -> None:
        await self._run_serial(self._reconcile_serial)

    async def _reconcile_serial(self) -> None:
        if not self._stopped:
            await self._recover_device_events_locked()
            if self._recovery_blocked:
                if self._recovery_reason == "response_unknown":
                    await self._recover_response_locked()
                elif self._recovery_reason and self._recovery_reason.startswith("clear_unknown"):
                    await self._recover_clear_locked()
                elif self._recovery_reason == "repeat_unknown":
                    await self._repeat_locked()
                return
            if self.current_interaction_id is None:
                return
            if self.state == BridgeState.PROCESSING_RESPONSE:
                return
            notification = self._current_notification or {}
            if not await self._service_recheck_locked(notification):
                if not await self._clear_binding_locked("expired"):
                    self._recovery_blocked = True
                    self._recovery_reason = "clear_unknown_expired"
                    self.journal.save_recovery(
                        self.session_id,
                        self.current_interaction_id,
                        None,
                        None,
                        "clear_unknown",
                    )
                    self._persist()
                    return
                self._released.add(str(self.current_interaction_id))
                self._reset_current_locked()
                await self._start_next_locked()

    async def _send_turn_result_locked(
        self,
        decision: str,
        *,
        reply_to: str,
        business_status: str | None = None,
    ) -> None:
        message = make_message(
            "turn.result",
            self.session_id,
            {"decision": decision, **({"business_status": business_status} if business_status else {})},
            interaction_id=str(self.current_interaction_id),
            binding_revision=self.binding_revision,
            reply_to=reply_to,
        )
        self._last_result_message_id = message["message_id"]
        await self._send(message)

    async def _send_error_locked(self, code: str, detail: str, request: dict | None = None) -> None:
        kwargs = {}
        if request and request.get("interaction_id") and request.get("binding_revision"):
            kwargs = {
                "interaction_id": request["interaction_id"],
                "binding_revision": request["binding_revision"],
                "reply_to": request.get("message_id"),
            }
        message = make_message(
            "error",
            self.session_id,
            {"code": code, "message": detail},
            **kwargs,
        )
        await self._send(message)

    def _reset_current_locked(self) -> None:
        self.state = BridgeState.IDLE
        self.current_interaction_id = None
        self.current_playback_id = None
        self.current_turn_id = None
        self._current_notification = None
        self._current_playback = None
        self._playback_binding_revision = None
        self._interaction_binding_revision = None
        self._turn_message_id = None
        self._last_result_message_id = None
        self._pending_user = None
        self._pending_response_payload = None
        self._pending_response_message = None
        self._pending_response_decision = None
        self._pending_response_event_id = None
        self._pending_result_text = None
        self._pending_result_revision = None
        self._binding_installed = False
        self._persist()

    async def close(self) -> None:
        await self._run_serial(self._close_serial, allow_stopped=True)
        executor = self._executor_task
        if executor is not None and executor is not asyncio.current_task():
            executor.cancel()
            try:
                await executor
            except asyncio.CancelledError:
                pass
        self._executor_task = None
        return

    async def _close_serial(self) -> None:
        if not self._stopped:
            self._stopped = True
            if self.current_turn_id:
                self.journal.save_recovery(
                    self.session_id,
                    self.current_interaction_id,
                    self.current_turn_id,
                    None,
                    "session_closed",
                )
            self._persist()
