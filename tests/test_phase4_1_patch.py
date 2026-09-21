import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from medication_reminder.routine import ElderRoutine
from medication_reminder.schedule import ScheduleExpander, ScheduleType
from medication_reminder.safety import FixtureDoseRuleProvider
from medication_reminder.service import MedicationService, SHANGHAI, iso


class Phase41PatchTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "medication.db")
        self.service = MedicationService(self.db_path)
        self.local_clock = datetime(2026, 9, 20, 0, 0, tzinfo=SHANGHAI)
        self.clock = self.local_clock.astimezone(timezone.utc)

    def tearDown(self):
        service = getattr(self, "service", None)
        if service is not None:
            service.close()
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

    def rows(self, plan_id):
        return self.service.storage.fetchall(
            "SELECT * FROM medication_occurrence WHERE plan_id=? "
            "ORDER BY scheduled_at, occurrence_id",
            (plan_id,),
        )

    @staticmethod
    def snapshot(row):
        return json.loads(row["schedule_snapshot_json"])

    def meal_plan(self):
        return self.draft(
            schedule_type="MEAL_RELATION",
            schedule_config={
                "meal": "BREAKFAST", "relation": "AFTER", "offset_minutes": 30
            },
        )

    def test_meal_occurrence_snapshot_contains_resolved_anchor_and_version(self):
        self.service.update_routine("E001", {"breakfast_time": "08:00"}, self.clock)
        active = self.approve(self.meal_plan())
        snapshot = self.snapshot(self.rows(active["plan_id"])[0])
        self.assertEqual(snapshot["resolved_anchor_type"], "BREAKFAST")
        self.assertEqual(snapshot["resolved_anchor_time"], "08:00")
        self.assertEqual(snapshot["routine_version"], 1)
        self.assertEqual(
            snapshot["resolved_anchor_local_datetime"],
            "2026-09-20T08:00:00+08:00",
        )
        self.assertEqual(
            snapshot["resolved_local_datetime"],
            "2026-09-20T08:30:00+08:00",
        )

    def test_old_occurrence_snapshot_is_immutable_after_routine_change(self):
        self.service.update_routine("E001", {"breakfast_time": "08:00"}, self.clock)
        active = self.approve(self.meal_plan())
        self.service.update_routine(
            "E001", {"breakfast_time": "09:00"}, self.clock + timedelta(minutes=1)
        )
        rows = self.rows(active["plan_id"])
        old = next(row for row in rows if row["scheduled_at"] == "2026-09-20T00:30:00+00:00")
        new = next(row for row in rows if row["scheduled_at"] == "2026-09-20T01:30:00+00:00")
        self.assertEqual(self.snapshot(old)["resolved_anchor_time"], "08:00")
        self.assertEqual(self.snapshot(old)["routine_version"], 1)
        self.assertEqual(self.snapshot(new)["resolved_anchor_time"], "09:00")
        self.assertEqual(self.snapshot(new)["routine_version"], 2)

    def test_identical_routine_put_does_not_increment_or_recalculate(self):
        first = self.service.update_routine(
            "E001", {"breakfast_time": "08:00"}, self.clock
        )
        before_events = len(self.service.list_event_log("medication.routine.updated"))
        second = self.service.update_routine(
            "E001", {"breakfast_time": "08:00"}, self.clock + timedelta(minutes=1)
        )
        self.assertEqual(second["recalculated"], [])
        self.assertEqual(second["routine"]["routine_version"], 1)
        self.assertEqual(second["routine"]["updated_at"], first["routine"]["updated_at"])
        self.assertEqual(
            len(self.service.list_event_log("medication.routine.updated")),
            before_events,
        )

    def test_changed_routine_increments_version(self):
        self.service.update_routine("E001", {"breakfast_time": "08:00"}, self.clock)
        changed = self.service.update_routine(
            "E001", {"breakfast_time": "08:30"}, self.clock + timedelta(minutes=1)
        )
        self.assertEqual(changed["routine"]["routine_version"], 2)
        self.assertNotEqual(changed["routine"]["updated_at"], None)

    def test_new_occurrence_uses_new_routine_version(self):
        self.service.update_routine("E001", {"breakfast_time": "08:00"}, self.clock)
        active = self.approve(self.meal_plan())
        self.service.update_routine(
            "E001", {"breakfast_time": "08:30"}, self.clock + timedelta(minutes=1)
        )
        new = next(
            row for row in self.rows(active["plan_id"])
            if row["scheduled_at"] == "2026-09-20T01:00:00+00:00"
        )
        self.assertEqual(self.snapshot(new)["routine_version"], 2)

    def test_routine_recalculation_failure_rolls_back_everything(self):
        self.service.update_routine("E001", {"breakfast_time": "08:00"}, self.clock)
        active = self.approve(self.meal_plan())
        before_routine = self.service.get_routine("E001")
        before_rows = [dict(row) for row in self.rows(active["plan_id"])]

        def fail_after_routine_save(*args, **kwargs):
            raise RuntimeError("injected regeneration failure")

        with patch.object(
            self.service,
            "_ensure_occurrences_in_transaction",
            side_effect=fail_after_routine_save,
        ):
            with self.assertRaises(RuntimeError):
                self.service.update_routine(
                    "E001", {"breakfast_time": "08:30"},
                    self.clock + timedelta(minutes=1),
                )

        self.assertEqual(self.service.get_routine("E001"), before_routine)
        self.assertEqual([dict(row) for row in self.rows(active["plan_id"])], before_rows)
        self.assertEqual(len(self.service.list_event_log("medication.schedule.recalculated")), 0)

    def test_rollback_state_is_consistent_after_service_restart(self):
        self.service.update_routine("E001", {"breakfast_time": "08:00"}, self.clock)
        active = self.approve(self.meal_plan())

        def fail_after_routine_save(*args, **kwargs):
            raise RuntimeError("injected regeneration failure")

        with patch.object(
            self.service,
            "_ensure_occurrences_in_transaction",
            side_effect=fail_after_routine_save,
        ):
            with self.assertRaises(RuntimeError):
                self.service.update_routine(
                    "E001", {"breakfast_time": "08:30"},
                    self.clock + timedelta(minutes=1),
                )
        self.service.close()
        self.service = MedicationService(self.db_path)
        self.assertEqual(self.service.get_routine("E001")["routine_version"], 1)
        self.assertEqual(
            len([row for row in self.rows(active["plan_id"])
                 if row["intake_status"] == "unconfirmed"]),
            7,
        )

    def test_routine_change_m2_block_cancels_future_without_new_active_occurrence(self):
        self.service.update_routine("E001", {"breakfast_time": "08:00"}, self.clock)
        active = self.approve(self.meal_plan())
        self.service.close()
        self.service = MedicationService(
            self.db_path,
            config={
                "safety_rule_provider": FixtureDoseRuleProvider(
                    {"TEST_DRUG_A": {"max_single_dose": 1}}, "fixture-routine-block-v2"
                )
            },
        )
        result = self.service.update_routine(
            "E001", {"breakfast_time": "09:00"}, self.clock + timedelta(minutes=1)
        )
        self.assertEqual(result["recalculated"][0]["safety_status"], "BLOCK")
        self.assertEqual(self.service.get_routine("E001")["routine_version"], 2)
        future = [
            row for row in self.rows(active["plan_id"])
            if row["scheduled_at"] > iso(self.clock + timedelta(minutes=1))
        ]
        self.assertTrue(future)
        self.assertTrue(all(row["intake_status"] == "cancelled" for row in future))
        self.assertEqual(
            self.service.get_latest_safety_check(active["plan_id"], 1)["status"],
            "BLOCK",
        )

    def test_meal_candidate_before_effective_from_is_filtered(self):
        routine = ElderRoutine("E001", breakfast_time="00:15", routine_version=3)
        plan = {
            "schedule_type": "MEAL_RELATION",
            "schedule_config": {
                "meal": "BREAKFAST", "relation": "BEFORE", "offset_minutes": 30
            },
            "start_date": "2026-09-20",
            "effective_from": "2026-09-20T00:00:00+08:00",
        }
        specs = ScheduleExpander().expand(
            plan=plan,
            start=date(2026, 9, 20),
            end=date(2026, 9, 20),
            routine=routine,
        )
        self.assertEqual(specs, [])

    def test_candidate_equal_to_effective_from_is_included(self):
        routine = ElderRoutine("E001", breakfast_time="00:30", routine_version=1)
        plan = {
            "schedule_type": "MEAL_RELATION",
            "schedule_config": {
                "meal": "BREAKFAST", "relation": "BEFORE", "offset_minutes": 30
            },
            "start_date": "2026-09-20",
            "effective_from": "2026-09-20T00:00:00+08:00",
        }
        specs = ScheduleExpander().expand(
            plan=plan,
            start=date(2026, 9, 20),
            end=date(2026, 9, 20),
            routine=routine,
        )
        self.assertEqual([item.scheduled_at.isoformat() for item in specs], [
            "2026-09-20T00:00:00+08:00"
        ])

    def test_fixed_time_at_effective_until_is_excluded(self):
        plan = {
            "schedule_type": "FIXED_TIME",
            "schedule_config": {"times": ["08:00"]},
            "start_date": "2026-09-20",
            "effective_from": "2026-09-20T00:00:00+08:00",
            "effective_until": "2026-09-25T00:00:00+08:00",
        }
        specs = ScheduleExpander().expand(
            plan=plan,
            start=datetime(2026, 9, 20, tzinfo=SHANGHAI),
            end=datetime(2026, 9, 26, tzinfo=SHANGHAI),
        )
        self.assertEqual([item.scheduled_at.date().isoformat() for item in specs], [
            "2026-09-20", "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24"
        ])

    def test_interval_anchor_before_effective_from_keeps_mathematical_sequence(self):
        plan = {
            "schedule_type": "INTERVAL",
            "schedule_config": {"interval_hours": 8, "anchor_at": "2026-09-19T08:00:00+08:00"},
            "start_date": "2026-09-19",
            "effective_from": "2026-09-20T00:00:00+08:00",
        }
        specs = ScheduleExpander().expand(
            plan=plan,
            start=datetime(2026, 9, 20, tzinfo=SHANGHAI),
            end=datetime(2026, 9, 21, tzinfo=SHANGHAI),
        )
        self.assertEqual([item.scheduled_at.isoformat() for item in specs], [
            "2026-09-20T00:00:00+08:00",
            "2026-09-20T08:00:00+08:00",
            "2026-09-20T16:00:00+08:00",
        ])
        self.assertEqual(self.snapshot_from_spec(specs[0])["interval_index"], 2)

    @staticmethod
    def snapshot_from_spec(spec):
        return spec.schedule_snapshot

    def test_weekly_horizon_is_clipped_by_effective_until(self):
        plan = {
            "schedule_type": "WEEKLY",
            "schedule_config": {"weekdays": [1], "times": ["08:00"]},
            "start_date": "2026-09-21",
            "effective_from": "2026-09-21T00:00:00+08:00",
            "effective_until": "2026-09-25T00:00:00+08:00",
        }
        specs = ScheduleExpander().expand(
            plan=plan,
            start=date(2026, 9, 21),
            end=date(2026, 10, 5),
        )
        self.assertTrue(specs)
        self.assertTrue(all(item.scheduled_at < datetime(2026, 9, 25, tzinfo=SHANGHAI)
                            for item in specs))

    def test_cycle_anchor_before_effective_from_keeps_cycle_day(self):
        plan = {
            "schedule_type": "CYCLE",
            "schedule_config": {
                "cycle_start_date": "2026-09-18",
                "days_on": 2,
                "days_off": 1,
                "times": ["08:00"],
            },
            "start_date": "2026-09-18",
            "effective_from": "2026-09-20T00:00:00+08:00",
        }
        specs = ScheduleExpander().expand(
            plan=plan,
            start=date(2026, 9, 20),
            end=date(2026, 9, 23),
        )
        self.assertEqual([item.scheduled_at.date().isoformat() for item in specs], [
            "2026-09-21", "2026-09-22"
        ])
        self.assertEqual(specs[0].schedule_snapshot["cycle_day"], 1)

    def test_preview_is_strictly_limited_to_plan_effective_range(self):
        active = self.approve(self.draft(end_date="2026-09-22"))
        preview = self.service.preview_schedule(
            active["plan_id"], now=self.clock, horizon_days=7
        )
        self.assertEqual(len(preview["items"]), 3)
        self.assertTrue(all(
            item["scheduled_at_local"] < "2026-09-23T00:00:00+08:00"
            for item in preview["items"]
        ))

    def test_formal_generation_matches_preview_under_effective_range(self):
        active = self.approve(self.draft(end_date="2026-09-22"))
        preview = self.service.preview_schedule(
            active["plan_id"], now=self.clock, horizon_days=7
        )
        generated = [row["scheduled_at"] for row in self.rows(active["plan_id"])]
        self.assertEqual(generated, [item["scheduled_at"] for item in preview["items"]])

    def test_fixed_snapshot_keeps_selected_time(self):
        active = self.approve(self.draft(schedule_config={"times": ["08:00", "20:00"]}))
        snapshots = [self.snapshot(row) for row in self.rows(active["plan_id"])[:2]]
        self.assertEqual([item["selected_time"] for item in snapshots], ["08:00", "20:00"])

    def test_other_schedule_snapshots_keep_interval_weekly_and_cycle_context(self):
        interval = ScheduleExpander().expand(
            {"schedule_type": "INTERVAL", "schedule_config": {
                "interval_hours": 8, "anchor_at": "2026-09-20T08:00:00+08:00"
            }},
            datetime(2026, 9, 20, 8, tzinfo=SHANGHAI),
            datetime(2026, 9, 20, 17, tzinfo=SHANGHAI),
        )
        weekly = ScheduleExpander().expand(
            {"schedule_type": "WEEKLY", "schedule_config": {
                "weekdays": [7], "times": ["09:00"]
            }},
            date(2026, 9, 20), date(2026, 9, 20),
        )
        cycle = ScheduleExpander().expand(
            {"schedule_type": "CYCLE", "schedule_config": {
                "cycle_start_date": "2026-09-20", "days_on": 1,
                "days_off": 1, "times": ["10:00"]
            }},
            date(2026, 9, 20), date(2026, 9, 20),
        )
        self.assertEqual(interval[0].schedule_snapshot["interval_index"], 0)
        self.assertEqual(weekly[0].schedule_snapshot["selected_weekday"], 7)
        self.assertEqual(cycle[0].schedule_snapshot["cycle_day"], 1)

    def test_history_statuses_survive_routine_recalculation(self):
        self.service.update_routine("E001", {"breakfast_time": "08:00"}, self.clock)
        active = self.approve(self.meal_plan())
        rows = self.rows(active["plan_id"])
        for row, status in zip(rows[:3], ("confirmed_taken", "skipped", "closed_unconfirmed")):
            self.service.storage.execute(
                "UPDATE medication_occurrence SET intake_status=? WHERE occurrence_id=?",
                (status, row["occurrence_id"]),
            )
        self.service.update_routine(
            "E001", {"breakfast_time": "08:30"}, self.clock + timedelta(minutes=1)
        )
        statuses = {
            row["intake_status"]
            for row in self.rows(active["plan_id"])
            if row["scheduled_at"] in {item["scheduled_at"] for item in rows[:3]}
        }
        self.assertEqual(statuses, {"confirmed_taken", "skipped", "closed_unconfirmed"})

    def test_repeated_recalculation_reactivates_same_identity_without_duplicates(self):
        active = self.approve(self.draft())
        for offset in (1, 2):
            self.service.recalculate_plan_schedule(
                active["plan_id"], now=self.clock + timedelta(minutes=offset)
            )
        rows = self.rows(active["plan_id"])
        self.assertEqual(len(rows), 7)
        self.assertEqual(
            len([row for row in rows if row["intake_status"] == "unconfirmed"]), 7
        )

    def test_legacy_routine_row_gets_version_one_during_migration(self):
        legacy_path = str(Path(self.temp_dir.name) / "legacy.db")
        connection = sqlite3.connect(legacy_path)
        connection.execute(
            """CREATE TABLE elder_routine (
                elder_id TEXT PRIMARY KEY,
                breakfast_time TEXT,
                lunch_time TEXT,
                dinner_time TEXT,
                bedtime TEXT,
                timezone TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            "INSERT INTO elder_routine VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("E001", "08:00", None, None, None, "Asia/Shanghai", "old"),
        )
        connection.commit()
        connection.close()
        legacy_service = MedicationService(legacy_path)
        try:
            routine = legacy_service.get_routine("E001")
            self.assertEqual(routine["routine_version"], 1)
        finally:
            legacy_service.close()


if __name__ == "__main__":
    unittest.main()
