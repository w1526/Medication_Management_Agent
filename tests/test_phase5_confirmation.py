import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from medication_reminder.http import Application
from medication_reminder.semantic.agent import MedicationSemanticAgent
from medication_reminder.service import DomainError, MedicationService, SHANGHAI


class Phase5ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "medication.db")
        self.local_now = datetime.now(SHANGHAI).replace(second=0, microsecond=0)
        self.service = MedicationService(
            self.db_path,
            config={
                "evidence_pre_window_minutes": 120,
                "evidence_post_window_minutes": 120,
                "evidence_clock_skew_seconds": 300,
            },
        )

    def tearDown(self):
        self.service.close()
        self.temp_dir.cleanup()

    def _prepare_plan(self, schedule_time=None, confirmation_window=30, elder="E001"):
        schedule_time = schedule_time or self.local_now.strftime("%H:%M")
        draft = self.service.create_draft({
            "elder_id": elder,
            "drug_name": "氨氯地平",
            "dosage_text": "5mg",
            "schedule_time": schedule_time,
            "start_date": self.local_now.date().isoformat(),
            "confirmation_window_minutes": confirmation_window,
            "relation_to_meal": "餐后",
            "created_by": "family:F001",
            "device_sn": "device-001",
        })
        self.service.submit_plan(draft["plan_id"], draft["version"])
        self.service.approve_plan(
            draft["plan_id"],
            "doctor:D001",
            draft["version"],
            self.local_now.astimezone(timezone.utc),
        )
        occurrence = next(
            item for item in self.service.get_today(elder)
            if item["plan_id"] == draft["plan_id"]
        )
        return draft, occurrence

    def _open(self, occurrence):
        scheduled = datetime.fromisoformat(occurrence["scheduled_at"])
        self.service.run_scheduler_cycle(scheduled + timedelta(seconds=1), publish=False)
        current = self.service.get_occurrence(occurrence["occurrence_id"])
        return current, current["interactions"][-1]

    def _record(self, occurrence, event_id, evidence_type, source_type="DEVICE",
                interaction_id=None, observed_at=None, value=None, elder_id="E001"):
        return self.service.record_evidence({
            "event_id": event_id,
            "elder_id": elder_id,
            "occurrence_id": occurrence["occurrence_id"],
            "interaction_id": interaction_id,
            "source_type": source_type,
            "evidence_type": evidence_type,
            "observed_at": observed_at or occurrence["scheduled_at"],
            "value": value or {"simulated": True},
            "device_id": "test-device" if source_type in ("DEVICE", "SENSOR") else None,
        })

    def _close(self, occurrence):
        current, interaction = self._open(occurrence)
        deadline = datetime.fromisoformat(current["confirmation_deadline_at"])
        self.service.run_scheduler_cycle(deadline + timedelta(seconds=1), publish=False)
        return self.service.get_occurrence(occurrence["occurrence_id"]), interaction

    def test_voice_response_flows_through_m5_assessment(self):
        _plan, occurrence = self._prepare_plan()
        current, interaction = self._open(occurrence)
        response = self.service.process_user_response({
            "event_id": "voice-response-1",
            "elder_id": "E001",
            "interaction_id": interaction["interaction_id"],
            "action": "CONFIRM_TAKEN",
            "text": "我吃了",
            "source": "chat_agent",
        })
        self.assertEqual(response["occurrence"]["intake_status"], "confirmed_taken")
        self.assertEqual(response["assessment"]["result"], "CONFIRMED")
        self.assertEqual(response["assessment"]["basis"], "SELF_REPORT")
        self.assertEqual(response["evidence"]["source_type"], "USER_VOICE")
        self.assertEqual(response["evidence"]["evidence_type"], "SELF_REPORTED_TAKEN")
        self.assertEqual(
            len(self.service.list_event_log("medication.confirmation.confirmed")), 1
        )
        self.assertEqual(
            self.service.get_occurrence(current["occurrence_id"])["confirmation_method"],
            "voice",
        )

    def test_button_response_flows_through_m5_assessment(self):
        _plan, occurrence = self._prepare_plan()
        _current, interaction = self._open(occurrence)
        response = self.service.process_user_response({
            "event_id": "button-response-1",
            "elder_id": "E001",
            "interaction_id": interaction["interaction_id"],
            "action": "CONFIRM_TAKEN",
            "source": "web_test",
        })
        self.assertEqual(response["assessment"]["basis"], "BUTTON_SELF_REPORT")
        self.assertEqual(response["evidence"]["source_type"], "USER_BUTTON")
        self.assertEqual(response["occurrence"]["confirmation_method"], "button")

    def test_box_opened_and_weight_decrease_do_not_confirm_alone(self):
        _plan, occurrence = self._prepare_plan()
        first = self._record(occurrence, "box-opened-1", "BOX_OPENED")
        self.assertEqual(first["assessment"]["result"], "UNCONFIRMED")
        second = self._record(
            occurrence,
            "weight-decrease-1",
            "WEIGHT_DECREASE_OBSERVED",
            source_type="SENSOR",
            value={"raw_before": 10.0, "raw_after": 9.5, "delta_grams": -0.5},
        )
        self.assertEqual(second["assessment"]["result"], "UNCONFIRMED")
        self.assertEqual(second["occurrence"]["intake_status"], "unconfirmed")
        self.assertEqual(second["assessment"]["basis"], "NONE")

    def test_weight_raw_observation_is_preserved_without_inference(self):
        _plan, occurrence = self._prepare_plan()
        result = self._record(
            occurrence,
            "weight-raw-1",
            "WEIGHT_OBSERVATION",
            source_type="SENSOR",
            value={"raw_before": 10.0, "raw_after": 9.5, "delta": -0.5, "unit": "g"},
        )
        self.assertEqual(result["evidence"]["value"]["raw_before"], 10.0)
        self.assertEqual(result["evidence"]["value"]["raw_after"], 9.5)
        self.assertEqual(result["occurrence"]["intake_status"], "unconfirmed")

    def test_taken_plus_no_weight_change_is_conflict_and_review(self):
        _plan, occurrence = self._prepare_plan()
        _current, interaction = self._open(occurrence)
        self.service.process_user_response({
            "event_id": "voice-conflict-1",
            "elder_id": "E001",
            "interaction_id": interaction["interaction_id"],
            "action": "CONFIRM_TAKEN",
        })
        result = self._record(
            occurrence,
            "no-weight-after-voice-1",
            "NO_WEIGHT_CHANGE",
            source_type="SENSOR",
        )
        self.assertEqual(result["assessment"]["result"], "CONFIRMED")
        self.assertTrue(result["assessment"]["conflict_detected"])
        self.assertTrue(result["assessment"]["review_required"])
        self.assertEqual(result["occurrence"]["intake_status"], "confirmed_taken")
        self.assertEqual(
            len(self.service.list_event_log("medication.confirmation.conflict_detected")),
            1,
        )
        self.assertEqual(len(self.service.list_event_log("manual_review.request")), 1)

    def test_device_error_avoids_false_conflict(self):
        _plan, occurrence = self._prepare_plan()
        _current, interaction = self._open(occurrence)
        self.service.process_user_response({
            "event_id": "voice-device-error-1",
            "elder_id": "E001",
            "interaction_id": interaction["interaction_id"],
            "action": "CONFIRM_TAKEN",
        })
        self._record(occurrence, "no-weight-device-error-1", "NO_WEIGHT_CHANGE",
                     source_type="SENSOR")
        result = self._record(occurrence, "device-error-1", "DEVICE_ERROR",
                              source_type="DEVICE")
        self.assertEqual(result["assessment"]["result"], "CONFIRMED")
        self.assertFalse(result["assessment"]["conflict_detected"])
        self.assertFalse(result["assessment"]["review_required"])
        self.assertEqual(result["occurrence"]["intake_status"], "confirmed_taken")

    def test_excess_removal_requires_review_and_never_means_overdose(self):
        _plan, occurrence = self._prepare_plan()
        result = self._record(
            occurrence,
            "excess-removal-1",
            "EXCESS_REMOVAL_SUSPECTED",
            source_type="SENSOR",
            value={"planned_removal": 1, "observed_removal": 2},
        )
        self.assertEqual(result["assessment"]["result"], "REVIEW_REQUIRED")
        self.assertTrue(result["assessment"]["review_required"])
        self.assertEqual(result["occurrence"]["intake_status"], "unconfirmed")
        self.assertEqual(len(self.service.list_event_log("manual_review.request")), 1)
        with self.assertRaises(DomainError) as caught:
            self._record(occurrence, "overdose-1", "OVERDOSE_CONFIRMED")
        self.assertEqual(caught.exception.status, 422)
        self.assertEqual(len(self.service.list_evidence(occurrence["occurrence_id"])), 1)

    def test_duplicate_evidence_event_is_idempotent(self):
        _plan, occurrence = self._prepare_plan()
        first = self._record(occurrence, "evidence-duplicate-1", "BOX_OPENED")
        second = self._record(occurrence, "evidence-duplicate-1", "BOX_OPENED",
                              value={"different": True})
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(self.service.list_evidence(occurrence["occurrence_id"])), 1)
        self.assertEqual(
            len(self.service.list_confirmation_assessments(occurrence["occurrence_id"])),
            1,
        )

    def test_cross_occurrence_and_cross_elder_binding_are_rejected(self):
        _plan1, occurrence1 = self._prepare_plan()
        _plan2, occurrence2 = self._prepare_plan(
            schedule_time=(self.local_now + timedelta(minutes=1)).strftime("%H:%M")
        )
        _current1, interaction1 = self._open(occurrence1)
        with self.assertRaises(DomainError) as mismatch:
            self._record(
                occurrence2,
                "binding-occurrence-1",
                "BOX_OPENED",
                interaction_id=interaction1["interaction_id"],
            )
        self.assertEqual(mismatch.exception.status, 409)
        with self.assertRaises(DomainError) as elder_mismatch:
            self._record(
                occurrence1,
                "binding-elder-1",
                "BOX_OPENED",
                elder_id="E999",
            )
        self.assertEqual(elder_mismatch.exception.status, 409)
        self.assertEqual(self.service.list_evidence(occurrence1["occurrence_id"]), [])

    def test_voice_evidence_requires_trusted_interaction_binding(self):
        _plan, occurrence = self._prepare_plan()
        with self.assertRaises(DomainError) as caught:
            self._record(
                occurrence,
                "voice-without-interaction-1",
                "SELF_REPORTED_TAKEN",
                source_type="USER_VOICE",
            )
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.service.list_evidence(occurrence["occurrence_id"]), [])

    def test_future_clock_skew_is_rejected_and_old_evidence_is_out_of_window(self):
        _plan, occurrence = self._prepare_plan()
        future = datetime.now(timezone.utc) + timedelta(seconds=301)
        with self.assertRaises(DomainError) as caught:
            self._record(
                occurrence,
                "future-evidence-1",
                "BOX_OPENED",
                observed_at=future.isoformat(),
            )
        self.assertEqual(caught.exception.status, 422)
        old = datetime.fromisoformat(occurrence["scheduled_at"]) - timedelta(days=3)
        result = self._record(
            occurrence,
            "old-evidence-1",
            "BOX_OPENED",
            observed_at=old.isoformat(),
        )
        self.assertIsNone(result["assessment"])
        self.assertTrue(result["evidence"]["out_of_window"])
        self.assertEqual(result["occurrence"]["intake_status"], "unconfirmed")

    def test_invalid_source_and_evidence_type_are_rejected(self):
        _plan, occurrence = self._prepare_plan()
        with self.assertRaises(DomainError):
            self._record(occurrence, "bad-source-1", "BOX_OPENED", source_type="LLM")
        with self.assertRaises(DomainError):
            self._record(occurrence, "bad-type-1", "UNKNOWN_EVIDENCE", source_type="DEVICE")
        self.assertEqual(self.service.list_evidence(occurrence["occurrence_id"]), [])

    def test_manual_confirmation_is_an_m5_manual_evidence_report(self):
        _plan, occurrence = self._prepare_plan()
        with self.assertRaises(DomainError):
            self.service.record_manual_confirmation(occurrence["occurrence_id"], {})
        result = self.service.record_manual_confirmation(occurrence["occurrence_id"], {
            "actor_id": "operator-001",
            "actor_role": "caregiver",
            "event_id": "manual-m5-1",
            "late_verified_source": "现场观察",
            "late_verified_note": "确认已服药",
        })
        self.assertEqual(result["assessment"]["basis"], "MANUAL_REPORT")
        self.assertEqual(result["evidence"]["source_type"], "MANUAL_OPERATOR")
        self.assertEqual(result["evidence"]["actor_id"], "operator-001")
        self.assertEqual(result["occurrence"]["intake_status"], "confirmed_taken")
        self.assertEqual(len(self.service.list_event_log("medication.intake.updated")), 1)

    def test_late_manual_verification_preserves_closed_unconfirmed(self):
        _plan, occurrence = self._prepare_plan(confirmation_window=1)
        closed, _interaction = self._close(occurrence)
        self.assertEqual(closed["intake_status"], "closed_unconfirmed")
        result = self.service.record_manual_confirmation(occurrence["occurrence_id"], {
            "actor_id": "operator-late-1",
            "actor_role": "caregiver",
            "event_id": "late-manual-1",
            "late_verified_source": "现场观察",
            "late_verified_note": "超时后核实",
        })
        self.assertTrue(result["late"])
        self.assertEqual(result["occurrence"]["intake_status"], "closed_unconfirmed")
        self.assertTrue(result["occurrence"]["late_verified_taken"])
        self.assertEqual(result["occurrence"]["late_verified_by"], "operator-late-1")
        self.assertEqual(result["occurrence"]["late_verified_source"], "现场观察")
        self.assertTrue(result["assessment"]["late"])
        self.assertEqual(len(self.service.list_event_log("medication.intake.late_verified")), 1)

    def test_late_voice_verification_preserves_closed_unconfirmed(self):
        _plan, occurrence = self._prepare_plan(confirmation_window=1)
        closed, interaction = self._close(occurrence)
        self.assertEqual(closed["intake_status"], "closed_unconfirmed")
        agent = MedicationSemanticAgent(self.service)
        result = agent.handle(
            "E001",
            "我吃了",
            interaction_id=interaction["interaction_id"],
            event_id="late-voice-1",
        )
        self.assertEqual(result["kind"], "medication_response")
        self.assertEqual(result["occurrence"]["intake_status"], "closed_unconfirmed")
        current = self.service.get_occurrence(occurrence["occurrence_id"])
        self.assertTrue(current["late_verified_taken"])
        self.assertEqual(current["late_verified_source"], "USER_VOICE")
        self.assertEqual(len(self.service.list_event_log("medication.intake.late_verified")), 1)

    def test_skip_records_not_taken_evidence_without_confirmation_assessment(self):
        _plan, occurrence = self._prepare_plan()
        _current, interaction = self._open(occurrence)
        result = self.service.process_user_response({
            "event_id": "skip-evidence-1",
            "elder_id": "E001",
            "interaction_id": interaction["interaction_id"],
            "action": "SKIP",
        })
        self.assertEqual(result["occurrence"]["intake_status"], "skipped")
        self.assertEqual(result["evidence"]["evidence_type"], "SELF_REPORTED_NOT_TAKEN")
        self.assertEqual(self.service.list_confirmation_assessments(occurrence["occurrence_id"]), [])

    def test_delay_does_not_write_confirmation_evidence(self):
        _plan, occurrence = self._prepare_plan()
        _current, interaction = self._open(occurrence)
        result = self.service.process_user_response({
            "event_id": "delay-no-evidence-1",
            "elder_id": "E001",
            "interaction_id": interaction["interaction_id"],
            "action": "DELAY",
            "delay_minutes": 5,
        })
        self.assertEqual(result["action"], "DELAY")
        self.assertEqual(self.service.list_evidence(occurrence["occurrence_id"]), [])

    def test_repeat_does_not_write_confirmation_evidence(self):
        _plan, occurrence = self._prepare_plan()
        _current, interaction = self._open(occurrence)
        result = self.service.process_user_response({
            "event_id": "repeat-no-evidence-1",
            "elder_id": "E001",
            "interaction_id": interaction["interaction_id"],
            "action": "REPEAT",
        })
        self.assertEqual(result["action"], "REPEAT")
        self.assertEqual(self.service.list_evidence(occurrence["occurrence_id"]), [])

    def test_assessment_transaction_rolls_back_evidence(self):
        _plan, occurrence = self._prepare_plan()
        with patch.object(
            self.service,
            "_create_confirmation_assessment_in_transaction",
            side_effect=RuntimeError("assessment failure"),
        ):
            with self.assertRaises(RuntimeError):
                self._record(occurrence, "rollback-assessment-1", "BOX_OPENED")
        self.assertEqual(self.service.list_evidence(occurrence["occurrence_id"]), [])
        self.assertEqual(
            self.service.list_confirmation_assessments(occurrence["occurrence_id"]), []
        )
        self.assertEqual(self.service.get_occurrence(occurrence["occurrence_id"])["intake_status"],
                         "unconfirmed")

    def test_apply_transaction_rolls_back_evidence_and_assessment(self):
        _plan, occurrence = self._prepare_plan()
        _current, interaction = self._open(occurrence)
        with patch.object(
            self.service,
            "_apply_confirmation_assessment_in_transaction",
            side_effect=RuntimeError("apply failure"),
        ):
            with self.assertRaises(RuntimeError):
                self._record(
                    occurrence,
                    "rollback-apply-1",
                    "SELF_REPORTED_TAKEN",
                    source_type="USER_VOICE",
                    interaction_id=interaction["interaction_id"],
                )
        self.assertEqual(self.service.list_evidence(occurrence["occurrence_id"]), [])
        self.assertEqual(
            self.service.list_confirmation_assessments(occurrence["occurrence_id"]), []
        )
        self.assertEqual(self.service.get_occurrence(occurrence["occurrence_id"])["intake_status"],
                         "unconfirmed")

    def test_evidence_and_assessment_are_immutable(self):
        _plan, occurrence = self._prepare_plan()
        _current, interaction = self._open(occurrence)
        result = self._record(
            occurrence,
            "immutable-1",
            "SELF_REPORTED_TAKEN",
            source_type="USER_VOICE",
            interaction_id=interaction["interaction_id"],
        )
        evidence_id = result["evidence"]["evidence_id"]
        assessment_id = result["assessment"]["assessment_id"]
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.storage.execute(
                "UPDATE medication_evidence SET value_json=? WHERE evidence_id=?",
                ("{}", evidence_id),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.storage.execute(
                "DELETE FROM medication_confirmation_assessment WHERE assessment_id=?",
                (assessment_id,),
            )
        self.assertEqual(len(self.service.list_evidence(occurrence["occurrence_id"])), 1)
        self.assertEqual(
            len(self.service.list_confirmation_assessments(occurrence["occurrence_id"])), 1
        )

    def test_http_evidence_and_confirmation_routes(self):
        _plan, occurrence = self._prepare_plan()
        app = Application(self.service)
        status, created = app.handle(
            "POST",
            "/api/v1/medication/occurrences/%s/evidence" % occurrence["occurrence_id"],
            body={
                "event_id": "http-evidence-1",
                "elder_id": "E001",
                "source_type": "DEVICE",
                "evidence_type": "BOX_OPENED",
                "value": {"simulated": True},
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["assessment"]["result"], "UNCONFIRMED")
        status, listing = app.handle(
            "GET",
            "/api/v1/medication/occurrences/%s/evidence" % occurrence["occurrence_id"],
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(listing["items"]), 1)
        status, confirmation = app.handle(
            "GET",
            "/api/v1/medication/occurrences/%s/confirmation" % occurrence["occurrence_id"],
        )
        self.assertEqual(status, 200)
        self.assertEqual(confirmation["evidence_count"], 1)
        self.assertEqual(confirmation["policy_version"], "confirmation-policy-v1")

    def test_restart_preserves_evidence_assessment_and_policy_fingerprint(self):
        _plan, occurrence = self._prepare_plan()
        self._record(occurrence, "restart-evidence-1", "BOX_OPENED")
        before = self.service.get_confirmation(occurrence["occurrence_id"])
        self.service.close()
        self.service = MedicationService(self.db_path)
        after = self.service.get_confirmation(occurrence["occurrence_id"])
        self.assertEqual(after["evidence_count"], 1)
        self.assertEqual(len(after["assessment_history"]), 1)
        self.assertEqual(after["latest_assessment"]["policy_fingerprint"],
                         before["latest_assessment"]["policy_fingerprint"])
        self.assertEqual(after["policy_version"], "confirmation-policy-v1")

    def test_weak_evidence_reaches_deadline_and_opens_m6(self):
        _plan, occurrence = self._prepare_plan(confirmation_window=1)
        self._record(occurrence, "weak-deadline-box-1", "BOX_OPENED")
        self._record(
            occurrence,
            "weak-deadline-weight-1",
            "WEIGHT_DECREASE_OBSERVED",
            source_type="SENSOR",
            value={"raw_before": 10, "raw_after": 9.5, "delta_grams": -0.5},
        )
        closed, _interaction = self._close(occurrence)
        self.assertEqual(closed["intake_status"], "closed_unconfirmed")
        self.assertTrue(self.service.list_escalations("E001"))
        self.assertEqual(
            self.service.get_confirmation(occurrence["occurrence_id"])["latest_assessment"]["result"],
            "UNCONFIRMED",
        )

    def test_duplicate_voice_event_does_not_duplicate_evidence_or_assessment(self):
        _plan, occurrence = self._prepare_plan()
        _current, interaction = self._open(occurrence)
        payload = {
            "event_id": "duplicate-voice-m5-1",
            "elder_id": "E001",
            "interaction_id": interaction["interaction_id"],
            "action": "CONFIRM_TAKEN",
        }
        first = self.service.process_user_response(payload)
        second = self.service.process_user_response(payload)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(self.service.list_evidence(occurrence["occurrence_id"])), 1)
        self.assertEqual(
            len(self.service.list_confirmation_assessments(occurrence["occurrence_id"])), 1
        )
        self.assertEqual(len(self.service.list_event_log("medication.intake.updated")), 1)

    def test_full_reminder_voice_then_sensor_review_keeps_confirmed_state(self):
        _plan, occurrence = self._prepare_plan()
        current, interaction = self._open(occurrence)
        attempt = current["reminder_attempts"][-1]
        self.service.process_device_event({
            "event_id": "tts-started-m5-1",
            "event_type": "STARTED",
            "elder_id": "E001",
            "interaction_id": interaction["interaction_id"],
            "attempt_id": attempt["attempt_id"],
        })
        self.service.process_device_event({
            "event_id": "tts-completed-m5-1",
            "event_type": "COMPLETED",
            "elder_id": "E001",
            "interaction_id": interaction["interaction_id"],
            "attempt_id": attempt["attempt_id"],
        })
        voice = self.service.process_user_response({
            "event_id": "integration-voice-m5-1",
            "elder_id": "E001",
            "interaction_id": interaction["interaction_id"],
            "text": "我已经吃了",
        })
        self.assertEqual(voice["occurrence"]["intake_status"], "confirmed_taken")
        sensor = self._record(
            occurrence,
            "integration-no-weight-m5-1",
            "NO_WEIGHT_CHANGE",
            source_type="SENSOR",
        )
        self.assertEqual(sensor["occurrence"]["intake_status"], "confirmed_taken")
        self.assertTrue(sensor["assessment"]["conflict_detected"])
        self.assertTrue(sensor["assessment"]["review_required"])
        self.assertTrue(self.service.list_event_log("manual_review.request"))



if __name__ == "__main__":
    unittest.main()
