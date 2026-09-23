import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from medication_reminder.service import MedicationService, SHANGHAI


class Phase51ClosureTests(unittest.TestCase):
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
                "escalation_caregiver_timeout_minutes": 30,
                "escalation_family_timeout_minutes": 60,
            },
        )

    def tearDown(self):
        self.service.close()
        self.temp_dir.cleanup()

    def _prepare_plan(self, confirmation_window=30):
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
            draft["plan_id"],
            "doctor:D001",
            draft["version"],
            self.local_now.astimezone(timezone.utc),
        )
        occurrence = next(
            item for item in self.service.get_today("E001")
            if item["plan_id"] == draft["plan_id"]
        )
        return draft, occurrence

    def _open(self, occurrence):
        scheduled = datetime.fromisoformat(occurrence["scheduled_at"])
        self.service.run_scheduler_cycle(scheduled + timedelta(seconds=1), publish=False)
        current = self.service.get_occurrence(occurrence["occurrence_id"])
        return current, current["interactions"][-1]

    def _record(
        self,
        occurrence,
        event_id,
        evidence_type,
        source_type="DEVICE",
        value=None,
        observed_at=None,
    ):
        return self.service.record_evidence({
            "event_id": event_id,
            "elder_id": occurrence["elder_id"],
            "occurrence_id": occurrence["occurrence_id"],
            "source_type": source_type,
            "evidence_type": evidence_type,
            "observed_at": observed_at or occurrence["scheduled_at"],
            "value": value or {"simulated": True},
            "device_id": "test-device" if source_type in ("DEVICE", "SENSOR") else None,
        })

    def _create_conflict(self):
        _plan, occurrence = self._prepare_plan()
        _current, interaction = self._open(occurrence)
        self.service.process_user_response({
            "event_id": "phase51-voice-%s" % occurrence["occurrence_id"],
            "elder_id": "E001",
            "interaction_id": interaction["interaction_id"],
            "action": "CONFIRM_TAKEN",
        })
        result = self._record(
            occurrence,
            "phase51-no-weight-%s" % occurrence["occurrence_id"],
            "NO_WEIGHT_CHANGE",
            source_type="SENSOR",
        )
        return occurrence, result

    def test_evidence_conflict_opens_direct_manual_review_escalation(self):
        occurrence, result = self._create_conflict()

        self.assertEqual(result["assessment"]["result"], "CONFIRMED")
        self.assertTrue(result["assessment"]["conflict_detected"])
        self.assertTrue(result["assessment"]["review_required"])
        self.assertEqual(result["occurrence"]["intake_status"], "confirmed_taken")

        escalations = self.service.list_escalations("E001")
        self.assertEqual(len(escalations), 1)
        escalation = escalations[0]
        self.assertEqual(escalation["occurrence_id"], occurrence["occurrence_id"])
        self.assertEqual(escalation["current_level"], "MANUAL_REVIEW")
        self.assertTrue(escalation["needs_manual_review"])
        self.assertEqual(escalation["reason"], "EVIDENCE_CONFLICT")

        detail = self.service.get_escalation(escalation["escalation_id"])
        self.assertEqual(len(detail["steps"]), 1)
        self.assertEqual(detail["steps"][0]["level"], "MANUAL_REVIEW")
        self.assertEqual(
            len(self.service.list_event_log("manual_review.request")), 1
        )
        self.assertEqual(
            len(self.service.list_event_log("medication.escalation.opened")), 1
        )

        self.service.publish_outbox()
        published_detail = self.service.get_escalation(escalation["escalation_id"])
        self.assertEqual(published_detail["steps"][0]["status"], "simulated")
        manual_notifications = [
            item for item in self.service.list_outbox()
            if item["event_type"] == "manual_review.request"
        ]
        self.assertEqual(len(manual_notifications), 1)
        self.assertEqual(manual_notifications[0]["status"], "published")

    def test_excess_removal_opens_manual_review_without_changing_occurrence(self):
        _plan, occurrence = self._prepare_plan()
        result = self._record(
            occurrence,
            "phase51-excess-1",
            "EXCESS_REMOVAL_SUSPECTED",
            source_type="SENSOR",
            value={"planned_removal": 1, "observed_removal": 2},
        )

        self.assertEqual(result["assessment"]["result"], "REVIEW_REQUIRED")
        self.assertTrue(result["assessment"]["review_required"])
        self.assertEqual(result["occurrence"]["intake_status"], "unconfirmed")
        escalation = self.service.list_escalations("E001")[0]
        self.assertEqual(escalation["current_level"], "MANUAL_REVIEW")
        self.assertTrue(escalation["needs_manual_review"])
        self.assertEqual(escalation["reason"], "EXCESS_REMOVAL_SUSPECTED")

    def test_duplicate_evidence_event_does_not_duplicate_assessment_or_escalation(self):
        _plan, occurrence = self._prepare_plan()
        first = self._record(
            occurrence,
            "phase51-duplicate-1",
            "EXCESS_REMOVAL_SUSPECTED",
            source_type="SENSOR",
        )
        second = self._record(
            occurrence,
            "phase51-duplicate-1",
            "EXCESS_REMOVAL_SUSPECTED",
            source_type="SENSOR",
            value={"different": True},
        )

        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(self.service.list_evidence(occurrence["occurrence_id"])), 1)
        self.assertEqual(
            len(self.service.list_confirmation_assessments(occurrence["occurrence_id"])),
            1,
        )
        self.assertEqual(len(self.service.list_escalations("E001")), 1)

    def test_multiple_conflicts_for_one_occurrence_keep_one_escalation(self):
        occurrence, _first = self._create_conflict()
        second = self._record(
            occurrence,
            "phase51-no-weight-2",
            "NO_WEIGHT_CHANGE",
            source_type="SENSOR",
        )

        self.assertTrue(second["assessment"]["review_required"])
        self.assertEqual(len(self.service.list_escalations("E001")), 1)
        escalation = self.service.list_escalations("E001")[0]
        self.assertEqual(escalation["current_level"], "MANUAL_REVIEW")
        self.assertTrue(escalation["needs_manual_review"])
        self.assertEqual(
            len(self.service.get_escalation(escalation["escalation_id"])["steps"]), 1
        )

    def test_manual_review_escalation_can_be_acknowledged(self):
        _occurrence, _result = self._create_conflict()
        escalation = self.service.list_escalations("E001")[0]

        acknowledged = self.service.acknowledge_escalation(
            escalation["escalation_id"],
            {
                "actor_id": "caregiver-001",
                "actor_role": "caregiver",
                "event_id": "phase51-ack-1",
            },
        )

        self.assertEqual(acknowledged["escalation"]["status"], "ACKNOWLEDGED")
        self.assertEqual(
            acknowledged["escalation"]["acknowledged_by"], "caregiver-001"
        )

    def test_manual_review_resolve_keeps_confirmed_intake_history(self):
        occurrence, _result = self._create_conflict()
        escalation = self.service.list_escalations("E001")[0]
        self.service.acknowledge_escalation(
            escalation["escalation_id"],
            {
                "actor_id": "caregiver-001",
                "actor_role": "caregiver",
                "event_id": "phase51-ack-2",
            },
        )
        resolved = self.service.resolve_escalation(
            escalation["escalation_id"],
            {
                "actor_id": "doctor-001",
                "actor_role": "doctor",
                "resolution_code": "TAKEN_VERIFIED",
                "resolution_note": "人工复核完成",
                "event_id": "phase51-resolve-1",
            },
        )

        self.assertEqual(resolved["escalation"]["status"], "RESOLVED")
        self.assertEqual(resolved["escalation"]["resolved_by"], "doctor-001")
        self.assertEqual(resolved["escalation"]["resolution_code"], "TAKEN_VERIFIED")
        self.assertEqual(
            self.service.get_occurrence(occurrence["occurrence_id"])["intake_status"],
            "confirmed_taken",
        )
        self.assertEqual(
            len(self.service.list_event_log("medication.escalation.acknowledged")), 1
        )
        self.assertEqual(
            len(self.service.list_event_log("medication.escalation.resolved")), 1
        )

    def test_manual_review_bridge_failure_rolls_back_entire_m5_transaction(self):
        _plan, occurrence = self._prepare_plan()
        with patch.object(
            self.service,
            "_open_manual_review_escalation_in_transaction",
            side_effect=RuntimeError("injected escalation failure"),
        ):
            with self.assertRaises(RuntimeError):
                self._record(
                    occurrence,
                    "phase51-rollback-1",
                    "EXCESS_REMOVAL_SUSPECTED",
                    source_type="SENSOR",
                )

        self.assertEqual(self.service.list_evidence(occurrence["occurrence_id"]), [])
        self.assertEqual(
            self.service.list_confirmation_assessments(occurrence["occurrence_id"]), []
        )
        self.assertEqual(self.service.list_escalations("E001"), [])
        self.assertEqual(
            self.service.list_event_log("manual_review.request"), []
        )
        self.assertEqual(
            self.service.get_occurrence(occurrence["occurrence_id"])[
                "intake_status"
            ],
            "unconfirmed",
        )

    def test_existing_missed_dose_escalation_is_promoted_without_duplicate(self):
        _plan, occurrence = self._prepare_plan(confirmation_window=1)
        current, _interaction = self._open(occurrence)
        deadline = datetime.fromisoformat(current["confirmation_deadline_at"])
        self.service.run_scheduler_cycle(deadline + timedelta(seconds=1), publish=False)
        original = self.service.list_escalations("E001")[0]
        self.assertEqual(original["current_level"], "CAREGIVER")

        self._record(
            occurrence,
            "phase51-late-taken-1",
            "MANUAL_REPORTED_TAKEN",
            source_type="MANUAL_OPERATOR",
        )
        self._record(
            occurrence,
            "phase51-late-no-weight-1",
            "NO_WEIGHT_CHANGE",
            source_type="SENSOR",
        )

        escalations = self.service.list_escalations("E001")
        self.assertEqual(len(escalations), 1)
        current = escalations[0]
        self.assertEqual(current["escalation_id"], original["escalation_id"])
        self.assertEqual(current["current_level"], "MANUAL_REVIEW")
        self.assertTrue(current["needs_manual_review"])
        self.assertEqual(current["reason"], "EVIDENCE_CONFLICT")

    def test_get_plan_without_version_returns_latest_version(self):
        draft, _occurrence = self._prepare_plan()
        revised = self.service.revise_plan(
            draft["plan_id"], {"dosage_text": "10mg"}
        )

        self.assertEqual(revised["version"], 2)
        self.assertEqual(self.service.get_plan(draft["plan_id"])["version"], 2)
        self.assertEqual(self.service.get_plan(draft["plan_id"], 1)["version"], 1)


if __name__ == "__main__":
    unittest.main()
