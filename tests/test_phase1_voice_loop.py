import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from medication_reminder.device_adapter import ChatAgentHttpAdapter
from medication_reminder.http import Application
from medication_reminder.service import DomainError, MedicationService, SHANGHAI, iso


class FakeChatAgent:
    """Small HTTP peer used to verify the public reminder contract."""

    def __init__(self, status=200, response=None, delay_seconds=0):
        self.status = status
        self.response = response if response is not None else {"accepted": True}
        self.delay_seconds = delay_seconds
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - stdlib handler API
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    body = None
                owner.requests.append({
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": body,
                })
                if owner.delay_seconds:
                    time.sleep(owner.delay_seconds)
                self.send_response(owner.status)
                if owner.status != 204:
                    encoded = json.dumps(owner.response, ensure_ascii=False).encode("utf-8")
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                else:
                    encoded = b""
                    self.send_header("Content-Length", "0")
                self.end_headers()
                if encoded:
                    try:
                        self.wfile.write(encoded)
                    except BrokenPipeError:
                        pass

            def log_message(self, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return "http://127.0.0.1:%s" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class Phase1VoiceLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.fake = FakeChatAgent()
        self.service = MedicationService(
            str(Path(self.temp_dir.name) / "medication.db"),
            device_adapter=ChatAgentHttpAdapter(
                base_url=self.fake.base_url,
                reminder_path="/reminders",
                timeout_seconds=0.2,
            ),
        )
        self.local_now = datetime.now(SHANGHAI).replace(second=0, microsecond=0)

    def tearDown(self):
        self.service.close()
        self.fake.close()
        self.temp_dir.cleanup()

    def _prepare_reminder(self, elder_id="E001"):
        draft = self.service.create_draft({
            "elder_id": elder_id,
            "drug_name": "二甲双胍",
            "dosage_text": "1片",
            "schedule_time": self.local_now.strftime("%H:%M"),
            "start_date": self.local_now.date().isoformat(),
            "relation_to_meal": "餐后服用",
            "created_by": "family:F001",
            "device_sn": "chat-device-001",
        })
        self.service.submit_plan(draft["plan_id"], draft["version"])
        self.service.approve_plan(
            draft["plan_id"], "doctor:D001", draft["version"],
            self.local_now.astimezone(timezone.utc),
        )
        occurrence = self.service.get_today(elder_id)[0]
        scheduled = datetime.fromisoformat(occurrence["scheduled_at"])
        self.service.run_scheduler_cycle(scheduled + timedelta(minutes=1))
        current = self.service.get_occurrence(occurrence["occurrence_id"])
        attempt = current["reminder_attempts"][-1]
        interaction = current["interactions"][-1]
        return current, attempt, interaction

    def _device_event(self, interaction, attempt, status, event_id=None, elder_id="E001"):
        return self.service.process_device_event({
            "event_id": event_id or "device-%s" % status,
            "event_type": status.upper(),
            "elder_id": elder_id,
            "interaction_id": interaction["interaction_id"],
            "occurrence_id": interaction["occurrence_id"],
            "attempt_id": attempt["attempt_id"],
            "timestamp": iso(datetime.now(timezone.utc)),
            "trace_id": "trace-phase1",
            "source": "fake_chat_agent",
        })

    def test_scheduler_http_adapter_marks_dispatched_and_sends_contract(self):
        occurrence, attempt, interaction = self._prepare_reminder()
        self.assertEqual(attempt["delivery_status"], "dispatched")
        self.assertEqual(len(self.fake.requests), 1)
        request = self.fake.requests[0]
        self.assertEqual(request["path"], "/reminders")
        body = request["body"]
        self.assertEqual(body["event_type"], "device.interaction.request")
        self.assertEqual(body["elder_id"], "E001")
        self.assertEqual(body["interaction_id"], interaction["interaction_id"])
        self.assertEqual(body["occurrence_id"], occurrence["occurrence_id"])
        self.assertEqual(body["plan_id"], occurrence["plan_id"])
        self.assertEqual(body["medication"]["name"], "二甲双胍")
        self.assertEqual(body["medication"]["dose"], "1片")
        self.assertIn("二甲双胍", body["reminder_text"])

    def test_started_then_completed_only_changes_delivery_state(self):
        occurrence, attempt, interaction = self._prepare_reminder()
        started = self._device_event(interaction, attempt, "started")
        self.assertEqual(started["attempt"]["delivery_status"], "started")
        completed = self._device_event(interaction, attempt, "completed")
        self.assertEqual(completed["attempt"]["delivery_status"], "completed")
        self.assertEqual(
            self.service.get_occurrence(occurrence["occurrence_id"])["intake_status"],
            "unconfirmed",
        )

    def test_agent_fast_path_confirms_bound_occurrence(self):
        occurrence, attempt, interaction = self._prepare_reminder()
        self._device_event(interaction, attempt, "started")
        self._device_event(interaction, attempt, "completed")
        app = Application(self.service)
        status, result = app.handle(
            "POST", "/api/v1/medication/agent/message",
            body={
                "elder_id": "E001",
                "interaction_id": interaction["interaction_id"],
                "text": "晓得了，我已经吃了",
                "source": "chat_agent",
                "event_id": "asr-response-001",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["semantic_source"], "fast_path")
        self.assertEqual(result["action"], "CONFIRM_TAKEN")
        self.assertEqual(result["occurrence"]["intake_status"], "confirmed_taken")

    def test_delay_keeps_schedule_and_changes_next_reminder(self):
        occurrence, _attempt, interaction = self._prepare_reminder()
        app = Application(self.service)
        status, result = app.handle(
            "POST", "/api/v1/medication/agent/message",
            body={
                "elder_id": "E001",
                "interaction_id": interaction["interaction_id"],
                "text": "等十分钟",
                "source": "chat_agent",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["action"], "DELAY")
        self.assertEqual(result["occurrence"]["intake_status"], "unconfirmed")
        self.assertEqual(result["occurrence"]["scheduled_at"], occurrence["scheduled_at"])
        self.assertNotEqual(result["occurrence"]["next_reminder_at"], occurrence["next_reminder_at"])

    def test_skip_marks_only_bound_occurrence(self):
        occurrence, _attempt, interaction = self._prepare_reminder()
        result = self.service.process_user_response({
            "elder_id": "E001",
            "interaction_id": interaction["interaction_id"],
            "text": "这次不吃了",
        })
        self.assertEqual(result["action"], "SKIP")
        self.assertEqual(result["occurrence"]["occurrence_id"], occurrence["occurrence_id"])
        self.assertEqual(result["occurrence"]["intake_status"], "skipped")

    def test_repeat_returns_text_without_changing_occurrence_or_creating_request(self):
        occurrence, _attempt, interaction = self._prepare_reminder()
        before = self.service.get_occurrence(occurrence["occurrence_id"])
        request_count = len(self.service.list_event_log("device.interaction.request"))
        result = self.service.process_user_response({
            "elder_id": "E001",
            "interaction_id": interaction["interaction_id"],
            "text": "再说一遍",
        })
        after = self.service.get_occurrence(occurrence["occurrence_id"])
        self.assertEqual(result["action"], "REPEAT")
        self.assertIn("二甲双胍", result["reminder_text"])
        self.assertEqual(before["intake_status"], after["intake_status"])
        self.assertEqual(before["next_reminder_at"], after["next_reminder_at"])
        self.assertEqual(request_count, len(self.service.list_event_log("device.interaction.request")))

    def test_invalid_binding_and_elder_are_rejected_without_mutation(self):
        occurrence, _attempt, interaction = self._prepare_reminder()
        before = self.service.get_occurrence(occurrence["occurrence_id"])
        with self.assertRaises(DomainError):
            self.service.process_user_response({
                "elder_id": "E001",
                "interaction_id": "not-the-real-interaction",
                "text": "我已经吃了",
            })
        with self.assertRaises(DomainError):
            self.service.process_user_response({
                "elder_id": "E999",
                "interaction_id": interaction["interaction_id"],
                "text": "我已经吃了",
            })
        after = self.service.get_occurrence(occurrence["occurrence_id"])
        self.assertEqual(before["intake_status"], after["intake_status"])

    def test_expired_interaction_is_rejected(self):
        occurrence, _attempt, interaction = self._prepare_reminder()
        expired_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        with self.service.storage.transaction() as connection:
            connection.execute(
                "UPDATE medication_interaction SET expires_at=? WHERE interaction_id=?",
                (iso(expired_at), interaction["interaction_id"]),
            )
        with self.assertRaises(DomainError):
            self.service.process_user_response({
                "elder_id": "E001",
                "interaction_id": interaction["interaction_id"],
                "text": "我已经吃了",
            })
        self.assertEqual(
            self.service.get_occurrence(occurrence["occurrence_id"])["intake_status"],
            "unconfirmed",
        )

    def test_duplicate_device_event_is_idempotent(self):
        _occurrence, attempt, interaction = self._prepare_reminder()
        first = self._device_event(interaction, attempt, "started", event_id="same-device-event")
        duplicate = self._device_event(interaction, attempt, "started", event_id="same-device-event")
        self.assertFalse(first["duplicate"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["attempt"]["delivery_status"], "started")
        self.assertEqual(len(self.service.list_event_log("device.reminder.started")), 1)

    def test_timeout_keeps_outbox_pending_and_never_confirms(self):
        slow_fake = FakeChatAgent(delay_seconds=0.1)
        self.service.device_adapter = ChatAgentHttpAdapter(
            base_url=slow_fake.base_url,
            reminder_path="/reminders",
            timeout_seconds=0.01,
        )
        occurrence, attempt, _interaction = self._prepare_reminder()
        outbox = [
            item for item in self.service.list_outbox()
            if item["event_type"] == "device.interaction.request"
        ]
        self.assertTrue(outbox)
        self.assertEqual(outbox[-1]["status"], "pending")
        self.assertNotEqual(attempt["delivery_status"], "completed")
        self.assertEqual(
            self.service.get_occurrence(occurrence["occurrence_id"])["intake_status"],
            "unconfirmed",
        )
        slow_fake.close()

    def test_http_4xx_marks_attempt_and_outbox_failed(self):
        self.fake.status = 400
        occurrence, attempt, _interaction = self._prepare_reminder()
        request_outbox = [
            item for item in self.service.list_outbox()
            if item["event_type"] == "device.interaction.request"
        ]
        self.assertTrue(request_outbox)
        self.assertEqual(request_outbox[-1]["status"], "failed")
        current = self.service.get_occurrence(occurrence["occurrence_id"])
        self.assertEqual(current["reminder_attempts"][-1]["delivery_status"], "failed")
        self.assertEqual(current["intake_status"], "unconfirmed")
        self.assertTrue(self.service.list_event_log("device.reminder.failed"))

    def test_full_fake_chat_agent_loop_reaches_confirmed_taken(self):
        occurrence, attempt, interaction = self._prepare_reminder()
        self._device_event(interaction, attempt, "started")
        self._device_event(interaction, attempt, "completed")
        app = Application(self.service)
        status, response = app.handle(
            "POST", "/api/v1/medication/agent/message",
            body={
                "elder_id": "E001",
                "interaction_id": interaction["interaction_id"],
                "text": "晓得了，我已经吃了",
                "source": "chat_agent",
                "trace_id": "trace-phase1",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(response["occurrence"]["intake_status"], "confirmed_taken")
        events = self.service.list_event_log(occurrence_id=occurrence["occurrence_id"], limit=100)
        event_types = [item["event_type"] for item in events]
        for event_type in (
            "medication.reminder_due",
            "device.interaction.request",
            "device.reminder.started",
            "device.reminder.completed",
            "medication.user_response",
            "medication.intake.updated",
        ):
            self.assertIn(event_type, event_types)


if __name__ == "__main__":
    unittest.main()

