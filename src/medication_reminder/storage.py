"""SQLite persistence used by the development MVP.

The repository deliberately keeps transactions explicit. The same SQL
transaction contains domain state, audit event and outbox writes, which is
the part that can later be mapped to PostgreSQL without changing the domain
services.
"""

from contextlib import contextmanager
import sqlite3
from pathlib import Path
from threading import Lock, RLock, local


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS medication_plan (
    plan_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    elder_id TEXT NOT NULL,
    drug_name TEXT NOT NULL,
    dosage_text TEXT NOT NULL,
    route TEXT NOT NULL,
    schedule_type TEXT NOT NULL,
    schedule_time TEXT NOT NULL,
    timezone TEXT NOT NULL,
    relation_to_meal TEXT,
    start_date TEXT NOT NULL,
    end_date TEXT,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    created_by TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT,
    effective_from TEXT,
    device_sn TEXT,
    confirmation_window_minutes INTEGER NOT NULL DEFAULT 120,
    max_snooze_count INTEGER NOT NULL DEFAULT 3,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (plan_id, version)
);

CREATE INDEX IF NOT EXISTS idx_plan_elder_status
    ON medication_plan (elder_id, status);

CREATE TABLE IF NOT EXISTS medication_safety_check (
    check_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    ruleset_version TEXT NOT NULL,
    ruleset_fingerprint TEXT,
    coverage_json TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    trace_id TEXT,
    check_failed INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_safety_check_plan_version
    ON medication_safety_check (plan_id, plan_version, checked_at DESC);

CREATE TABLE IF NOT EXISTS medication_safety_finding (
    finding_id TEXT PRIMARY KEY,
    check_id TEXT NOT NULL,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    rule_id TEXT,
    rule_version TEXT,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (check_id) REFERENCES medication_safety_check(check_id)
);

CREATE INDEX IF NOT EXISTS idx_safety_finding_check
    ON medication_safety_finding (check_id, severity, created_at);

CREATE TABLE IF NOT EXISTS medication_occurrence (
    occurrence_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    elder_id TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    confirmation_deadline_at TEXT NOT NULL,
    next_reminder_at TEXT NOT NULL,
    intake_status TEXT NOT NULL,
    actual_time TEXT,
    confirmation_method TEXT,
    reminder_count INTEGER NOT NULL DEFAULT 0,
    snooze_count INTEGER NOT NULL DEFAULT 0,
    max_snooze_count INTEGER NOT NULL DEFAULT 3,
    max_snooze_until TEXT NOT NULL,
    reminder_claimed_at TEXT,
    drug_name_snapshot TEXT NOT NULL,
    dosage_snapshot TEXT NOT NULL,
    relation_to_meal_snapshot TEXT,
    cancel_reason TEXT,
    cancelled_by_safety_check_id TEXT,
    late_verified_taken_at TEXT,
    late_verified_by TEXT,
    late_verified_source TEXT,
    late_verified_note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (plan_id, plan_version, scheduled_at)
);

CREATE INDEX IF NOT EXISTS idx_occurrence_due
    ON medication_occurrence (intake_status, next_reminder_at);
CREATE INDEX IF NOT EXISTS idx_occurrence_elder_time
    ON medication_occurrence (elder_id, scheduled_at);

CREATE TABLE IF NOT EXISTS medication_escalation (
    escalation_id TEXT PRIMARY KEY,
    elder_id TEXT NOT NULL,
    occurrence_id TEXT NOT NULL UNIQUE,
    interaction_id TEXT,
    plan_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    reason TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    current_level TEXT NOT NULL,
    status TEXT NOT NULL,
    needs_manual_review INTEGER NOT NULL DEFAULT 0,
    opened_at TEXT NOT NULL,
    next_escalation_at TEXT,
    resolution_deadline_at TEXT,
    acknowledged_at TEXT,
    resolved_at TEXT,
    acknowledged_by TEXT,
    resolved_by TEXT,
    resolution_code TEXT,
    resolution_note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_escalation_elder_status
    ON medication_escalation (elder_id, status, current_level);
CREATE INDEX IF NOT EXISTS idx_escalation_due
    ON medication_escalation (status, next_escalation_at);

CREATE TABLE IF NOT EXISTS medication_escalation_step (
    step_id TEXT PRIMARY KEY,
    escalation_id TEXT NOT NULL,
    level TEXT NOT NULL,
    target_role TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    executed_at TEXT,
    event_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    UNIQUE (escalation_id, level)
);

CREATE INDEX IF NOT EXISTS idx_escalation_step_history
    ON medication_escalation_step (escalation_id, scheduled_at);

CREATE TABLE IF NOT EXISTS reminder_attempt (
    attempt_id TEXT PRIMARY KEY,
    occurrence_id TEXT NOT NULL,
    interaction_id TEXT NOT NULL,
    level INTEGER NOT NULL,
    scheduled_at TEXT NOT NULL,
    delivery_status TEXT NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    started_at TEXT,
    completed_at TEXT,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attempt_occurrence
    ON reminder_attempt (occurrence_id, created_at);

CREATE TABLE IF NOT EXISTS medication_interaction (
    interaction_id TEXT PRIMARY KEY,
    elder_id TEXT NOT NULL,
    device_sn TEXT,
    occurrence_id TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_interaction_occurrence
    ON medication_interaction (occurrence_id, status);
CREATE INDEX IF NOT EXISTS idx_interaction_elder_status_expiry
    ON medication_interaction (elder_id, status, expires_at);

CREATE TABLE IF NOT EXISTS medication_event_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    elder_id TEXT,
    plan_id TEXT,
    occurrence_id TEXT,
    event_type TEXT NOT NULL,
    source TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    processed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_log_occurrence
    ON medication_event_log (occurrence_id, created_at);
CREATE INDEX IF NOT EXISTS idx_event_log_elder_log
    ON medication_event_log (elder_id, log_id DESC);

CREATE TABLE IF NOT EXISTS domain_outbox (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    dedup_key TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    locked_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    published_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON domain_outbox (status, created_at);
"""


class Storage:
    """SQLite storage with one connection per worker thread.

    SQLite still serializes writes, but WAL mode allows independent read
    connections to run while a writer is active. The previous implementation
    put one ``RLock`` in front of the single connection, which made every HTTP
    request and the scheduler wait in one process-wide queue.

    ``:memory:`` remains a single protected connection because an ordinary
    in-memory SQLite database is private to one connection. File databases
    use thread-local connections instead.
    """

    def __init__(self, database: str):
        self.database = str(database)
        self._local = local()
        self._connections_lock = Lock()
        self._memory_lock = RLock()
        self._connections = []
        self._closed = False
        self._shared_memory = self.database == ":memory:"
        if not self._shared_memory:
            Path(self.database).parent.mkdir(parents=True, exist_ok=True)
            bootstrap = self._new_connection()
            try:
                bootstrap.execute("PRAGMA journal_mode=WAL")
                bootstrap.execute("PRAGMA synchronous=NORMAL")
                bootstrap.executescript(SCHEMA)
                self._ensure_schema_evolution(bootstrap)
            finally:
                self._discard_connection(bootstrap)
        else:
            # Keep one connection alive so all HTTP worker threads see the
            # same in-memory DB.  Access is serialized by _memory_guard.
            self._memory_connection = self._new_connection()
            with self._memory_lock:
                self._memory_connection.executescript(SCHEMA)
                self._ensure_schema_evolution(self._memory_connection)

    def _ensure_schema_evolution(self, connection):
        """Add nullable Phase 2.1 columns without rebuilding user databases."""

        migrations = {
            "medication_safety_check": {
                "ruleset_fingerprint": "TEXT",
            },
            "medication_occurrence": {
                "cancel_reason": "TEXT",
                "cancelled_by_safety_check_id": "TEXT",
                "late_verified_taken_at": "TEXT",
                "late_verified_by": "TEXT",
                "late_verified_source": "TEXT",
                "late_verified_note": "TEXT",
            },
            "medication_escalation": {
                "resolution_deadline_at": "TEXT",
            },
        }
        for table, columns in migrations.items():
            existing = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(%s)" % table).fetchall()
            }
            for name, definition in columns.items():
                if name not in existing:
                    connection.execute(
                        "ALTER TABLE %s ADD COLUMN %s %s" % (table, name, definition)
                    )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_escalation_resolution_due "
            "ON medication_escalation (status, resolution_deadline_at)"
        )
        connection.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS trg_plan_insert_active_requires_safety
            BEFORE INSERT ON medication_plan
            WHEN NEW.status='active' AND NOT EXISTS (
                SELECT 1 FROM medication_safety_check
                WHERE plan_id=NEW.plan_id AND plan_version=NEW.version
                  AND status IN ('PASS', 'WARN') AND check_failed=0
                  AND ruleset_fingerprint IS NOT NULL
            )
            BEGIN
                SELECT RAISE(ABORT, 'active plan requires a current safety check');
            END;

            CREATE TRIGGER IF NOT EXISTS trg_plan_update_active_requires_safety
            BEFORE UPDATE OF status ON medication_plan
            WHEN NEW.status='active' AND NOT EXISTS (
                SELECT 1 FROM medication_safety_check
                WHERE plan_id=NEW.plan_id AND plan_version=NEW.version
                  AND status IN ('PASS', 'WARN') AND check_failed=0
                  AND ruleset_fingerprint IS NOT NULL
            )
            BEGIN
                SELECT RAISE(ABORT, 'active plan requires a current safety check');
            END;

            CREATE TRIGGER IF NOT EXISTS trg_active_plan_sensitive_update_frozen
            BEFORE UPDATE OF elder_id, drug_name, dosage_text, route, schedule_type,
                schedule_time, timezone, relation_to_meal, start_date, end_date,
                device_sn, confirmation_window_minutes, max_snooze_count
                ON medication_plan
            WHEN OLD.status='active' AND (
                NEW.elder_id IS NOT OLD.elder_id OR
                NEW.drug_name IS NOT OLD.drug_name OR
                NEW.dosage_text IS NOT OLD.dosage_text OR
                NEW.route IS NOT OLD.route OR
                NEW.schedule_type IS NOT OLD.schedule_type OR
                NEW.schedule_time IS NOT OLD.schedule_time OR
                NEW.timezone IS NOT OLD.timezone OR
                NEW.relation_to_meal IS NOT OLD.relation_to_meal OR
                NEW.start_date IS NOT OLD.start_date OR
                NEW.end_date IS NOT OLD.end_date OR
                NEW.device_sn IS NOT OLD.device_sn OR
                NEW.confirmation_window_minutes IS NOT OLD.confirmation_window_minutes OR
                NEW.max_snooze_count IS NOT OLD.max_snooze_count
            )
            BEGIN
                SELECT RAISE(ABORT, 'active plan changes require revise_plan');
            END;
            """
        )

    def _new_connection(self):
        connection = sqlite3.connect(
            self.database,
            timeout=10.0,
            check_same_thread=False,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        if not self._shared_memory:
            connection.execute("PRAGMA synchronous=NORMAL")
        with self._connections_lock:
            self._connections.append(connection)
        return connection

    def _discard_connection(self, connection):
        with self._connections_lock:
            if connection in self._connections:
                self._connections.remove(connection)
        connection.close()

    def _connection(self):
        if self._closed:
            raise RuntimeError("storage is closed")
        if self._shared_memory:
            return self._memory_connection
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = self._new_connection()
            self._local.connection = connection
        return connection

    @property
    def connection(self):
        """Return the connection belonging to the current worker thread."""
        return self._connection()

    @contextmanager
    def _memory_guard(self):
        if self._shared_memory:
            with self._memory_lock:
                yield
        else:
            yield

    @contextmanager
    def transaction(self):
        connection = self._connection()
        with self._memory_guard():
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")

    def execute(self, sql, parameters=()):
        with self._memory_guard():
            return self._connection().execute(sql, parameters)

    def fetchone(self, sql, parameters=()):
        with self._memory_guard():
            return self._connection().execute(sql, parameters).fetchone()

    def fetchall(self, sql, parameters=()):
        with self._memory_guard():
            return self._connection().execute(sql, parameters).fetchall()

    def close_thread_connection(self):
        """Release a short-lived HTTP worker's connection after its request."""
        if self._shared_memory:
            return
        connection = getattr(self._local, "connection", None)
        if connection is None:
            return
        with self._connections_lock:
            if connection in self._connections:
                self._connections.remove(connection)
        self._local.connection = None
        connection.close()

    def close(self):
        with self._connections_lock:
            if self._closed:
                return
            self._closed = True
            connections = list(self._connections)
            self._connections.clear()
        for connection in connections:
            connection.close()

