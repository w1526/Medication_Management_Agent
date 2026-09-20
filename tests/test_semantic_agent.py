import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from medication_reminder.semantic.agent import MedicationSemanticAgent
from medication_reminder.service import MedicationService, SHANGHAI


class FakeSemanticAdapter:
    def __init__(self):
        self.plan_calls = 0
        self.response_calls = 0
        self.omit_start_date = False
        self.schedule_time = "21:00"

    def status(self):
        return {"sdk_installed": True, "key_configured": True, "ready": True, "model": "fake"}

    def parse_plan(self, text, elder_id):
        self.plan_calls += 1
        return {
            "kind": "plan_draft",
            "elder_id": elder_id,
            "drug_name": "二甲双胍",
            "dosage_text": "500mg",
            "schedule_time": self.schedule_time,
            "relation_to_meal": "餐后",
            "route": "oral",
            "start_date": None if self.omit_start_date else datetime.now(SHANGHAI).date().isoformat(),
            "timezone": "Asia/Shanghai",
            "missing_fields": ["start_date"] if self.omit_start_date else [],
            "confidence": 0.99,
        }

    def parse_response(self, text):
        self.response_calls += 1
        return {"kind": "medication_response", "action": "CONFIRM_TAKEN", "delay_minutes": None}


class SemanticAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = MedicationService(str(Path(self.temp_dir.name) / "medication.db"))
        self.fake = FakeSemanticAdapter()
        self.agent = MedicationSemanticAgent(self.service, self.fake)

    def tearDown(self):
        self.service.close()
        self.temp_dir.cleanup()

    def test_natural_language_plan_only_creates_draft(self):
        result = self.agent.handle("E001", "每天晚上九点提醒我吃二甲双胍500mg", source="chat_agent")
        self.assertEqual(result["kind"], "plan_draft_created")
        self.assertEqual(result["draft"]["status"], "draft")
        self.assertEqual(self.fake.plan_calls, 1)
        self.assertEqual(self.service.list_plans("E001")[0]["status"], "draft")

    def test_missing_start_date_defaults_to_today(self):
        self.fake.omit_start_date = True
        result = self.agent.handle(
            "E001", "每天晚上十点提醒我吃氨氯地平5mg", source="chat_agent"
        )
        self.assertEqual(result["kind"], "plan_draft_created")
        self.assertEqual(
            result["draft"]["start_date"],
            datetime.now(SHANGHAI).date().isoformat(),
        )
        self.assertEqual(result["draft"]["status"], "draft")

    def test_evening_time_is_normalized_to_24_hour_clock(self):
        self.fake.schedule_time = "10:00"
        result = self.agent.handle(
            "E001", "每天晚上十点提醒我吃氨氯地平5mg", source="chat_agent"
        )
        self.assertEqual(result["kind"], "plan_draft_created")
        self.assertEqual(result["draft"]["schedule_time"], "22:00")
        self.assertTrue(result["schedule_time_normalized"])

    def test_open_interaction_is_bound_before_semantic_response(self):
        local_now = datetime.now(SHANGHAI).replace(second=0, microsecond=0)
        draft = self.service.create_draft({
            "elder_id": "E001",
            "drug_name": "氨氯地平",
            "dosage_text": "5mg",
            "schedule_time": local_now.strftime("%H:%M"),
            "start_date": local_now.date().isoformat(),
            "created_by": "test",
        })
        self.service.approve_plan(
            draft["plan_id"], "doctor:D001", 1, local_now.astimezone(timezone.utc)
        )
        occurrence = self.service.get_today("E001")[0]
        self.service.run_scheduler_cycle(datetime.fromisoformat(occurrence["scheduled_at"]))
        result = self.agent.handle("E001", "这剂我已经处理好了", source="chat_agent")
        self.assertEqual(result["kind"], "medication_response")
        self.assertEqual(result["occurrence"]["intake_status"], "confirmed_taken")
        self.assertEqual(self.fake.response_calls, 1)

    def test_without_open_interaction_ordinary_text_does_not_call_llm(self):
        result = self.agent.handle("E001", "你好，今天天气怎么样", source="chat_agent")
        self.assertEqual(result["kind"], "clarification")
        self.assertEqual(self.fake.plan_calls, 0)

    def test_multiple_open_interactions_require_trusted_binding(self):
        local_now = datetime.now(SHANGHAI).replace(second=0, microsecond=0)
        for drug_name in ("氨氯地平", "二甲双胍"):
            draft = self.service.create_draft({
                "elder_id": "E001",
                "drug_name": drug_name,
                "dosage_text": "5mg",
                "schedule_time": local_now.strftime("%H:%M"),
                "start_date": local_now.date().isoformat(),
                "created_by": "test",
            })
            self.service.approve_plan(
                draft["plan_id"], "doctor:D001", 1, local_now.astimezone(timezone.utc)
            )
        self.service.run_scheduler_cycle(local_now.astimezone(timezone.utc))
        result = self.agent.handle("E001", "吃了", source="chat_agent")
        self.assertEqual(result["kind"], "clarification")
        self.assertTrue(result["requires_interaction_id"])
        self.assertEqual(self.fake.response_calls, 0)


if __name__ == "__main__":
    unittest.main()

