"""Durable, minimal communication journal for Bridge recovery and de-duplication."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any

from .protocol import message_digest


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JournalConflict(RuntimeError):
    """The same stable identifier was used with different content."""


class DeliveryJournal:
    """SQLite journal containing transport facts, not medication business facts.

    ASR text and full medication speech are intentionally represented by hashes
    or bounded summaries. The Medication Service remains the only business
    source of truth.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS bridge_message (
                message_id TEXT PRIMARY KEY,
                digest TEXT NOT NULL,
                direction TEXT NOT NULL,
                message_type TEXT NOT NULL,
                session_id TEXT NOT NULL,
                interaction_id TEXT,
                binding_revision INTEGER,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bridge_turn (
                session_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                interaction_id TEXT NOT NULL,
                binding_revision INTEGER NOT NULL,
                request_digest TEXT NOT NULL,
                state TEXT NOT NULL,
                decision TEXT,
                business_status TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (session_id, turn_id)
            );
            CREATE TABLE IF NOT EXISTS bridge_playback (
                session_id TEXT NOT NULL,
                playback_id TEXT NOT NULL,
                interaction_id TEXT NOT NULL,
                binding_revision INTEGER NOT NULL,
                purpose TEXT NOT NULL,
                message_id TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt_id TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (session_id, playback_id)
            );
            CREATE TABLE IF NOT EXISTS bridge_runtime (
                session_id TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bridge_release (
                session_id TEXT NOT NULL,
                interaction_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (session_id, interaction_id)
            );
            CREATE TABLE IF NOT EXISTS bridge_recovery (
                session_id TEXT PRIMARY KEY,
                interaction_id TEXT,
                turn_id TEXT,
                event_id TEXT,
                reason TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bridge_recovery_request (
                session_id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bridge_device_event (
                session_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                interaction_id TEXT,
                attempt_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                failure_reason TEXT,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (session_id, event_id)
            );
            """
        )

    def record_message(self, message: dict, direction: str, status: str = "accepted") -> str:
        digest = message_digest(message)
        message_id = message["message_id"]
        with self._lock:
            existing = self._connection.execute(
                "SELECT digest FROM bridge_message WHERE message_id=?", (message_id,)
            ).fetchone()
            if existing is not None:
                if existing["digest"] != digest:
                    raise JournalConflict("message_id reused with different content")
                return "duplicate"
            self._connection.execute(
                """INSERT INTO bridge_message
                   (message_id,digest,direction,message_type,session_id,
                    interaction_id,binding_revision,status,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    message_id,
                    digest,
                    direction,
                    message["type"],
                    message["session_id"],
                    message.get("interaction_id"),
                    message.get("binding_revision"),
                    status,
                    _now(),
                ),
            )
        return "new"

    def get_turn(self, session_id: str, turn_id: str) -> dict | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM bridge_turn WHERE session_id=? AND turn_id=?",
                (session_id, turn_id),
            ).fetchone()
        return dict(row) if row else None

    def save_turn(
        self,
        session_id: str,
        turn_id: str,
        event_id: str,
        interaction_id: str,
        binding_revision: int,
        request_text: str,
        state: str,
        decision: str | None = None,
        business_status: str | None = None,
    ) -> None:
        # Only a digest is persisted; the raw ASR text is not a normal log field.
        import hashlib

        digest = hashlib.sha256((request_text or "").encode("utf-8")).hexdigest()
        with self._lock:
            self._connection.execute(
                """INSERT INTO bridge_turn
                   (session_id,turn_id,event_id,interaction_id,binding_revision,
                    request_digest,state,decision,business_status,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(session_id,turn_id) DO UPDATE SET
                    state=excluded.state, decision=excluded.decision,
                    business_status=excluded.business_status, updated_at=excluded.updated_at""",
                (
                    session_id,
                    turn_id,
                    event_id,
                    interaction_id,
                    binding_revision,
                    digest,
                    state,
                    decision,
                    business_status,
                    _now(),
                ),
            )

    def save_playback(
        self,
        session_id: str,
        playback_id: str,
        interaction_id: str,
        binding_revision: int,
        purpose: str,
        message_id: str,
        status: str,
        attempt_id: str | None = None,
    ) -> None:
        with self._lock:
            self._connection.execute(
                """INSERT INTO bridge_playback
                   (session_id,playback_id,interaction_id,binding_revision,purpose,
                    message_id,status,attempt_id,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(session_id,playback_id) DO UPDATE SET
                    status=excluded.status, updated_at=excluded.updated_at""",
                (
                    session_id,
                    playback_id,
                    interaction_id,
                    binding_revision,
                    purpose,
                    message_id,
                    status,
                    attempt_id,
                    _now(),
                ),
            )

    def save_device_event(
        self,
        session_id: str,
        event_id: str,
        interaction_id: str | None,
        attempt_id: str,
        event_type: str,
        failure_reason: str | None,
        status: str,
    ) -> None:
        with self._lock:
            self._connection.execute(
                """INSERT INTO bridge_device_event
                   (session_id,event_id,interaction_id,attempt_id,event_type,
                    failure_reason,status,updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(session_id,event_id) DO UPDATE SET
                    failure_reason=excluded.failure_reason,
                    status=excluded.status, updated_at=excluded.updated_at""",
                (
                    session_id,
                    event_id,
                    interaction_id,
                    attempt_id,
                    event_type,
                    failure_reason,
                    status,
                    _now(),
                ),
            )

    def pending_device_events(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM bridge_device_event WHERE session_id=? AND status=?",
                (session_id, "pending"),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_runtime(self, session_id: str, state: dict[str, Any]) -> None:
        with self._lock:
            self._connection.execute(
                """INSERT INTO bridge_runtime(session_id,state_json,updated_at)
                   VALUES(?,?,?)
                   ON CONFLICT(session_id) DO UPDATE SET
                    state_json=excluded.state_json, updated_at=excluded.updated_at""",
                (session_id, json.dumps(state, ensure_ascii=False), _now()),
            )

    def load_runtime(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT state_json FROM bridge_runtime WHERE session_id=?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["state_json"])
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def mark_released(self, session_id: str, interaction_id: str, reason: str) -> None:
        with self._lock:
            self._connection.execute(
                """INSERT OR REPLACE INTO bridge_release
                   (session_id,interaction_id,reason,created_at) VALUES(?,?,?,?)""",
                (session_id, interaction_id, reason, _now()),
            )

    def is_released(self, session_id: str, interaction_id: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM bridge_release WHERE session_id=? AND interaction_id=?",
                (session_id, interaction_id),
            ).fetchone()
        return row is not None

    def save_recovery(
        self,
        session_id: str,
        interaction_id: str | None,
        turn_id: str | None,
        event_id: str | None,
        reason: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._connection.execute(
                """INSERT INTO bridge_recovery
                   (session_id,interaction_id,turn_id,event_id,reason,updated_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(session_id) DO UPDATE SET
                    interaction_id=excluded.interaction_id, turn_id=excluded.turn_id,
                    event_id=excluded.event_id, reason=excluded.reason,
                    updated_at=excluded.updated_at""",
                (session_id, interaction_id, turn_id, event_id, reason, _now()),
            )
            if payload is not None:
                stored_payload = {
                    key: payload[key]
                    for key in ("event_id", "elder_id", "interaction_id", "action", "delay_minutes", "source", "reply_to", "binding_revision", "turn_id")
                    if key in payload
                }
                if turn_id:
                    stored_payload["turn_id"] = turn_id
                self._connection.execute(
                    """INSERT INTO bridge_recovery_request(session_id,payload_json,updated_at)
                       VALUES(?,?,?)
                       ON CONFLICT(session_id) DO UPDATE SET
                        payload_json=excluded.payload_json, updated_at=excluded.updated_at""",
                    (session_id, json.dumps(stored_payload, ensure_ascii=False), _now()),
                )

    def load_recovery_request(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM bridge_recovery_request WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def clear_recovery(self, session_id: str) -> None:
        with self._lock:
            self._connection.execute("DELETE FROM bridge_recovery WHERE session_id=?", (session_id,))
            self._connection.execute("DELETE FROM bridge_recovery_request WHERE session_id=?", (session_id,))

    def close(self) -> None:
        with self._lock:
            with closing(self._connection):
                pass
