import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from medication_reminder.http import Application
from medication_reminder.service import DomainError, MedicationService, SHANGHAI


class Phase2EscalationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "medication.db")
        self.service = MedicationService(
            self.db_path,
            config={
                "escalation_caregiver_timeout_minutes": 1,
                "escalation_family_timeout_minutes": 1,
                "escalation_ack_resolution_timeout_minutes": 1,
                "escalation_repeat_missed_count": 99,
                "escalation_repeat_missed_lookback_hours": 48,
            },
        )
        self.local_now = datetime.now(SHANGHAI).replace(second=0, microsecond=0)

    def tearDown(self):
        self.service.close()
        self.temp_dir.cleanup()

    def _prepare_plan(self, confirmation_window=1):
        draft = self.service.create_draft({
            "elder_id": "E001",
            "drug_name": "氨氯地平",
            "dosage_text": "5mg",
            "schedule_time": self.local_now.strftime("%H:%M"),
            "start_date": self.local_now.date().isoformat(),
            "confirmation_window_minutes": confirmation_window,
            "relation_to_meal": "餐后",
            "created_by": "family:F001",
            "device_sn": "device-001",
        })
        self.service.submit_plan(draft["plan_id"], draft["version"])
        self.service.approve_plan(
            draft["plan_id"], "doctor:D001", draft["version"],
            self.local_now.astimezone(timezone.utc),
        )
        return draft

    def _first_occurrence(self, plan_id):
        return next(
            item for item in self.service.get_today("E001")
            if item["plan_id"] == plan_id and item["plan_version"] == 1
        )

    def _close_without_response(self, plan_id):
        occurrence = self._first_occurrence(plan_id)
        scheduled = datetime.fromisoformat(occurrence["scheduled_at"])
        self.service.run_scheduler_cycle(scheduled + timedelta(seconds=1))
        deadline = datetime.fromisoformat(occurrence["confirmation_deadline_at"])
        result = self.service.run_scheduler_cycle(deadline + timedelta(seconds=1))
        self.assertIn(occurrence["occurrence_id"], result["closed_unconfirmed"])
        return occurrence, deadline, self.service.get_escalation(
            self.service.list_escalations("E001")[0]["escalation_id"]
        )

    def _outbox_events(self, event_type):
        return [item for item in self.service.list_outbox() if item["event_type"] == event_type]

    def test_deadline_opens_caregiver_escalation_in_same_flow(self):
        plan = self._prepare_plan()
        occurrence, _deadline, escalation = self._close_without_response(plan["plan_id"])

        self.assertEqual(occurrence["occurrence_id"], escalation["occurrence_id"])
        self.assertEqual(escalation["current_level"], "CAREGIVER")
        self.assertEqual(escalation["status"], "OPEN")
        self.assertEqual(len(self._outbox_events("caregiver.task.assign")), 1)
        self.assertEqual(len(self._outbox_events("medication.escalation.opened")), 1)
        self.assertEqual(len(escalation["steps"]), 1)

    def test_repeated_scheduler_does_not_duplicate_escalation_or_caregiver_step(self):
        plan = self._prepare_plan()
        _occurrence, deadline, escalation = self._close_without_response(plan["plan_id"])
        self.service.run_scheduler_cycle(deadline + timedelta(seconds=2))

        self.assertEqual(len(self.service.list_escalations("E001")), 1)
        self.assertEqual(len(self._outbox_events("caregiver.task.assign")), 1)
        self.assertEqual(len(self.service.get_escalation(escalation["escalation_id"])["steps"]), 1)

    def test_restart_keeps_open_escalation_and_deadline(self):
        plan = self._prepare_plan()
        occurrence = self._first_occurrence(plan["plan_id"])
        scheduled = datetime.fromisoformat(occurrence["scheduled_at"])
        self.service.run_scheduler_cycle(scheduled + timedelta(seconds=1), publish=False)
        deadline = datetime.fromisoformat(occurrence["confirmation_deadline_at"])
        self.service.run_scheduler_cycle(deadline + timedelta(seconds=1), publish=False)
        before = self.service.list_escalations("E001")[0]

        self.service.close()
        self.service = MedicationService(
            self.db_path,
            config={
                "escalation_caregiver_timeout_minutes": 1,
                "escalation_family_timeout_minutes": 1,
                "escalation_ack_resolution_timeout_minutes": 1,
                "escalation_repeat_missed_count": 99,
            },
        )
        after = self.service.get_escalation(before["escalation_id"])
        self.assertEqual(after["status"], "OPEN")
        self.assertEqual(after["next_escalation_at"], before["next_escalation_at"])

    def test_caregiver_timeout_promotes_once_to_family(self):
        plan = self._prepare_plan()
        _occurrence, _deadline, escalation = self._close_without_response(plan["plan_id"])
        due = datetime.fromisoformat(escalation["next_escalation_at"]) + timedelta(seconds=1)
        processed = self.service.process_due_escalations(due)
        self.assertEqual(processed[0]["level"], "FAMILY")
        current = self.service.get_escalation(escalation["escalation_id"])
        self.assertEqual(current["current_level"], "FAMILY")
        self.assertEqual(len(self._outbox_events("family_notify.request")), 1)

        self.service.process_due_escalations(due + timedelta(seconds=1))
        self.assertEqual(len(self._outbox_events("family_notify.request")), 1)

    def test_family_timeout_marks_manual_review_without_emergency_call(self):
        plan = self._prepare_plan()
        _occurrence, _deadline, escalation = self._close_without_response(plan["plan_id"])
        family_due = datetime.fromisoformat(escalation["next_escalation_at"]) + timedelta(seconds=1)
        self.service.process_due_escalations(family_due)
        current = self.service.get_escalation(escalation["escalation_id"])
        manual_due = datetime.fromisoformat(current["next_escalation_at"]) if current["next_escalation_at"] else family_due
        self.service.process_due_escalations(manual_due + timedelta(seconds=1))
        current = self.service.get_escalation(escalation["escalation_id"])

        self.assertEqual(current["current_level"], "MANUAL_REVIEW")
        self.assertTrue(current["needs_manual_review"])
        self.assertIsNone(current["next_escalation_at"])
        self.assertEqual(len(self._outbox_events("manual_review.request")), 1)
        self.assertFalse(self._outbox_events("emergency_alert.trigger"))

    def test_acknowledge_sets_resolution_deadline_and_is_idempotent(self):
        plan = self._prepare_plan()
        _occurrence, _deadline, escalation = self._close_without_response(plan["plan_id"])
        payload = {"actor_id": "caregiver-001", "actor_role": "caregiver", "event_id": "ack-1"}
        first = self.service.acknowledge_escalation(escalation["escalation_id"], payload)
        second = self.service.acknowledge_escalation(escalation["escalation_id"], payload)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["escalation"]["status"], "ACKNOWLEDGED")
        self.assertIsNone(first["escalation"]["next_escalation_at"])
        self.assertIsNotNone(first["escalation"]["resolution_deadline_at"])
        resolution_deadline = datetime.fromisoformat(first["escalation"]["resolution_deadline_at"])
        self.service.process_due_escalations(resolution_deadline + timedelta(seconds=1))
        current = self.service.get_escalation(escalation["escalation_id"])
        self.assertEqual(current["status"], "OPEN")
        self.assertEqual(current["current_level"], "FAMILY")
        self.assertEqual(current["acknowledged_by"], "caregiver-001")
        self.assertIsNone(current["resolution_deadline_at"])
        self.assertEqual(len(self._outbox_events("family_notify.request")), 1)
        self.assertEqual(len(self.service.list_event_log("medication.escalation.acknowledged")), 1)

    def test_resolve_requires_ack_and_does_not_change_occurrence(self):
        plan = self._prepare_plan()
        occurrence, _deadline, escalation = self._close_without_response(plan["plan_id"])
        with self.assertRaises(DomainError):
            self.service.resolve_escalation(escalation["escalation_id"], {
                "actor_id": "caregiver-001", "actor_role": "caregiver",
                "resolution_code": "TAKEN_VERIFIED", "event_id": "resolve-before-ack",
            })
        self.service.acknowledge_escalation(escalation["escalation_id"], {
            "actor_id": "caregiver-001", "actor_role": "caregiver", "event_id": "ack-2",
        })
        first = self.service.resolve_escalation(escalation["escalation_id"], {
            "actor_id": "caregiver-001", "actor_role": "caregiver",
            "resolution_code": "TAKEN_VERIFIED", "resolution_note": "现场记录",
            "event_id": "resolve-1",
        })
        second = self.service.resolve_escalation(escalation["escalation_id"], {
            "actor_id": "caregiver-001", "actor_role": "caregiver",
            "resolution_code": "TAKEN_VERIFIED", "event_id": "resolve-1",
        })
        self.assertEqual(first["escalation"]["status"], "RESOLVED")
        self.assertTrue(second["duplicate"])
        self.assertEqual(self.service.get_occurrence(occurrence["occurrence_id"])["intake_status"], "closed_unconfirmed")
        self.service.process_due_escalations(datetime.now(timezone.utc) + timedelta(days=2))
        self.assertEqual(self.service.get_escalation(escalation["escalation_id"])["status"], "RESOLVED")

    def test_single_skip_does_not_open_escalation(self):
        plan = self._prepare_plan(confirmation_window=120)
        occurrence = self._first_occurrence(plan["plan_id"])
        scheduled = datetime.fromisoformat(occurrence["scheduled_at"])
        self.service.run_scheduler_cycle(scheduled + timedelta(seconds=1))
        current = self.service.get_occurrence(occurrence["occurrence_id"])
        interaction = current["interactions"][-1]
        response = self.service.process_user_response({
            "elder_id": "E001", "interaction_id": interaction["interaction_id"],
            "action": "SKIP", "event_id": "skip-1",
        })
        self.assertEqual(response["occurrence"]["intake_status"], "skipped")
        self.assertEqual(self.service.list_escalations("E001"), [])

    def test_repeat_missed_threshold_starts_at_family(self):
        self.service.config["escalation_repeat_missed_count"] = 2
        plan = self._prepare_plan()
        first, first_deadline, _first_escalation = self._close_without_response(plan["plan_id"])
        tomorrow = (self.local_now.date() + timedelta(days=1)).isoformat()
        second = next(
            item for item in self.service.get_today("E001", tomorrow)
            if item["occurrence_id"] != first["occurrence_id"] and item["plan_id"] == plan["plan_id"]
        )
        second_deadline = datetime.fromisoformat(second["confirmation_deadline_at"])
        self.service.run_scheduler_cycle(second_deadline + timedelta(seconds=1))
        current = next(
            item for item in self.service.list_escalations("E001")
            if item["occurrence_id"] == second["occurrence_id"]
        )
        self.assertEqual(current["current_level"], "FAMILY")
        family_events = [
            item for item in self._outbox_events("family_notify.request")
            if item["event"]["payload"].get("escalation_id") == current["escalation_id"]
        ]
        self.assertEqual(len(family_events), 1)
        self.assertEqual(
            self.service.get_occurrence(first["occurrence_id"])["intake_status"],
            "closed_unconfirmed",
        )
        self.assertLess(first_deadline, second_deadline)

    def test_completed_delivery_still_closes_and_escalates_without_confirmation(self):
        plan = self._prepare_plan()
        occurrence = self._first_occurrence(plan["plan_id"])
        scheduled = datetime.fromisoformat(occurrence["scheduled_at"])
        self.service.run_scheduler_cycle(scheduled + timedelta(seconds=1))
        current = self.service.get_occurrence(occurrence["occurrence_id"])
        attempt = current["reminder_attempts"][-1]
        interaction = current["interactions"][-1]
        self.service.process_device_event({
            "event_id": "device-started-2", "event_type": "STARTED", "elder_id": "E001",
            "interaction_id": interaction["interaction_id"], "attempt_id": attempt["attempt_id"],
        })
        self.service.process_device_event({
            "event_id": "device-completed-2", "event_type": "COMPLETED", "elder_id": "E001",
            "interaction_id": interaction["interaction_id"], "attempt_id": attempt["attempt_id"],
        })
        self.assertEqual(self.service.get_occurrence(occurrence["occurrence_id"])["intake_status"], "unconfirmed")
        deadline = datetime.fromisoformat(occurrence["confirmation_deadline_at"])
        self.service.run_scheduler_cycle(deadline + timedelta(seconds=1))
        self.assertEqual(self.service.get_occurrence(occurrence["occurrence_id"])["intake_status"], "closed_unconfirmed")
        self.assertEqual(len(self.service.list_escalations("E001")), 1)

    def test_delivery_failure_does_not_change_intake_fact(self):
        plan = self._prepare_plan(confirmation_window=120)
        occurrence = self._first_occurrence(plan["plan_id"])
        scheduled = datetime.fromisoformat(occurrence["scheduled_at"])
        self.service.run_scheduler_cycle(scheduled + timedelta(seconds=1))
        current = self.service.get_occurrence(occurrence["occurrence_id"])
        attempt = current["reminder_attempts"][-1]
        interaction = current["interactions"][-1]
        self.service.process_device_event({
            "event_id": "device-failed-2", "event_type": "FAILED", "elder_id": "E001",
            "interaction_id": interaction["interaction_id"], "attempt_id": attempt["attempt_id"],
            "reason": "test delivery failure",
        })
        current = self.service.get_occurrence(occurrence["occurrence_id"])
        self.assertEqual(current["intake_status"], "unconfirmed")
        self.assertEqual(current["reminder_attempts"][-1]["delivery_status"], "failed")
        self.assertFalse(self.service.list_escalations("E001"))

    def test_manual_confirmation_is_separate_from_resolve(self):
        plan = self._prepare_plan()
        occurrence, _deadline, escalation = self._close_without_response(plan["plan_id"])
        confirmed = self.service.record_manual_confirmation(occurrence["occurrence_id"], {
            "actor_id": "caregiver-001", "actor_role": "caregiver",
            "late_verified_source": "现场观察", "late_verified_note": "确认已服药",
            "event_id": "manual-confirm-1",
        })
        self.assertEqual(confirmed["occurrence"]["intake_status"], "closed_unconfirmed")
        self.assertTrue(confirmed["occurrence"]["late_verified_taken"])
        self.assertEqual(confirmed["occurrence"]["late_verified_by"], "caregiver-001")
        self.assertEqual(confirmed["occurrence"]["late_verified_source"], "现场观察")
        duplicate = self.service.record_manual_confirmation(occurrence["occurrence_id"], {
            "actor_id": "caregiver-001", "actor_role": "caregiver", "event_id": "manual-confirm-1",
        })
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(len(self.service.list_event_log("medication.intake.late_verified")), 1)
        self.service.acknowledge_escalation(escalation["escalation_id"], {
            "actor_id": "caregiver-001", "actor_role": "caregiver", "event_id": "ack-3",
        })
        resolved = self.service.resolve_escalation(escalation["escalation_id"], {
            "actor_id": "caregiver-001", "actor_role": "caregiver",
            "resolution_code": "TAKEN_VERIFIED", "event_id": "resolve-3",
        })
        self.assertEqual(resolved["escalation"]["status"], "RESOLVED")
        self.assertEqual(resolved["escalation"]["resolution_code"], "TAKEN_VERIFIED")
        self.assertEqual(
            self.service.get_occurrence(occurrence["occurrence_id"])["intake_status"],
            "closed_unconfirmed",
        )

    def test_acknowledge_before_resolution_deadline_resolves_without_promotion(self):
        plan = self._prepare_plan()
        _occurrence, _deadline, escalation = self._close_without_response(plan["plan_id"])
        acknowledged = self.service.acknowledge_escalation(escalation["escalation_id"], {
            "actor_id": "caregiver-001", "actor_role": "caregiver", "event_id": "ack-before-resolve",
        })
        resolution_deadline = datetime.fromisoformat(
            acknowledged["escalation"]["resolution_deadline_at"]
        )
        resolved = self.service.resolve_escalation(escalation["escalation_id"], {
            "actor_id": "caregiver-001", "actor_role": "caregiver",
            "resolution_code": "TAKEN_VERIFIED", "event_id": "resolve-before-deadline",
        })
        self.assertEqual(resolved["escalation"]["status"], "RESOLVED")
        self.assertIsNone(resolved["escalation"]["resolution_deadline_at"])
        self.service.process_due_escalations(resolution_deadline + timedelta(seconds=1))
        self.assertEqual(
            self.service.get_escalation(escalation["escalation_id"])["status"],
            "RESOLVED",
        )

    def test_family_ack_timeout_promotes_to_manual_review(self):
        plan = self._prepare_plan()
        _occurrence, _deadline, escalation = self._close_without_response(plan["plan_id"])
        caregiver_due = datetime.fromisoformat(escalation["next_escalation_at"]) + timedelta(seconds=1)
        self.service.process_due_escalations(caregiver_due)
        family = self.service.get_escalation(escalation["escalation_id"])
        self.assertEqual(family["current_level"], "FAMILY")
        acknowledged = self.service.acknowledge_escalation(escalation["escalation_id"], {
            "actor_id": "family-001", "actor_role": "family", "event_id": "family-ack-1",
        })
        self.assertEqual(acknowledged["escalation"]["status"], "ACKNOWLEDGED")
        self.assertIsNotNone(acknowledged["escalation"]["resolution_deadline_at"])
        family_resolution_deadline = datetime.fromisoformat(
            acknowledged["escalation"]["resolution_deadline_at"]
        )
        self.service.process_due_escalations(family_resolution_deadline + timedelta(seconds=1))
        current = self.service.get_escalation(escalation["escalation_id"])
        self.assertEqual(current["status"], "OPEN")
        self.assertEqual(current["current_level"], "MANUAL_REVIEW")
        self.assertTrue(current["needs_manual_review"])
        self.assertEqual(current["acknowledged_by"], "family-001")
        self.assertEqual(len(self._outbox_events("manual_review.request")), 1)
        self.assertEqual(len(current["steps"]), 3)

    def test_local_escalation_publisher_marks_simulated_not_delivered(self):
        plan = self._prepare_plan()
        _occurrence, _deadline, escalation = self._close_without_response(plan["plan_id"])
        step = escalation["steps"][0]
        self.assertEqual(step["status"], "simulated")
        self.assertNotEqual(step["status"], "delivered")
        notification = self._outbox_events("caregiver.task.assign")[0]
        self.assertEqual(notification["status"], "published")

    def test_normal_window_manual_confirmation_stays_confirmed_taken(self):
        plan = self._prepare_plan(confirmation_window=120)
        occurrence = self._first_occurrence(plan["plan_id"])
        confirmed = self.service.record_manual_confirmation(occurrence["occurrence_id"], {
            "actor_id": "caregiver-001", "actor_role": "caregiver", "event_id": "on-time-confirm-1",
        })
        self.assertEqual(confirmed["occurrence"]["intake_status"], "confirmed_taken")
        self.assertFalse(confirmed["occurrence"]["late_verified_taken"])
        self.assertEqual(len(self.service.list_event_log("medication.intake.updated")), 1)
        with self.assertRaises(DomainError):
            self.service.record_manual_confirmation(occurrence["occurrence_id"], {
                "actor_id": "caregiver-001", "actor_role": "caregiver", "event_id": "on-time-confirm-2",
            })

    def test_escalation_promotion_rolls_back_if_outbox_insert_fails(self):
        plan = self._prepare_plan()
        _occurrence, _deadline, escalation = self._close_without_response(plan["plan_id"])
        due = datetime.fromisoformat(escalation["next_escalation_at"]) + timedelta(seconds=1)
        original_enqueue = self.service._enqueue_outbox

        def fail_enqueue(_connection, _event, dedup_key=None):
            raise RuntimeError("injected outbox failure")

        self.service._enqueue_outbox = fail_enqueue
        try:
            with self.assertRaises(RuntimeError):
                self.service.process_due_escalations(due)
        finally:
            self.service._enqueue_outbox = original_enqueue
        current = self.service.get_escalation(escalation["escalation_id"])
        self.assertEqual(current["current_level"], "CAREGIVER")
        self.assertEqual(current["status"], "OPEN")
        self.assertEqual(len(current["steps"]), 1)
        self.assertFalse(self._outbox_events("family_notify.request"))
        self.assertFalse(self.service.list_event_log("medication.escalation.escalated"))

    def test_acknowledge_rolls_back_if_event_log_write_fails(self):
        plan = self._prepare_plan()
        _occurrence, _deadline, escalation = self._close_without_response(plan["plan_id"])
        original_insert = self.service._insert_event_log

        def fail_insert(_connection, _event, processed_at=None):
            raise RuntimeError("injected event log failure")

        self.service._insert_event_log = fail_insert
        try:
            with self.assertRaises(RuntimeError):
                self.service.acknowledge_escalation(escalation["escalation_id"], {
                    "actor_id": "caregiver-001", "actor_role": "caregiver", "event_id": "ack-rollback",
                })
        finally:
            self.service._insert_event_log = original_insert
        current = self.service.get_escalation(escalation["escalation_id"])
        self.assertEqual(current["status"], "OPEN")
        self.assertIsNone(current["resolution_deadline_at"])
        self.assertFalse(self.service.list_event_log("medication.escalation.acknowledged"))
        self.assertFalse(self._outbox_events("medication.escalation.acknowledged"))

    def test_resolve_rolls_back_if_outbox_insert_fails(self):
        plan = self._prepare_plan()
        _occurrence, _deadline, escalation = self._close_without_response(plan["plan_id"])
        self.service.acknowledge_escalation(escalation["escalation_id"], {
            "actor_id": "caregiver-001", "actor_role": "caregiver", "event_id": "ack-resolve-rollback",
        })
        original_enqueue = self.service._enqueue_outbox

        def fail_enqueue(_connection, _event, dedup_key=None):
            raise RuntimeError("injected outbox failure")

        self.service._enqueue_outbox = fail_enqueue
        try:
            with self.assertRaises(RuntimeError):
                self.service.resolve_escalation(escalation["escalation_id"], {
                    "actor_id": "caregiver-001", "actor_role": "caregiver",
                    "resolution_code": "TAKEN_VERIFIED", "event_id": "resolve-rollback",
                })
        finally:
            self.service._enqueue_outbox = original_enqueue
        current = self.service.get_escalation(escalation["escalation_id"])
        self.assertEqual(current["status"], "ACKNOWLEDGED")
        self.assertIsNotNone(current["resolution_deadline_at"])
        self.assertFalse(self.service.list_event_log("medication.escalation.resolved"))
        self.assertFalse(self._outbox_events("medication.escalation.resolved"))

    def test_full_ack_timeout_ack_resolve_history_is_retained(self):
        plan = self._prepare_plan()
        _occurrence, _deadline, escalation = self._close_without_response(plan["plan_id"])
        self.service.acknowledge_escalation(escalation["escalation_id"], {
            "actor_id": "caregiver-001", "actor_role": "caregiver", "event_id": "ack-full-1",
        })
        caregiver_resolution_deadline = datetime.fromisoformat(
            self.service.get_escalation(escalation["escalation_id"])["resolution_deadline_at"]
        )
        self.service.process_due_escalations(caregiver_resolution_deadline + timedelta(seconds=1))
        family = self.service.acknowledge_escalation(escalation["escalation_id"], {
            "actor_id": "family-001", "actor_role": "family", "event_id": "ack-full-2",
        })
        self.assertEqual(family["escalation"]["current_level"], "FAMILY")
        resolved = self.service.resolve_escalation(escalation["escalation_id"], {
            "actor_id": "family-001", "actor_role": "family",
            "resolution_code": "NOT_TAKEN", "event_id": "resolve-full",
        })
        self.assertEqual(resolved["escalation"]["status"], "RESOLVED")
        self.assertEqual(resolved["escalation"]["acknowledged_by"], "family-001")
        self.assertEqual(
            [step["level"] for step in resolved["escalation"]["steps"]],
            ["CAREGIVER", "FAMILY"],
        )
        self.assertEqual(len(self.service.list_event_log("medication.escalation.acknowledged")), 2)
        self.assertEqual(len(self.service.list_event_log("medication.escalation.resolved")), 1)

    def test_plan_pause_keeps_historical_escalation(self):
        plan = self._prepare_plan()
        _occurrence, deadline, escalation = self._close_without_response(plan["plan_id"])
        self.service.pause_plan(plan["plan_id"], now=deadline)
        self.assertEqual(self.service.get_escalation(escalation["escalation_id"])["status"], "OPEN")

    def test_http_escalation_endpoints_and_summary(self):
        plan = self._prepare_plan()
        _occurrence, _deadline, escalation = self._close_without_response(plan["plan_id"])
        app = Application(self.service)
        status, listing = app.handle("GET", "/api/v1/medication/escalations", query={"elder_id": "E001"})
        self.assertEqual(status, 200)
        self.assertEqual(len(listing["items"]), 1)
        status, detail = app.handle(
            "GET", "/api/v1/medication/escalations/%s" % escalation["escalation_id"]
        )
        self.assertEqual(status, 200)
        self.assertEqual(detail["steps"][0]["target_role"], "caregiver")
        status, summary = app.handle("GET", "/api/v1/medication/escalations/summary", query={"elder_id": "E001"})
        self.assertEqual(status, 200)
        self.assertEqual(summary["open_escalations"], 1)
        status, late = app.handle(
            "POST", "/api/v1/medication/occurrences/%s/confirm" % _occurrence["occurrence_id"],
            body={
                "actor_id": "caregiver-001", "actor_role": "caregiver",
                "late_verified_source": "http", "event_id": "http-late-confirm",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(late["occurrence"]["intake_status"], "closed_unconfirmed")
        self.assertTrue(late["occurrence"]["late_verified_taken"])
        status, acknowledged = app.handle(
            "POST", "/api/v1/medication/escalations/%s/acknowledge" % escalation["escalation_id"],
            body={"actor_id": "caregiver-001", "actor_role": "caregiver", "event_id": "http-ack"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(acknowledged["escalation"]["status"], "ACKNOWLEDGED")
        status, resolved = app.handle(
            "POST", "/api/v1/medication/escalations/%s/resolve" % escalation["escalation_id"],
            body={
                "actor_id": "caregiver-001", "actor_role": "caregiver",
                "resolution_code": "NOT_TAKEN", "resolution_note": "拒绝服药",
                "event_id": "http-resolve",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(resolved["escalation"]["status"], "RESOLVED")


    def test_cancel_is_audited_and_terminal(self):
        plan = self._prepare_plan()
        _occurrence, _deadline, escalation = self._close_without_response(plan["plan_id"])
        payload = {
            "actor_id": "caregiver-001", "actor_role": "caregiver",
            "cancel_reason": "人工确认无需继续跟进", "event_id": "cancel-1",
        }
        first = self.service.cancel_escalation(escalation["escalation_id"], payload)
        second = self.service.cancel_escalation(escalation["escalation_id"], payload)
        self.assertEqual(first["escalation"]["status"], "CANCELLED")
        self.assertTrue(second["duplicate"])
        self.service.process_due_escalations(datetime.now(timezone.utc) + timedelta(days=2))
        self.assertEqual(self.service.get_escalation(escalation["escalation_id"])["status"], "CANCELLED")
        self.assertEqual(len(self.service.list_event_log("medication.escalation.cancelled")), 1)

    def test_resolved_escalation_cannot_be_acknowledged_again(self):
        plan = self._prepare_plan()
        _occurrence, _deadline, escalation = self._close_without_response(plan["plan_id"])
        self.service.acknowledge_escalation(escalation["escalation_id"], {
            "actor_id": "caregiver-001", "actor_role": "caregiver", "event_id": "ack-terminal",
        })
        self.service.resolve_escalation(escalation["escalation_id"], {
            "actor_id": "caregiver-001", "actor_role": "caregiver",
            "resolution_code": "NOT_TAKEN", "event_id": "resolve-terminal",
        })
        with self.assertRaises(DomainError):
            self.service.acknowledge_escalation(escalation["escalation_id"], {
                "actor_id": "caregiver-001", "actor_role": "caregiver", "event_id": "ack-after-resolve",
            })


if __name__ == "__main__":
    unittest.main()
