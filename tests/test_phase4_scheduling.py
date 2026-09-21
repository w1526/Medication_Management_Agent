import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from medication_reminder.http import Application
from medication_reminder.routine import ElderRoutine
from medication_reminder.schedule import (
    ScheduleError,
    ScheduleExpander,
    ScheduleType,
    ScheduleValidator,
)
from medication_reminder.service import DomainError, MedicationService, SHANGHAI


class Phase4SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "medication.db")
        self.service = MedicationService(self.db_path)
        self.local_clock = datetime(2026, 9, 20, 0, 0, tzinfo=SHANGHAI)
        self.clock = self.local_clock.astimezone(timezone.utc)

    def tearDown(self):
        self.service.close()
        self.temp_dir.cleanup()

    def draft(self, **extra):
        data = {
            "elder_id": "E001",
            "drug_name": "TEST_DRUG_A",
            "dosage_text": "5mg",
            "schedule_time": "08:00",
            "start_date": "2026-09-20",
            "created_by": "family:F001",
        }
        data.update(extra)
        return self.service.create_draft(data)

    def approve(self, plan, now=None):
        return self.service.approve_plan(
            plan["plan_id"], "doctor:D001", plan["version"], now or self.clock
        )

    def occurrences(self, plan_id=None):
        if plan_id:
            return self.service.storage.fetchall(
                "SELECT * FROM medication_occurrence WHERE plan_id=? ORDER BY scheduled_at",
                (plan_id,),
            )
        return self.service.storage.fetchall(
            "SELECT * FROM medication_occurrence ORDER BY scheduled_at"
        )

    def test_legacy_schedule_time_normalizes_to_fixed_time(self):
        plan = self.draft()
        self.assertEqual(plan["schedule_type"], ScheduleType.FIXED_TIME)
        self.assertEqual(plan["schedule_config"], {"times": ["08:00"]})
        self.assertEqual(plan["schedule_time"], "08:00")

    def test_fixed_time_multiple_times_are_sorted_and_idempotent(self):
        plan = self.draft(
            schedule_type="FIXED_TIME",
            schedule_config={"times": ["20:00", "08:00"]},
        )
        self.approve(plan)
        self.assertEqual(
            [row["scheduled_at"] for row in self.occurrences(plan["plan_id"])[:2]],
            ["2026-09-20T00:00:00+00:00", "2026-09-20T12:00:00+00:00"],
        )
        count = len(self.occurrences(plan["plan_id"]))
        self.service.ensure_occurrences(self.clock)
        self.assertEqual(len(self.occurrences(plan["plan_id"])), count)

    def test_invalid_fixed_time_is_schedule_invalid(self):
        with self.assertRaises(DomainError) as caught:
            self.draft(schedule_type="FIXED_TIME", schedule_config={"times": ["25:00"]})
        self.assertEqual(caught.exception.message, "SCHEDULE_INVALID")
        self.assertEqual(
            self.service.storage.fetchone("SELECT COUNT(*) AS n FROM medication_safety_check")["n"],
            0,
        )

    def test_meal_relation_requires_explicit_routine_before_m2(self):
        plan = self.draft(
            schedule_type="MEAL_RELATION",
            schedule_config={
                "meal": "BREAKFAST", "relation": "AFTER", "offset_minutes": 30
            },
        )
        with self.assertRaises(DomainError) as caught:
            self.approve(plan)
        self.assertEqual(caught.exception.message, "SCHEDULE_CONTEXT_MISSING")
        self.assertEqual(len(self.service.list_safety_history(plan["plan_id"])), 0)

    def test_meal_relation_expands_from_routine(self):
        self.service.update_routine("E001", {"breakfast_time": "08:00"}, self.clock)
        plan = self.draft(
            schedule_type="MEAL_RELATION",
            schedule_config={
                "meal": "BREAKFAST", "relation": "AFTER", "offset_minutes": 30
            },
        )
        self.approve(plan)
        first = self.occurrences(plan["plan_id"])[0]
        self.assertEqual(first["scheduled_at"], "2026-09-20T00:30:00+00:00")
        self.assertEqual(first["schedule_type"], "MEAL_RELATION")

    def test_meal_relation_negative_offset_crosses_previous_day(self):
        routine = ElderRoutine("E001", breakfast_time="00:15")
        specs = ScheduleExpander().expand(
            {
                "schedule_type": "MEAL_RELATION",
                "schedule_config": {
                    "meal": "BREAKFAST", "relation": "BEFORE", "offset_minutes": 30
                },
            },
            date(2026, 9, 20), date(2026, 9, 20), routine,
        )
        self.assertEqual([item.scheduled_at.isoformat() for item in specs], [
            "2026-09-19T23:45:00+08:00"
        ])

    def test_interval_expands_across_midnight(self):
        specs = ScheduleExpander().expand(
            {
                "schedule_type": "INTERVAL",
                "schedule_config": {
                    "interval_hours": 8,
                    "anchor_at": "2026-09-20T08:00:00+08:00",
                },
            },
            datetime(2026, 9, 20, 8, tzinfo=SHANGHAI),
            datetime(2026, 9, 21, 9, tzinfo=SHANGHAI),
        )
        self.assertEqual([item.scheduled_at.strftime("%H:%M") for item in specs], [
            "08:00", "16:00", "00:00", "08:00"
        ])

    def test_interval_without_anchor_is_invalid(self):
        with self.assertRaises(ScheduleError) as caught:
            ScheduleValidator.validate("INTERVAL", {"interval_hours": 8})
        self.assertEqual(caught.exception.code, "SCHEDULE_INVALID")

    def test_weekly_uses_iso_weekday(self):
        specs = ScheduleExpander().expand(
            {
                "schedule_type": "WEEKLY",
                "schedule_config": {"weekdays": [1, 3, 5], "times": ["09:00"]},
            },
            date(2026, 9, 21), date(2026, 9, 27),
        )
        self.assertEqual([item.scheduled_at.date().isoformat() for item in specs], [
            "2026-09-21", "2026-09-23", "2026-09-25"
        ])
        with self.assertRaises(ScheduleError):
            ScheduleValidator.validate("WEEKLY", {"weekdays": [0], "times": ["09:00"]})

    def test_cycle_handles_on_off_and_month_boundary(self):
        specs = ScheduleExpander().expand(
            {
                "schedule_type": "CYCLE",
                "schedule_config": {
                    "cycle_start_date": "2026-09-28",
                    "days_on": 5,
                    "days_off": 2,
                    "times": ["08:00"],
                },
            },
            date(2026, 9, 28), date(2026, 10, 5),
        )
        self.assertEqual([item.scheduled_at.date().isoformat() for item in specs], [
            "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02",
            "2026-10-05"
        ])

    def test_prn_can_be_approved_without_automatic_occurrence(self):
        plan = self.draft(
            schedule_type="PRN",
            schedule_config={"condition_text": "疼痛时"},
        )
        active = self.approve(plan)
        self.assertEqual(active["status"], "active")
        self.assertEqual(len(self.occurrences(plan["plan_id"])), 0)

    def test_restart_refills_missing_future_occurrences_without_duplicates(self):
        plan = self.approve(self.draft())
        rows = self.occurrences(plan["plan_id"])
        self.service.storage.execute(
            "DELETE FROM medication_occurrence WHERE occurrence_id=?", (rows[-1]["occurrence_id"],)
        )
        self.service.close()
        self.service = MedicationService(self.db_path)
        self.service.ensure_occurrences(self.clock)
        self.assertEqual(len(self.occurrences(plan["plan_id"])), 7)
        self.service.ensure_occurrences(self.clock)
        self.assertEqual(len(self.occurrences(plan["plan_id"])), 7)

    def test_revision_replaces_future_schedule_and_rechecks_m2(self):
        active = self.approve(self.draft(schedule_time="08:00"))
        revised = self.service.revise_plan(active["plan_id"], {"schedule_time": "09:00"})
        self.assertEqual(revised["version"], 2)
        self.approve(revised)
        self.assertEqual(revised["schedule_config"], {"times": ["09:00"]})
        old_future = self.occurrences(active["plan_id"])
        old = [row for row in old_future if row["plan_version"] == 1]
        new = [row for row in old_future if row["plan_version"] == 2]
        self.assertTrue(old and new)
        self.assertTrue(all(row["intake_status"] == "cancelled" for row in old))
        self.assertEqual(len(self.service.list_safety_history(active["plan_id"])), 2)

    def test_routine_change_recalculates_future_and_preserves_history(self):
        self.service.update_routine("E001", {"breakfast_time": "08:00"}, self.clock)
        plan = self.draft(
            schedule_type="MEAL_RELATION",
            schedule_config={
                "meal": "BREAKFAST", "relation": "AFTER", "offset_minutes": 30
            },
        )
        self.approve(plan)
        before = self.occurrences(plan["plan_id"])
        result = self.service.update_routine("E001", {"breakfast_time": "08:30"}, self.clock)
        self.assertEqual(len(result["recalculated"]), 1)
        after = self.occurrences(plan["plan_id"])
        self.assertTrue(any(row["intake_status"] == "cancelled" for row in after))
        self.assertTrue(any(row["scheduled_at"] == "2026-09-20T01:00:00+00:00" for row in after))
        self.assertEqual(len(self.service.list_event_log("medication.schedule.recalculated")), 1)
        self.service.update_routine("E001", {"breakfast_time": "08:30"}, self.clock)
        self.assertEqual(len(self.occurrences(plan["plan_id"])), len(after))

    def test_preview_matches_expansion_without_writing_database(self):
        plan = self.approve(self.draft(
            schedule_type="FIXED_TIME",
            schedule_config={"times": ["08:00", "20:00"]},
        ))
        before = self.service.storage.fetchone(
            "SELECT COUNT(*) AS n FROM medication_occurrence WHERE plan_id=?", (plan["plan_id"],)
        )["n"]
        preview = self.service.preview_schedule(plan["plan_id"], now=self.clock)
        self.assertFalse(preview["persisted"])
        self.assertEqual(len(preview["items"]), before)
        self.assertEqual(
            [item["scheduled_at"] for item in preview["items"]],
            [row["scheduled_at"] for row in self.occurrences(plan["plan_id"])],
        )
        after = self.service.storage.fetchone(
            "SELECT COUNT(*) AS n FROM medication_occurrence WHERE plan_id=?", (plan["plan_id"],)
        )["n"]
        self.assertEqual(before, after)

    def test_http_routine_and_preview_routes(self):
        app = Application(self.service)
        status, _ = app.handle("PUT", "/api/v1/medication/elders/E001/routine", {
            "breakfast_time": "08:00"
        })
        self.assertEqual(status, 200)
        plan = self.draft(
            schedule_type="MEAL_RELATION",
            schedule_config={
                "meal": "BREAKFAST", "relation": "AFTER", "offset_minutes": 30
            },
        )
        status, preview = app.handle(
            "GET", "/api/v1/medication/plans/%s/schedule/preview" % plan["plan_id"],
            query={
                "horizon_days": "1",
                "now": self.clock.isoformat(),
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(preview["items"][0]["scheduled_at_local"], "2026-09-20T08:30:00+08:00")

    def test_harness_does_not_guess_two_daily_times(self):
        class FakeAdapter:
            def status(self):
                return {"ready": True}

            def parse_plan(self, text, elder_id):
                return {
                    "kind": "plan_draft", "elder_id": elder_id,
                    "drug_name": "TEST_DRUG_A", "dosage_text": "5mg",
                    "schedule_type": None, "schedule_config": None,
                    "schedule_time": None, "start_date": "2026-09-20",
                    "missing_fields": ["schedule_config"], "timezone": "Asia/Shanghai",
                }

        from medication_reminder.semantic.agent import MedicationSemanticAgent
        result = MedicationSemanticAgent(self.service, FakeAdapter()).handle(
            "E001", "一天两次提醒我吃 TEST_DRUG_A 5mg"
        )
        self.assertEqual(result["kind"], "plan_clarification")
        self.assertIn("schedule_config", result["missing_fields"])
        self.assertEqual(self.service.list_plans("E001"), [])

    def test_harness_style_meal_draft_does_not_guess_routine(self):
        class FakeAdapter:
            def status(self):
                return {"ready": True}

            def parse_plan(self, text, elder_id):
                return {
                    "kind": "plan_draft", "elder_id": elder_id,
                    "drug_name": "TEST_DRUG_A", "dosage_text": "5mg",
                    "schedule_type": "MEAL_RELATION",
                    "schedule_config": {
                        "meal": "BREAKFAST", "relation": "AFTER", "offset_minutes": 30
                    },
                    "schedule_time": None, "start_date": "2026-09-20",
                    "missing_fields": [], "timezone": "Asia/Shanghai",
                }

        from medication_reminder.semantic.agent import MedicationSemanticAgent
        result = MedicationSemanticAgent(self.service, FakeAdapter()).handle(
            "E001", "早餐后半小时吃 TEST_DRUG_A 5mg"
        )
        self.assertEqual(result["kind"], "plan_draft_created")
        self.assertEqual(result["draft"]["schedule_type"], "MEAL_RELATION")
        self.assertEqual(self.service.list_safety_history(result["draft"]["plan_id"]), [])


if __name__ == "__main__":
    unittest.main()
