"""aiohttp WebSocket server and Bridge session transport."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging

from .config import BridgeConfig
from .delivery_journal import DeliveryJournal, JournalConflict
from .medication_client import MedicationClient
from .protocol import ProtocolError, decode_message, encode_message, make_message
from .reminder_poller import ReminderPoller
from .session_registry import RegisteredIdentity, SessionRegistry, SessionRegistryError
from .session_state_machine import InteractionStateMachine

try:  # Imported lazily by the deployment process; unit tests need no web server.
    from aiohttp import WSMsgType, web
except ImportError:  # pragma: no cover - documented by requirements.txt
    WSMsgType = None
    web = None

logger = logging.getLogger(__name__)


class BridgeSession:
    def __init__(
        self,
        identity: RegisteredIdentity,
        websocket,
        medication: MedicationClient,
        journal: DeliveryJournal,
        config: BridgeConfig,
    ) -> None:
        self.identity = identity
        self.session_id = identity.session_id
        self.websocket = websocket
        self.medication = medication
        self.journal = journal
        self.config = config
        self._send_lock = asyncio.Lock()
        self._ack_futures: dict[str, asyncio.Future[bool]] = {}
        self._operations: set[asyncio.Task] = set()
        self.closed = False
        self.state_machine = InteractionStateMachine(
            session_id=identity.session_id,
            elder_id=identity.elder_id,
            device_sn=identity.device_sn,
            medication=medication,
            journal=journal,
            send=self.send,
            wait_ack=self.wait_ack,
            ack_timeout=config.request_timeout_seconds,
        )

    def _spawn_operation(self, operation) -> None:
        task = asyncio.create_task(operation)
        self._operations.add(task)

        def finished(done: asyncio.Task) -> None:
            self._operations.discard(done)
            if not done.cancelled():
                error = done.exception()
                if error is not None:
                    logger.error("bridge session operation failed: %s", error)

        task.add_done_callback(finished)

    async def send(self, message: dict) -> None:
        if self.closed:
            raise RuntimeError("bridge session is closed")
        self.journal.record_message(message, "bridge", "sent")
        if message["type"] in {"interaction.bind", "interaction.clear"}:
            loop = asyncio.get_running_loop()
            self._ack_futures.setdefault(message["message_id"], loop.create_future())
        encoded = encode_message(message, self.config.max_message_bytes)
        async with self._send_lock:
            await self.websocket.send_str(encoded)

    async def wait_ack(self, message: dict) -> bool:
        future = self._ack_futures.get(message["message_id"])
        if future is None:
            return False
        try:
            return bool(await future)
        finally:
            self._ack_futures.pop(message["message_id"], None)

    async def _ack(self, request: dict) -> None:
        fields = {}
        if request.get("interaction_id") and request.get("binding_revision"):
            fields = {
                "interaction_id": request["interaction_id"],
                "binding_revision": request["binding_revision"],
            }
        await self.send(
            make_message(
                "message.ack",
                self.session_id,
                {"status": "applied"},
                reply_to=request["message_id"],
                **fields,
            )
        )

    async def _error(self, code: str, detail: str, request: dict | None = None) -> None:
        fields = {}
        reply_to = None
        if request:
            reply_to = request.get("message_id")
            if request.get("interaction_id") and request.get("binding_revision"):
                fields = {
                    "interaction_id": request["interaction_id"],
                    "binding_revision": request["binding_revision"],
                }
        await self.send(
            make_message(
                "error",
                self.session_id,
                {"code": code, "message": detail},
                reply_to=reply_to,
                **fields,
            )
        )

    async def receive(self, message: dict) -> bool:
        try:
            status = self.journal.record_message(message, "livekit", "received")
        except JournalConflict:
            await self._error("message_conflict", "message_id was reused with different content", message)
            return True
        if status == "duplicate":
            # The original operation remains authoritative. A duplicate packet
            # is acknowledged but never invokes the Medication API again.
            if message["type"] not in {"message.ack", "session.close"}:
                await self._ack(message)
            return True

        message_type = message["type"]
        if message_type == "message.ack":
            reply_to = message.get("reply_to")
            future = self._ack_futures.get(reply_to)
            if future is not None and not future.done():
                future.set_result(message.get("payload", {}).get("status") == "applied")
            return True
        if message_type == "user_text":
            await self._ack(message)
            self._spawn_operation(self.state_machine.handle_user_text(message))
            return True
        if message_type == "playback_status":
            await self._ack(message)
            self._spawn_operation(self.state_machine.handle_playback_status(message))
            return True
        if message_type == "interaction.release":
            await self._ack(message)
            self._spawn_operation(self.state_machine.handle_release(message))
            return True
        if message_type == "session.close":
            await self._ack(message)
            return False
        await self._error("unexpected_message", f"unsupported inbound type: {message_type}", message)
        return True

    async def run(self) -> None:
        ready = make_message("session.ready", self.session_id, {"status": "ready"})
        await self.send(ready)
        async for packet in self.websocket:
            if packet.type == WSMsgType.TEXT:
                try:
                    message = decode_message(packet.data, self.config.max_message_bytes)
                except ProtocolError as exc:
                    await self._error(exc.code, str(exc))
                    continue
                if message.get("session_id") != self.session_id:
                    await self._error("session_mismatch", "message session_id does not match connection", message)
                    continue
                if not await self.receive(message):
                    break
            elif packet.type == WSMsgType.ERROR:
                logger.warning("LiveKit WebSocket error for session=%s", self.session_id)
                break
            elif packet.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}:
                break
        self.closed = True
        for future in self._ack_futures.values():
            if not future.done():
                future.set_result(False)
        self._ack_futures.clear()
        if self._operations:
            await asyncio.gather(*list(self._operations), return_exceptions=True)
        self._operations.clear()
        await self.state_machine.close()


@dataclass
class BridgeRuntime:
    config: BridgeConfig
    medication: MedicationClient
    journal: DeliveryJournal

    def __post_init__(self) -> None:
        self.registry = SessionRegistry(self.config.test_mapping)
        self.poller = ReminderPoller(self.registry, self.medication, self.config.poll_seconds)

    async def start(self) -> None:
        self.poller.start()

    async def close(self) -> None:
        await self.poller.stop()
        self.journal.close()


def _authorized(request, token: str) -> bool:
    if not token:
        return True
    supplied = request.headers.get("X-Text-Bridge-Token", "")
    if not supplied:
        authorization = request.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            supplied = authorization[7:]
    return supplied == token


def create_app(
    config: BridgeConfig | None = None,
    *,
    medication: MedicationClient | None = None,
    journal: DeliveryJournal | None = None,
):
    if web is None:  # pragma: no cover
        raise RuntimeError("aiohttp is required to run the text bridge")
    config = config or BridgeConfig.from_env()
    runtime = BridgeRuntime(
        config,
        medication or MedicationClient(config.medication_service_url, config.request_timeout_seconds),
        journal or DeliveryJournal(config.journal_path),
    )
    app = web.Application()
    app["bridge_runtime"] = runtime

    async def health(_request):
        return web.json_response({"status": "ok" if config.enabled else "disabled"})

    async def websocket_handler(request):
        if not config.enabled:
            return web.json_response({"error": "text bridge is disabled"}, status=503)
        if not _authorized(request, config.token):
            return web.json_response({"error": "unauthorized"}, status=401)
        ws = web.WebSocketResponse(max_msg_size=config.max_message_bytes, heartbeat=30.0)
        await ws.prepare(request)
        identity = None
        bridge_session = None
        try:
            first = await ws.receive()
            if first.type != WSMsgType.TEXT:
                await ws.close(code=1002, message=b"session.register required")
                return ws
            try:
                register = decode_message(first.data, config.max_message_bytes)
                if register["type"] != "session.register":
                    raise ProtocolError("first message must be session.register", "registration_required")
                identity = await runtime.registry.register(register["session_id"], register["payload"])
                runtime.journal.record_message(register, "livekit", "received")
            except (ProtocolError, SessionRegistryError, JournalConflict) as exc:
                code = getattr(exc, "code", "registration_failed")
                await ws.send_str(
                    encode_message(make_message("error", register.get("session_id", "unknown") if "register" in locals() else "unknown", {"code": code, "message": str(exc)}))
                )
                await ws.close(code=1008, message=str(exc).encode("utf-8")[:120])
                return ws
            bridge_session = BridgeSession(identity, ws, runtime.medication, runtime.journal, config)
            await bridge_session.run()
        finally:
            if identity is not None:
                await runtime.registry.remove(identity.session_id)
            if bridge_session is not None:
                bridge_session.closed = True
        return ws

    app.router.add_get("/health/live", health)
    app.router.add_get("/ws", websocket_handler)

    async def startup(_app):
        await runtime.start()

    async def cleanup(_app):
        await runtime.close()

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app


def run(config: BridgeConfig | None = None) -> None:
    if web is None:  # pragma: no cover
        raise RuntimeError("aiohttp is required to run the text bridge")
    cfg = config or BridgeConfig.from_env()
    logging.basicConfig(level=logging.INFO)
    web.run_app(create_app(cfg), host=cfg.bind_host, port=cfg.bind_port)
