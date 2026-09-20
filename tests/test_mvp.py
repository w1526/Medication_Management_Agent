import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from medication_reminder.http import Application
from medication_reminder.service import MedicationService, SHANGHAI, iso


class MedicationMVPTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "medication.db")
        self.service = MedicationService(self.db_path)
        self.local_now = datetime.now(SHANGHAI).replace(second=0, microsecond=0)
        self.start_date = self.local_now.date().isoformat()

    def tearDown(self):
        self.service.close()
        self.temp_dir.cleanup()

    def _draft(self, schedule_time=None, elder="E001"):
        return self.service.create_draft({
            "elder_id": elder,
            "drug_name": "氨氯地平",
            "dosage_text": "5mg",
            "schedule_time": schedule_time or self.local_now.strftime("%H:%M"),
            "start_date": self.start_date,
            "relation_to_meal": "餐后",
            "created_by": "family:F001",
            "device_sn": "device-001",
        })

    def _approve(self, draft):
        self.service.submit_plan(draft["plan_id"], draft["version"])
        return self.service.approve_plan(
            draft["plan_id"], "doctor:D001", draft["version"], self.local_now.astimezone(timezone.utc)
        )

    def _first_occurrence(self, plan_id):
        items = self.service.get_today("E001")
        for item in items:
            if item["plan_id"] == plan_id:
                return item
        self.fail("expected occurrence for plan %s" % plan_id)

    def test_draft_requires_approval_and_generates_occurrences(self):
        draft = self._draft()
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(self.service.get_today("E001"), [])

        active = self._approve(draft)
        self.assertEqual(active["status"], "active")
        occurrence = self._first_occurrence(draft["plan_id"])
        self.assertEqual(occurrence["plan_version"], 1)
        self.assertEqual(occurrence["intake_status"], "unconfirmed")

    def test_scheduler_claims_once_and_publishes_device_request(self):
        active = self._approve(self._draft())
        occurrence = self._first_occurrence(active["plan_id"])
        scheduled = datetime.fromisoformat(occurrence["scheduled_at"])
        result = self.service.run_scheduler_cycle(scheduled + timedelta(minutes=1))
        self.assertEqual(len(result["reminders_claimed"]), 1)
        self.assertEqual(len(self.service.list_event_log("medication.reminder_due")), 1)

        second = self.service.run_scheduler_cycle(scheduled + timedelta(minutes=2))
        self.assertEqual(second["reminders_claimed"], [])
        self.service.publish_outbox()
        requests = self.service.list_event_log("device.interaction.request")
        self.assertEqual(len(requests), 1)
        current = self.service.get_occurrence(occurrence["occurrence_id"])
        self.assertEqual(current["reminder_attempts"][0]["delivery_status"], "dispatched")
        notifications = self.service.list_active_notifications("E001")
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]["interaction_id"], current["interactions"][0]["interaction_id"])
        self.assertIn("氨氯地平", notifications[0]["text"])

    def test_notification_http_endpoint_exposes_active_interaction(self):
        active = self._approve(self._draft())
        occurrence = self._first_occurrence(active["plan_id"])
        scheduled = datetime.fromisoformat(occurrence["scheduled_at"])
        self.service.run_scheduler_cycle(scheduled + timedelta(minutes=1))
        app = Application(self.service)
        status, payload = app.handle(
            "GET", "/api/v1/medication/notifications", query={"elder_id": "E001"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 1)
        interaction_id = payload["items"][0]["interaction_id"]
        status, response = app.handle(
            "POST", "/api/v1/medication/responses",
            body={"elder_id": "E001", "interaction_id": interaction_id,
                  "action": "CONFIRM_TAKEN", "source": "web_test"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(response["occurrence"]["intake_status"], "confirmed_taken")
        status, payload = app.handle(
            "GET", "/api/v1/medication/notifications", query={"elder_id": "E001"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"], [])

    def test_dashboard_endpoint_returns_current_elder_read_models(self):
        draft = self._draft()
        self._approve(draft)
        app = Application(self.service)
        status, payload = app.handle(
            "GET", "/api/v1/medication/dashboard",
            query={"elder_id": "E001", "limit": "30"},
        )
        self.assertEqual(status, 200)
        self.assertIn("today", payload)
        self.assertIn("plans", payload)
        self.assertIn("events", payload)
        self.assertIn("notifications", payload)
        self.assertEqual(payload["plans"]["items"][0]["elder_id"], "E001")

    def test_confirm_is_idempotent_and_only_closes_bound_occurrence(self):
        active = self._approve(self._draft())
        occurrence = self._first_occurrence(active["plan_id"])
        scheduled = datetime.fromisoformat(occurrence["scheduled_at"])
        self.service.run_scheduler_cycle(scheduled + timedelta(minutes=1))
        current = self.service.get_occurrence(occurrence["occurrence_id"])
        interaction = current["interactions"][0]
        response = self.service.process_user_response({
            "event_id": "response-001",
            "elder_id": "E001",
            "interaction_id": interaction["interaction_id"],
            "text": "我吃了",
        })
        self.assertEqual(response["occurrence"]["intake_status"], "confirmed_taken")
        duplicate = self.service.process_user_response({
            "event_id": "response-001",
            "elder_id": "E001",
            "interaction_id": interaction["interaction_id"],
            "action": "CONFIRM_TAKEN",
        })
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(len(self.service.list_event_log("medication.intake.updated")), 1)

    def test_delay_preserves_scheduled_at_and_creates_next_reminder(self):
        active = self._approve(self._draft())
        occurrence = self._first_occurrence(active["plan_id"])
        scheduled = datetime.fromisoformat(occurrence["scheduled_at"])
        first_clock = scheduled + timedelta(minutes=1)
        self.service.run_scheduler_cycle(first_clock)
        current = self.service.get_occurrence(occurrence["occurrence_id"])
        interaction = current["interactions"][0]
        delayed = self.service.process_user_response({
            "elder_id": "E001",
            "interaction_id": interaction["interaction_id"],
            "action": "DELAY",
            "delay_minutes": 30,
        })
        self.assertEqual(delayed["occurrence"]["scheduled_at"], occurrence["scheduled_at"])
        self.assertEqual(delayed["occurrence"]["snooze_count"], 1)
        next_reminder = datetime.fromisoformat(delayed["occurrence"]["next_reminder_at"])
        self.assertGreaterEqual(next_reminder, datetime.now(timezone.utc) + timedelta(minutes=29))
        self.assertLessEqual(next_reminder, datetime.now(timezone.utc) + timedelta(minutes=31))

        self.service.run_scheduler_cycle(next_reminder)
        after = self.service.get_occurrence(occurrence["occurrence_id"])
        self.assertEqual(len(after["interactions"]), 2)

    def test_deadline_closes_unconfirmed_and_pause_cancels_future_tasks(self):
        active = self._approve(self._draft())
        occurrence = self._first_occurrence(active["plan_id"])
        deadline = datetime.fromisoformat(occurrence["confirmation_deadline_at"])
        result = self.service.run_scheduler_cycle(deadline + timedelta(seconds=1))
        self.assertEqual(result["closed_unconfirmed"], [occurrence["occurrence_id"]])
        closed = self.service.get_occurrence(occurrence["occurrence_id"])
        self.assertEqual(closed["intake_status"], "closed_unconfirmed")

        self.service.pause_plan(active["plan_id"], now=deadline)
        future = [item for item in self.service.get_today("E001")
                  if item["scheduled_at"] > occurrence["scheduled_at"]]
        self.assertTrue(all(item["intake_status"] == "cancelled" for item in future))

    def test_plan_revision_keeps_old_snapshot_and_invalidates_future_version(self):
        active = self._approve(self._draft())
        revised = self.service.revise_plan(active["plan_id"], {
            "drug_name": "缬沙坦",
            "dosage_text": "80mg",
        })
        self.assertEqual(revised["version"], 2)
        self.assertEqual(revised["status"], "draft")
        self.service.submit_plan(active["plan_id"], 2)
        self.service.approve_plan(active["plan_id"], "doctor:D001", 2, self.local_now.astimezone(timezone.utc))
        versions = self.service.list_plans_by_id(active["plan_id"])
        self.assertEqual(versions[0]["drug_name"], "氨氯地平")
        self.assertEqual(versions[1]["drug_name"], "缬沙坦")
        self.assertEqual(versions[0]["status"], "completed")
        old = self.service.storage.fetchall(
            "SELECT * FROM medication_occurrence WHERE plan_id=? AND plan_version=1",
            (active["plan_id"],),
        )
        self.assertTrue(old)
        self.assertTrue(all(row["intake_status"] == "cancelled" for row in old
                            if row["scheduled_at"] > iso(self.local_now.astimezone(timezone.utc))))

    def test_two_scheduler_threads_do_not_duplicate_claim(self):
        active = self._approve(self._draft())
        occurrence = self._first_occurrence(active["plan_id"])
        scheduled = datetime.fromisoformat(occurrence["scheduled_at"])
        results = []

        def run():
            results.append(self.service.run_scheduler_cycle(scheduled + timedelta(minutes=1), publish=False))

        first = threading.Thread(target=run)
        second = threading.Thread(target=run)
        first.start()
        second.start()
        first.join()
        second.join()
        claimed = sum(len(item["reminders_claimed"]) for item in results)
        self.assertEqual(claimed, 1)
        self.assertEqual(len(self.service.list_event_log("medication.reminder_due")), 1)

    def test_restart_recovers_pending_outbox(self):
        active = self._approve(self._draft())
        occurrence = self._first_occurrence(active["plan_id"])
        scheduled = datetime.fromisoformat(occurrence["scheduled_at"])
        self.service.run_scheduler_cycle(scheduled + timedelta(minutes=1), publish=False)
        self.service.close()
        self.service = MedicationService(self.db_path)
        self.service.publish_outbox()
        self.assertEqual(len(self.service.list_event_log("device.interaction.request")), 1)
        current = self.service.get_occurrence(occurrence["occurrence_id"])
        self.assertEqual(current["reminder_attempts"][0]["delivery_status"], "dispatched")


if __name__ == "__main__":
    unittest.main()

