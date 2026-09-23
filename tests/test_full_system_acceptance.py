"""Cross-phase acceptance tests for the Phase 1-5.1 software loop.

These tests intentionally use temporary SQLite databases and the local device
adapter.  They assert durable business state, event/outbox state, and the
cross-phase result rather than only checking return values.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from medication_reminder.schedule import ScheduleExpander
from medication_reminder.service import MedicationService, SHANGHAI


UTC = timezone.utc


class FullSystemAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "acceptance.db")
        self.clock = datetime.now(SHANGHAI).replace(second=0, microsecond=0)
        self.service = MedicationService(self.db_path)

    def tearDown(self):
        self.service.close()
        self.temp_dir.cleanup()

    def _draft(self, *, schedule_type="FIXED_TIME", schedule_config=None,
               start_date=None, confirmation_window=30, elder_id="E001",
               drug_name="氨氯地平"):
        if schedule_config is None:
            schedule_config = {
                "times": [(self.clock + timedelta(minutes=2)).strftime("%H:%M")]
            }
        return self.service.create_draft({
            "elder_id": elder_id,
            "drug_name": drug_name,
            "dosage_text": "5mg",
            "schedule_type": schedule_type,
            "schedule_config": schedule_config,
            "start_date": start_date or self.clock.date().isoformat(),
            "confirmation_window_minutes": confirmation_window,
            "relation_to_meal": "餐后",
            "created_by": "family:F001",
            "device_sn": "local-device-001",
        })

    def _activate(self, draft):
        self.service.submit_plan(draft["plan_id"], draft["version"])
        return self.service.approve_plan(
            draft["plan_id"],
            "doctor:D001",
            draft["version"],
            self.clock.astimezone(UTC),
        )

    def _occurrences(self, plan_id, version=None):
        if version is None:
            rows = self.service.storage.fetchall(
                "SELECT * FROM medication_occurrence WHERE plan_id=? ORDER BY scheduled_at",
                (plan_id,),
            )
        else:
            rows = self.service.storage.fetchall(
                """SELECT * FROM medication_occurrence
                   WHERE plan_id=? AND plan_version=? ORDER BY scheduled_at""",
                (plan_id, version),
            )
        return [self.service.get_occurrence(row["occurrence_id"]) for row in rows]

    def _first_occurrence(self, plan_id, version=1):
        items = self._occurrences(plan_id, version)
        self.assertTrue(items, "expected an occurrence for %s v%s" % (plan_id, version))
        return items[0]

    def _open_reminder(self, occurrence):
        scheduled = datetime.fromisoformat(occurrence["scheduled_at"])
        result = self.service.run_scheduler_cycle(
            scheduled + timedelta(seconds=1)
        )
        self.assertEqual(len(result["reminders_claimed"]), 1)
        current = self.service.get_occurrence(occurrence["occurrence_id"])
        self.assertTrue(current["interactions"])
        self.assertTrue(current["reminder_attempts"])
        return current, current["interactions"][-1]

    def _event_types(self, occurrence_id):
        return {
            item["event_type"]
            for item in self.service.list_event_log(occurrence_id=occurrence_id,
                                                     limit=1000)
        }

    def test_e2e_01_normal_medication_loop(self):
        draft = self._draft()
        active = self._activate(draft)
        occurrence = self._first_occurrence(active["plan_id"])
        current, interaction = self._open_reminder(occurrence)

        self.assertEqual(current["intake_status"], "unconfirmed")
        self.assertEqual(current["reminder_attempts"][0]["delivery_status"],
                         "dispatched")
        self.assertEqual(self.service.list_active_notifications("E001")[0][
            "interaction_id"], interaction["interaction_id"])

        response = self.service.process_user_response({
            "event_id": "e2e01-response",
            "elder_id": "E001",
            "interaction_id": interaction["interaction_id"],
            "action": "CONFIRM_TAKEN",
            "source": "chat_agent",
        })
        self.assertEqual(response["occurrence"]["intake_status"], "confirmed_taken")
        self.assertEqual(len(self.service.list_evidence(occurrence["occurrence_id"])), 1)
        self.assertEqual(len(self.service.list_confirmation_assessments(
            occurrence["occurrence_id"])), 1)
        self.assertEqual(self.service.list_active_notifications("E001"), [])
        event_types = self._event_types(occurrence["occurrence_id"])
        self.assertIn("medication.reminder_due", event_types)
        self.assertIn("device.interaction.request", event_types)
        self.assertIn("medication.evidence.recorded", event_types)
        self.assertIn("medication.confirmation.confirmed", event_types)
        dashboard = self.service.get_dashboard("E001")
        self.assertEqual(dashboard["today"]["items"][0]["intake_status"],
                         "confirmed_taken")
        self.assertTrue(dashboard["events"]["items"])
        self.assertTrue(self.service.list_outbox())

    def test_e2e_02_delay_then_second_reminder_then_taken(self):
        active = self._activate(self._draft())
        occurrence = self._first_occurrence(active["plan_id"])
        current, interaction = self._open_reminder(occurrence)
        original_scheduled_at = current["scheduled_at"]

        delayed = self.service.process_user_response({
            "event_id": "e2e02-delay",
            "elder_id": "E001",
            "interaction_id": interaction["interaction_id"],
            "action": "DELAY",
            "delay_minutes": 5,
        })
        self.assertEqual(delayed["occurrence"]["scheduled_at"], original_scheduled_at)
        self.assertEqual(delayed["occurrence"]["snooze_count"], 1)
        next_reminder = datetime.fromisoformat(delayed["occurrence"]["next_reminder_at"])
        second_cycle = self.service.run_scheduler_cycle(next_reminder)
        self.assertEqual(len(second_cycle["reminders_claimed"]), 1)
        after = self.service.get_occurrence(occurrence["occurrence_id"])
        self.assertEqual(after["scheduled_at"], original_scheduled_at)
        self.assertEqual(len(after["interactions"]), 2)
        self.assertEqual(len(after["reminder_attempts"]), 2)

        response = self.service.process_user_response({
            "event_id": "e2e02-taken",
            "elder_id": "E001",
            "interaction_id": after["interactions"][-1]["interaction_id"],
            "action": "CONFIRM_TAKEN",
        })
        self.assertEqual(response["occurrence"]["intake_status"], "confirmed_taken")
        self.assertEqual(len(self.service.list_event_log(
            "medication.reminder.delayed", occurrence["occurrence_id"])), 1)

    def test_e2e_03_deadline_caregiver_family_manual_review_ack_resolve(self):
        active = self._activate(self._draft(confirmation_window=1))
        occurrence, _interaction = self._open_reminder(
            self._first_occurrence(active["plan_id"])
        )
        deadline = datetime.fromisoformat(occurrence["confirmation_deadline_at"])
        closed = self.service.run_scheduler_cycle(
            deadline + timedelta(seconds=1), publish=False
        )
        self.assertEqual(closed["closed_unconfirmed"], [occurrence["occurrence_id"]])
        escalation = self.service.list_escalations("E001")[0]
        self.assertEqual(escalation["current_level"], "CAREGIVER")
        self.assertEqual(escalation["status"], "OPEN")

        family_at = datetime.fromisoformat(escalation["next_escalation_at"])
        self.service.run_escalation_cycle(family_at, publish=False)
        escalation = self.service.list_escalations("E001")[0]
        self.assertEqual(escalation["current_level"], "FAMILY")
        manual_at = datetime.fromisoformat(escalation["next_escalation_at"])
        self.service.run_escalation_cycle(manual_at, publish=False)
        escalation = self.service.list_escalations("E001")[0]
        self.assertEqual(escalation["current_level"], "MANUAL_REVIEW")
        self.assertTrue(escalation["needs_manual_review"])

        acknowledged = self.service.acknowledge_escalation(
            escalation["escalation_id"], {
                "event_id": "e2e03-ack",
                "actor_id": "caregiver-001",
                "actor_role": "caregiver",
            }
        )
        self.assertEqual(acknowledged["escalation"]["status"], "ACKNOWLEDGED")
        resolved = self.service.resolve_escalation(
            escalation["escalation_id"], {
                "event_id": "e2e03-resolve",
                "actor_id": "caregiver-001",
                "actor_role": "caregiver",
                "resolution_code": "NOT_TAKEN",
                "resolution_note": "未在窗口内确认",
            }
        )
        self.assertEqual(resolved["escalation"]["status"], "RESOLVED")
        self.assertEqual(self.service.get_occurrence(
            occurrence["occurrence_id"])["intake_status"], "closed_unconfirmed")
        detail = self.service.get_escalation(escalation["escalation_id"])
        self.assertEqual([step["level"] for step in detail["steps"]], [
            "CAREGIVER", "FAMILY", "MANUAL_REVIEW"
        ])
        event_types = self._event_types(occurrence["occurrence_id"])
        self.assertIn("medication.escalation.opened", event_types)
        self.assertIn("medication.escalation.escalated", event_types)
        self.assertIn("medication.escalation.acknowledged", event_types)
        self.assertIn("medication.escalation.resolved", event_types)

    def test_e2e_04_two_daily_fixed_occurrences_are_independent(self):
        target_date = (self.clock + timedelta(days=1)).date().isoformat()
        active = self._activate(self._draft(
            start_date=target_date,
            schedule_config={"times": ["08:00", "20:00"]},
        ))
        target_day = [
            item for item in self._occurrences(active["plan_id"])
            if item["scheduled_at"].startswith(target_date)
        ]
        self.assertEqual(len(target_day), 2)
        self.assertLess(target_day[0]["scheduled_at"], target_day[1]["scheduled_at"])
        self.assertNotEqual(target_day[0]["occurrence_id"], target_day[1]["occurrence_id"])
        self.assertEqual({item["schedule_snapshot"]["selected_time"]
                          for item in target_day}, {"08:00", "20:00"})

    def test_e2e_05_meal_schedule_recalculates_after_routine_change(self):
        target_date = (self.clock + timedelta(days=1)).date().isoformat()
        self.service.update_routine("E001", {
            "breakfast_time": "08:00",
            "timezone": "Asia/Shanghai",
        }, now=self.clock.astimezone(UTC))
        active = self._activate(self._draft(
            schedule_type="MEAL_RELATION",
            schedule_config={
                "meal": "BREAKFAST", "relation": "AFTER", "offset_minutes": 30,
            },
            start_date=target_date,
        ))
        old = [item for item in self._occurrences(active["plan_id"])
               if item["scheduled_at"].startswith(target_date)][0]
        self.assertTrue(old["scheduled_at"].endswith("00:30:00+00:00"))
        old_id = old["occurrence_id"]
        self.service.update_routine("E001", {
            "breakfast_time": "09:00",
            "timezone": "Asia/Shanghai",
        }, now=(self.clock + timedelta(minutes=1)).astimezone(UTC))
        all_items = self._occurrences(active["plan_id"])
        old_after = self.service.get_occurrence(old_id)
        self.assertEqual(old_after["schedule_snapshot"]["routine_version"], 1)
        self.assertEqual(old_after["intake_status"], "cancelled")
        new_items = [item for item in all_items
                     if item["scheduled_at"].startswith(target_date)
                     and item["occurrence_id"] != old_id
                     and item["intake_status"] == "unconfirmed"]
        self.assertTrue(new_items)
        self.assertTrue(any(item["scheduled_at"].endswith("01:30:00+00:00")
                            for item in new_items))
        self.assertTrue(all(item["schedule_snapshot"]["routine_version"] == 2
                            for item in new_items))

    def test_e2e_06_taken_plus_no_weight_change_enters_manual_review(self):
        active = self._activate(self._draft())
        occurrence, interaction = self._open_reminder(
            self._first_occurrence(active["plan_id"])
        )
        taken = self.service.process_user_response({
            "event_id": "e2e06-taken",
            "elder_id": "E001",
            "interaction_id": interaction["interaction_id"],
            "action": "CONFIRM_TAKEN",
        })
        self.assertEqual(taken["occurrence"]["intake_status"], "confirmed_taken")
        conflict = self.service.record_evidence({
            "event_id": "e2e06-no-weight",
            "elder_id": "E001",
            "occurrence_id": occurrence["occurrence_id"],
            "source_type": "SENSOR",
            "evidence_type": "NO_WEIGHT_CHANGE",
            "device_id": "scale-001",
            "observed_at": occurrence["scheduled_at"],
            "value": {"delta_grams": 0},
        })
        self.assertEqual(conflict["assessment"]["result"], "CONFIRMED")
        self.assertTrue(conflict["assessment"]["conflict_detected"])
        self.assertTrue(conflict["assessment"]["review_required"])
        self.assertEqual(len(self.service.list_escalations("E001")), 1)
        escalation = self.service.list_escalations("E001")[0]
        self.assertEqual(escalation["current_level"], "MANUAL_REVIEW")
        self.service.acknowledge_escalation(escalation["escalation_id"], {
            "event_id": "e2e06-ack", "actor_id": "caregiver-001",
            "actor_role": "caregiver",
        })
        self.service.resolve_escalation(escalation["escalation_id"], {
            "event_id": "e2e06-resolve", "actor_id": "doctor-001",
            "actor_role": "doctor", "resolution_code": "TAKEN_VERIFIED",
        })
        self.assertEqual(self.service.get_occurrence(
            occurrence["occurrence_id"])["intake_status"], "confirmed_taken")
        for table in (
            "medication_evidence", "medication_confirmation_assessment",
            "medication_escalation", "medication_escalation_step",
        ):
            self.assertTrue(self.service.storage.fetchone(
                "SELECT 1 FROM %s LIMIT 1" % table
            ), table)

    def test_e2e_07_restart_recovers_pending_outbox_and_open_escalation(self):
        active = self._activate(self._draft(confirmation_window=1))
        occurrence = self._first_occurrence(active["plan_id"])
        scheduled = datetime.fromisoformat(occurrence["scheduled_at"])
        self.service.run_scheduler_cycle(scheduled + timedelta(seconds=1), publish=False)
        deadline = datetime.fromisoformat(
            self.service.get_occurrence(occurrence["occurrence_id"])[
                "confirmation_deadline_at"
            ]
        )
        self.service.close()
        self.service = MedicationService(self.db_path)
        self.service.run_scheduler_cycle(deadline + timedelta(seconds=1), publish=False)
        self.assertEqual(len(self.service.list_escalations("E001")), 1)
        pending_before = self.service.list_outbox("pending", limit=1000)
        self.assertTrue(pending_before)
        self.service.publish_outbox()
        self.service.run_scheduler_cycle(deadline + timedelta(seconds=2), publish=False)
        self.assertEqual(len(self._occurrences(active["plan_id"])), 8)
        self.assertEqual(len(self.service.list_escalations("E001")), 1)
        event_ids = [item["event_id"] for item in self.service.list_event_log(limit=1000)]
        self.assertEqual(len(event_ids), len(set(event_ids)))
        self.assertFalse(self.service.list_outbox("pending", limit=1000))

    def test_e2e_08_plan_revision_cancels_old_future_and_uses_latest(self):
        active = self._activate(self._draft(drug_name="旧药"))
        old_items = self._occurrences(active["plan_id"], 1)
        self.assertTrue(old_items)
        revised = self.service.revise_plan(active["plan_id"], {
            "drug_name": "新药",
            "dosage_text": "10mg",
        })
        self.service.submit_plan(revised["plan_id"], revised["version"])
        self.service.approve_plan(
            revised["plan_id"], "doctor:D001", revised["version"],
            (self.clock + timedelta(minutes=1)).astimezone(UTC),
        )
        latest = self.service.get_plan(active["plan_id"])
        self.assertEqual(latest["version"], 2)
        self.assertEqual(latest["drug_name"], "新药")
        self.assertEqual(self.service.get_plan(active["plan_id"], 1)["drug_name"], "旧药")
        self.assertTrue(all(item["intake_status"] == "cancelled"
                            for item in self._occurrences(active["plan_id"], 1)
                            if item["scheduled_at"] > self.clock.astimezone(UTC).isoformat()))
        self.assertTrue(self._occurrences(active["plan_id"], 2))
        self.assertTrue(all(item["drug_name_snapshot"] == "新药"
                            for item in self._occurrences(active["plan_id"], 2)))

    def test_schedule_matrix_includes_routine_relation_and_prn_no_occurrence(self):
        expander = ScheduleExpander()
        routine = {
            "elder_id": "E001", "bedtime": "22:00",
            "breakfast_time": "00:15", "timezone": "Asia/Shanghai",
            "routine_version": 3,
        }
        bedtime = expander.expand(
            {"schedule_type": "ROUTINE_RELATION", "schedule_config": {
                "anchor": "BEDTIME", "relation": "BEFORE", "offset_minutes": 30,
            }},
            start="2026-09-22", end="2026-09-23", routine=routine,
        )
        self.assertEqual(bedtime[0].scheduled_at.strftime("%H:%M"), "21:30")
        breakfast = expander.expand(
            {"schedule_type": "MEAL_RELATION", "schedule_config": {
                "meal": "BREAKFAST", "relation": "BEFORE", "offset_minutes": 30,
            }},
            start="2026-09-22", end="2026-09-23", routine=routine,
        )
        self.assertEqual(breakfast[0].scheduled_at.strftime("%Y-%m-%d %H:%M"),
                         "2026-09-21 23:45")
        interval = expander.expand(
            {"schedule_type": "INTERVAL", "schedule_config": {
                "interval_hours": 8, "anchor_at": "2026-09-22T20:00:00+08:00",
            }},
            start="2026-09-22T20:00:00+08:00",
            end=datetime(2026, 9, 24, tzinfo=timezone(timedelta(hours=8))),
        )
        self.assertEqual([item.scheduled_at.strftime("%H:%M") for item in interval],
                         ["20:00", "04:00", "12:00", "20:00"])
        weekly = expander.expand(
            {"schedule_type": "WEEKLY", "schedule_config": {
                "weekdays": [2], "times": ["08:00"],
            }},
            start="2026-09-22", end="2026-09-30",
        )
        self.assertTrue(weekly)
        self.assertTrue(all(item.scheduled_at.isoweekday() == 2 for item in weekly))
        cycle = expander.expand(
            {"schedule_type": "CYCLE", "schedule_config": {
                "cycle_start_date": "2026-09-28", "days_on": 5,
                "days_off": 2, "times": ["08:00"],
            }},
            start="2026-09-28", end="2026-10-06",
        )
        self.assertEqual(len(cycle), 7)
        prn = expander.expand(
            {"schedule_type": "PRN", "schedule_config": {
                "condition_text": "疼痛时",
            }},
            start="2026-09-22", end="2026-09-30",
        )
        self.assertEqual(prn, [])

    def test_database_consistency_after_m5_m6_chain(self):
        active = self._activate(self._draft())
        occurrence, interaction = self._open_reminder(
            self._first_occurrence(active["plan_id"])
        )
        self.service.process_user_response({
            "event_id": "consistency-taken", "elder_id": "E001",
            "interaction_id": interaction["interaction_id"],
            "action": "CONFIRM_TAKEN",
        })
        self.service.record_evidence({
            "event_id": "consistency-no-weight", "elder_id": "E001",
            "occurrence_id": occurrence["occurrence_id"], "source_type": "SENSOR",
            "evidence_type": "NO_WEIGHT_CHANGE", "observed_at": occurrence["scheduled_at"],
            "value": {"delta_grams": 0}, "device_id": "scale-001",
        })
        queries = {
            "orphan occurrence": """SELECT o.occurrence_id FROM medication_occurrence o
                LEFT JOIN medication_plan p ON p.plan_id=o.plan_id AND p.version=o.plan_version
                WHERE p.plan_id IS NULL""",
            "orphan interaction": """SELECT i.interaction_id FROM medication_interaction i
                LEFT JOIN medication_occurrence o ON o.occurrence_id=i.occurrence_id
                WHERE o.occurrence_id IS NULL""",
            "orphan reminder": """SELECT r.attempt_id FROM reminder_attempt r
                LEFT JOIN medication_occurrence o ON o.occurrence_id=r.occurrence_id
                WHERE o.occurrence_id IS NULL""",
            "orphan evidence": """SELECT e.evidence_id FROM medication_evidence e
                LEFT JOIN medication_occurrence o ON o.occurrence_id=e.occurrence_id
                WHERE o.occurrence_id IS NULL""",
            "orphan assessment": """SELECT a.assessment_id FROM medication_confirmation_assessment a
                LEFT JOIN medication_occurrence o ON o.occurrence_id=a.occurrence_id
                WHERE o.occurrence_id IS NULL""",
            "orphan escalation step": """SELECT s.step_id FROM medication_escalation_step s
                LEFT JOIN medication_escalation e ON e.escalation_id=s.escalation_id
                WHERE e.escalation_id IS NULL""",
            "duplicate occurrence": """SELECT plan_id, plan_version, scheduled_at, COUNT(*) n
                FROM medication_occurrence GROUP BY plan_id, plan_version, scheduled_at HAVING n>1""",
            "duplicate escalation": """SELECT occurrence_id, COUNT(*) n FROM medication_escalation
                GROUP BY occurrence_id HAVING n>1""",
            "confirmed without strong evidence": """SELECT o.occurrence_id FROM medication_occurrence o
                WHERE o.intake_status='confirmed_taken' AND NOT EXISTS (
                  SELECT 1 FROM medication_evidence e WHERE e.occurrence_id=o.occurrence_id
                  AND e.invalid=0 AND e.out_of_window=0 AND e.evidence_type IN
                  ('SELF_REPORTED_TAKEN','BUTTON_CONFIRMED','MANUAL_REPORTED_TAKEN'))""",
            "published outbox without event": """SELECT b.event_id FROM domain_outbox b
                LEFT JOIN medication_event_log e ON e.event_id=b.event_id
                WHERE b.status='published' AND e.event_id IS NULL""",
            "impossible occurrence state": """SELECT occurrence_id FROM medication_occurrence
                WHERE (intake_status='confirmed_taken' AND actual_time IS NULL)
                   OR (intake_status='unconfirmed' AND actual_time IS NOT NULL)""",
        }
        for name, sql in queries.items():
            self.assertEqual(self.service.storage.fetchall(sql), [], name)


if __name__ == "__main__":
    unittest.main()
