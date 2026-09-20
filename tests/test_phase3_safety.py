import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from medication_reminder.http import Application
from medication_reminder.semantic.agent import MedicationSemanticAgent
from medication_reminder.safety import (
    EmptySafetyRuleProvider,
    FixtureDDIProvider,
    FixtureDoseRuleProvider,
    FixtureSafetyRuleProvider,
    JsonSafetyRuleProvider,
    ProviderCheck,
    SafetyEngine,
    SafetyFinding,
    SafetyFreezeError,
    SafetyFreezeService,
    SafetyRuleProvider,
)
from medication_reminder.service import DomainError, MedicationService, SHANGHAI


class WarningProvider(SafetyRuleProvider):
    configured = True
    ruleset_version = "warning-fixture-v1"

    def check_dose(self, plan, medication):
        return ProviderCheck([
            SafetyFinding(
                category="fixture",
                severity="WARN",
                code="FIXTURE_WARNING",
                message="fictional fixture warning",
                evidence={"medication": medication.get("drug_name")},
                rule_id="fixture-warning",
                rule_version=self.ruleset_version,
            )
        ], "checked")


class FailingProvider(SafetyRuleProvider):
    configured = True
    ruleset_version = "failing-fixture-v1"

    def check_dose(self, plan, medication):
        raise RuntimeError("fixture provider unavailable")


class Phase3SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "medication.db")
        self.local_now = datetime.now(SHANGHAI).replace(second=0, microsecond=0)

    def tearDown(self):
        service = getattr(self, "service", None)
        if service is not None:
            service.close()
        self.temp_dir.cleanup()

    def make_service(self, provider=None, **config):
        if provider is not None:
            config["safety_rule_provider"] = provider
        self.service = MedicationService(self.db_path, config=config)
        return self.service

    def draft(self, service, drug="TEST_DRUG_A", dose="5mg", **extra):
        data = {
            "elder_id": "E001",
            "drug_name": drug,
            "dosage_text": dose,
            "schedule_time": self.local_now.strftime("%H:%M"),
            "start_date": self.local_now.date().isoformat(),
            "created_by": "family:F001",
        }
        data.update(extra)
        return service.create_draft(data)

    def approve(self, service, plan):
        service.submit_plan(plan["plan_id"], plan["version"])
        return service.approve_plan(
            plan["plan_id"], "doctor:D001", plan["version"],
            self.local_now.astimezone(timezone.utc),
        )

    def test_empty_provider_passes_structural_and_exposes_unconfigured_coverage(self):
        result = SafetyEngine(EmptySafetyRuleProvider("empty-v1")).check({
            "plan_id": "p1", "version": 1, "elder_id": "E001",
            "drug_name": "TEST_DRUG_A", "dosage_text": "5mg",
            "schedule_time": "08:00", "start_date": "2026-09-20",
        }).to_dict()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["coverage"]["structural"], "checked")
        self.assertEqual(result["coverage"]["ddi"], "not_configured")

    def test_structural_invalid_dose_and_missing_name_block_approval(self):
        service = self.make_service()
        for drug, dose in (("TEST_DRUG_A", "0mg"), ("", "5mg")):
            plan = self.draft(service, drug=drug, dose=dose)
            service.submit_plan(plan["plan_id"], 1)
            with self.assertRaises(DomainError) as caught:
                service.approve_plan(plan["plan_id"], "doctor:D001", 1, self.local_now)
            self.assertEqual(caught.exception.status, 409)
            self.assertEqual(service.get_plan(plan["plan_id"])["status"], "pending_confirmation")
            self.assertEqual(
                service.get_latest_safety_check(plan["plan_id"], 1)["status"], "BLOCK"
            )

    def test_fixture_dose_rule_blocks_without_occurrence(self):
        service = self.make_service(
            FixtureDoseRuleProvider(
                {"TEST_DRUG_A": {"max_single_dose": 100, "unit": "mg"}},
                "test-rules-v1",
            )
        )
        plan = self.draft(service, dose="120mg")
        with self.assertRaises(DomainError) as caught:
            self.approve(service, plan)
        self.assertEqual(caught.exception.message, "SAFETY_BLOCKED")
        self.assertEqual(service.get_plan(plan["plan_id"])["status"], "pending_confirmation")
        self.assertEqual(
            service.storage.fetchone("SELECT COUNT(*) AS n FROM medication_occurrence")["n"],
            0,
        )
        self.assertEqual(len(service.list_event_log("medication.safety.blocked")), 1)

    def test_fixture_ddi_pairwise_block_and_warn_aggregate_deterministically(self):
        plan = {
            "plan_id": "p-ddi", "version": 1, "elder_id": "E001",
            "schedule_time": "08:00", "start_date": "2026-09-20",
            "medications": [
                {"name": "TEST_DRUG_A", "dosage_text": "1mg"},
                {"name": "TEST_DRUG_B", "dosage_text": "1mg"},
            ],
        }
        blocked = SafetyEngine(ddi_provider=FixtureDDIProvider([
            {"drugs": ["TEST_DRUG_A", "TEST_DRUG_B"], "severity": "BLOCK"}
        ], "test-rules-v1"), ruleset_version="test-rules-v1").check(plan).to_dict()
        self.assertEqual(blocked["status"], "BLOCK")
        warning = SafetyEngine(ddi_provider=FixtureDDIProvider([
            {"drugs": ["TEST_DRUG_A", "TEST_DRUG_B"], "severity": "WARN"}
        ], "test-rules-v1"), ruleset_version="test-rules-v1").check(plan).to_dict()
        self.assertEqual(warning["status"], "WARN")
        self.assertEqual(warning["findings"][0]["severity"], "WARN")

    def test_warn_is_saved_and_plan_can_activate(self):
        service = self.make_service(WarningProvider())
        active = self.approve(service, self.draft(service))
        self.assertEqual(active["status"], "active")
        self.assertEqual(active["safety"]["status"], "WARN")
        self.assertEqual(len(active["safety"]["findings"]), 1)
        self.assertEqual(len(service.list_event_log("medication.safety.warning")), 1)

    def test_provider_or_engine_failure_never_passes(self):
        service = self.make_service(FailingProvider())
        plan = self.draft(service)
        with self.assertRaises(DomainError) as caught:
            self.approve(service, plan)
        self.assertEqual(caught.exception.message, "SAFETY_CHECK_FAILED")
        self.assertEqual(service.get_latest_safety_check(plan["plan_id"], 1)["status"], "BLOCK")
        service.safety_engine.check = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("engine broken")
        )
        second = self.draft(service, drug="TEST_DRUG_B")
        with self.assertRaises(DomainError):
            self.approve(service, second)
        self.assertEqual(service.get_latest_safety_check(second["plan_id"], 1)["status"], "BLOCK")

    def test_revision_does_not_inherit_old_pass(self):
        service = self.make_service(
            FixtureDoseRuleProvider({"TEST_DRUG_A": {"max_single_dose": 100}}, "test-v1")
        )
        active = self.approve(service, self.draft(service, dose="5mg"))
        revised = service.revise_plan(active["plan_id"], {"dosage_text": "120mg"})
        self.assertEqual(revised["safety_status"], "NOT_CHECKED")
        with self.assertRaises(DomainError):
            self.approve(service, revised)
        self.assertEqual(service.get_latest_safety_check(active["plan_id"], 1)["status"], "PASS")
        self.assertEqual(service.get_latest_safety_check(active["plan_id"], 2)["status"], "BLOCK")
        self.assertEqual(
            service.storage.fetchone(
                "SELECT COUNT(*) AS n FROM medication_occurrence WHERE plan_version=2"
            )["n"],
            0,
        )

    def test_ruleset_change_makes_old_pass_stale_and_rechecks(self):
        first = MedicationService(
            self.db_path,
            config={"safety_rule_provider": EmptySafetyRuleProvider("rules-v1")},
        )
        self.service = first
        active = self.approve(first, self.draft(first))
        self.assertEqual(active["safety"]["ruleset_version"], "rules-v1")
        first.close()
        self.service = MedicationService(
            self.db_path,
            config={"safety_rule_provider": EmptySafetyRuleProvider("rules-v2")},
        )
        self.service.run_scheduler_cycle(self.local_now.astimezone(timezone.utc))
        latest = self.service.get_latest_safety_check(active["plan_id"], 1)
        self.assertEqual(latest["ruleset_version"], "rules-v2")
        self.assertEqual(latest["status"], "PASS")
        self.assertGreaterEqual(len(self.service.list_safety_history(active["plan_id"])), 2)

    def test_scheduler_blocks_existing_occurrence_after_ruleset_becomes_block(self):
        first = MedicationService(
            self.db_path,
            config={"safety_rule_provider": EmptySafetyRuleProvider("rules-v1")},
        )
        self.service = first
        active = self.approve(first, self.draft(first, dose="5mg"))
        first.close()
        self.service = MedicationService(
            self.db_path,
            config={
                "safety_rule_provider": FixtureDoseRuleProvider(
                    {"TEST_DRUG_A": {"max_single_dose": 1}}, "rules-v2"
                )
            },
        )
        result = self.service.run_scheduler_cycle(
            self.local_now.astimezone(timezone.utc) + timedelta(minutes=1)
        )
        self.assertEqual(result["reminders_claimed"], [])
        self.assertEqual(self.service.get_latest_safety_check(active["plan_id"], 1)["status"], "BLOCK")
        self.assertEqual(len(self.service.list_event_log("device.interaction.request")), 0)
        self.assertGreaterEqual(len(self.service.list_event_log("medication.safety.blocked")), 1)

    def test_duplicate_approval_is_idempotent_for_occurrences(self):
        service = self.make_service()
        plan = self.draft(service)
        first = self.approve(service, plan)
        count = service.storage.fetchone("SELECT COUNT(*) AS n FROM medication_occurrence")["n"]
        second = service.approve_plan(plan["plan_id"], "doctor:D001", 1, self.local_now)
        self.assertEqual(second["status"], "active")
        self.assertEqual(
            service.storage.fetchone("SELECT COUNT(*) AS n FROM medication_occurrence")["n"],
            count,
        )
        self.assertEqual(first["safety"]["check_id"], second["safety"]["check_id"])

    def test_safety_api_returns_latest_history_and_check(self):
        service = self.make_service()
        plan = self.approve(service, self.draft(service))
        app = Application(service)
        status, latest = app.handle("GET", "/api/v1/medication/plans/%s/safety" % plan["plan_id"])
        self.assertEqual(status, 200)
        self.assertEqual(latest["status"], "PASS")
        status, history = app.handle(
            "GET", "/api/v1/medication/plans/%s/safety/history" % plan["plan_id"]
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(history["items"]), 1)
        status, check = app.handle(
            "GET", "/api/v1/medication/safety/checks/%s" % latest["check_id"]
        )
        self.assertEqual(status, 200)
        self.assertEqual(check["check_id"], latest["check_id"])

    def test_safety_freeze_rejects_disable_override_relax_and_agent_mutation(self):
        freeze = SafetyFreezeService()
        with self.assertRaises(SafetyFreezeError):
            freeze.assert_change_allowed("m2.check.disable", new=True)
        with self.assertRaises(SafetyFreezeError):
            freeze.assert_change_allowed("m2.block.override", new=True)
        with self.assertRaises(SafetyFreezeError):
            freeze.assert_change_allowed(
                "m2.severity.relax", from_severity="BLOCK", to_severity="WARN"
            )
        with self.assertRaises(SafetyFreezeError):
            freeze.assert_change_allowed("plan.medication.dose", source="llm", old="5mg", new="1mg")


class FullCoverageProvider(SafetyRuleProvider):
    configured = True
    provider_name = "full_fixture"
    ruleset_version = "full-fixture-v1"

    def _checked(self):
        return ProviderCheck([], "checked")

    def check_dose(self, plan, medication):
        return self._checked()

    def check_ddi(self, plan, medications):
        return self._checked()

    def check_allergy(self, plan, medications):
        return self._checked()

    def check_contraindication(self, plan, medications):
        return self._checked()


class HarnessDraftAdapter:
    def status(self):
        return {"sdk_installed": True, "key_configured": True, "ready": True}

    def parse_plan(self, text, elder_id):
        return {
            "kind": "plan_draft",
            "elder_id": elder_id,
            "drug_name": "TEST_DRUG_A",
            "dosage_text": "1mg",
            "schedule_time": "21:00",
            "relation_to_meal": None,
            "route": "oral",
            "start_date": datetime.now(SHANGHAI).date().isoformat(),
            "missing_fields": [],
        }


class Phase31SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "medication.db")
        self.local_now = datetime.now(SHANGHAI).replace(second=0, microsecond=0)
        self.service = None

    def tearDown(self):
        if self.service is not None:
            self.service.close()
        self.temp_dir.cleanup()

    def make_service(self, provider=None, **config):
        if provider is not None:
            config["safety_rule_provider"] = provider
        self.service = MedicationService(self.db_path, config=config)
        return self.service

    def draft(self, service, dose="5mg", drug="TEST_DRUG_A", **extra):
        data = {
            "elder_id": "E001",
            "drug_name": drug,
            "dosage_text": dose,
            "schedule_time": self.local_now.strftime("%H:%M"),
            "start_date": self.local_now.date().isoformat(),
            "created_by": "fixture",
        }
        data.update(extra)
        return service.create_draft(data)

    def approve(self, service, plan):
        return service.approve_plan(
            plan["plan_id"], "doctor:D001", plan["version"], self.local_now
        )

    def test_rule_pack_semantic_same_content_reuses_fingerprint_and_check(self):
        pack_a = {
            "ruleset_version": "fixture-v1",
            "dose_rules": {"TEST_DRUG_A": {"max_single_dose": 100, "unit": "mg"}},
            "ddi_rules": [],
        }
        service = self.make_service(JsonSafetyRuleProvider(pack_a))
        active = self.approve(service, self.draft(service))
        fingerprint = active["safety"]["ruleset_fingerprint"]
        service.close()
        pack_b = {
            "ddi_rules": [],
            "dose_rules": {"TEST_DRUG_A": {"unit": "mg", "max_single_dose": 100}},
            "ruleset_version": "fixture-v1",
        }
        service = self.make_service(JsonSafetyRuleProvider(pack_b))
        service.ensure_occurrences(self.local_now + timedelta(minutes=1))
        latest = service.get_latest_safety_check(active["plan_id"], 1)
        self.assertEqual(latest["ruleset_fingerprint"], fingerprint)
        self.assertEqual(len(service.list_safety_history(active["plan_id"])), 1)

    def test_rule_pack_content_change_same_version_makes_check_stale(self):
        service = self.make_service(JsonSafetyRuleProvider({
            "ruleset_version": "fixture-v1",
            "dose_rules": {"TEST_DRUG_A": {"max_single_dose": 100}},
        }))
        active = self.approve(service, self.draft(service))
        old_fingerprint = active["safety"]["ruleset_fingerprint"]
        service.close()
        service = self.make_service(JsonSafetyRuleProvider({
            "ruleset_version": "fixture-v1",
            "dose_rules": {"TEST_DRUG_A": {"max_single_dose": 1}},
        }))
        service.run_scheduler_cycle(self.local_now + timedelta(minutes=1))
        latest = service.get_latest_safety_check(active["plan_id"], 1)
        self.assertEqual(latest["status"], "BLOCK")
        self.assertNotEqual(latest["ruleset_fingerprint"], old_fingerprint)
        self.assertGreaterEqual(len(service.list_safety_history(active["plan_id"])), 2)

    def test_legacy_check_without_fingerprint_is_stale(self):
        service = self.make_service(EmptySafetyRuleProvider("empty-v1"))
        active = self.approve(service, self.draft(service))
        check_id = active["safety"]["check_id"]
        service.storage.connection.execute(
            "UPDATE medication_safety_check SET ruleset_fingerprint=NULL WHERE check_id=?",
            (check_id,),
        )
        service.close()
        service = self.make_service(EmptySafetyRuleProvider("empty-v1"))
        service.ensure_occurrences(self.local_now + timedelta(minutes=1))
        history = service.list_safety_history(active["plan_id"])
        self.assertEqual(len(history), 2)
        self.assertTrue(history[-1]["ruleset_fingerprint"])

    def test_active_plan_direct_dose_update_requires_revision(self):
        service = self.make_service()
        active = self.approve(service, self.draft(service))
        with self.assertRaises(DomainError):
            service.update_plan(active["plan_id"], {"dosage_text": "99mg"})
        self.assertEqual(service.get_plan(active["plan_id"], 1)["dosage_text"], "5mg")

    def test_active_plan_direct_identity_update_requires_revision(self):
        service = self.make_service()
        active = self.approve(service, self.draft(service))
        with self.assertRaises(DomainError):
            service.update_plan(active["plan_id"], {"drug_name": "TEST_DRUG_B"})
        self.assertEqual(service.get_plan(active["plan_id"], 1)["drug_name"], "TEST_DRUG_A")

    def test_harness_can_create_draft_with_explicit_dose(self):
        service = self.make_service()
        agent = MedicationSemanticAgent(service, HarnessDraftAdapter())
        result = agent.handle("E001", "每天晚上八点吃 TEST_DRUG_A 1mg", source="harness")
        self.assertEqual(result["kind"], "plan_draft_created")
        self.assertEqual(result["draft"]["status"], "draft")
        self.assertEqual(result["draft"]["dosage_text"], "1mg")

    def test_direct_activation_without_safety_is_rejected_by_service_and_sqlite(self):
        service = self.make_service()
        draft = self.draft(service)
        with self.assertRaises(DomainError):
            service.update_plan(draft["plan_id"], {"status": "active"})
        with self.assertRaises(sqlite3.IntegrityError):
            service.storage.connection.execute(
                "UPDATE medication_plan SET status='active' WHERE plan_id=? AND version=1",
                (draft["plan_id"],),
            )
        self.assertEqual(service.get_plan(draft["plan_id"], 1)["status"], "draft")

    def test_harness_cannot_directly_activate_draft(self):
        service = self.make_service()
        agent = MedicationSemanticAgent(service, HarnessDraftAdapter())
        draft = agent.handle("E001", "每天晚上八点吃 TEST_DRUG_A 1mg", source="harness")["draft"]
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(service.get_plan(draft["plan_id"], 1)["status"], "draft")

    def test_manual_safety_block_cancels_future_occurrences(self):
        service = self.make_service(EmptySafetyRuleProvider("fixture-v1"))
        active = self.approve(service, self.draft(service))
        service.close()
        service = self.make_service(
            FixtureDoseRuleProvider({"TEST_DRUG_A": {"max_single_dose": 1}}, "fixture-v1")
        )
        result = service.check_plan_safety(active["plan_id"], 1, self.local_now + timedelta(minutes=1))
        self.assertEqual(result["status"], "BLOCK")
        cancelled = service.storage.fetchone(
            "SELECT COUNT(*) AS n FROM medication_occurrence "
            "WHERE plan_id=? AND plan_version=1 AND intake_status='cancelled' "
            "AND cancel_reason='SAFETY_BLOCKED' AND cancelled_by_safety_check_id=?",
            (active["plan_id"], result["check_id"]),
        )
        self.assertGreater(cancelled["n"], 0)
        self.assertEqual(
            len(service.list_event_log("medication.safety.future_occurrences_cancelled")), 1
        )

    def test_safety_block_preserves_confirmed_and_closed_history(self):
        service = self.make_service(EmptySafetyRuleProvider("fixture-v1"))
        active = self.approve(service, self.draft(service))
        rows = service.storage.fetchall(
            "SELECT occurrence_id FROM medication_occurrence WHERE plan_id=? ORDER BY scheduled_at",
            (active["plan_id"],),
        )
        service.storage.connection.execute(
            "UPDATE medication_occurrence SET intake_status='confirmed_taken' WHERE occurrence_id=?",
            (rows[0]["occurrence_id"],),
        )
        service.storage.connection.execute(
            "UPDATE medication_occurrence SET intake_status='closed_unconfirmed' WHERE occurrence_id=?",
            (rows[1]["occurrence_id"],),
        )
        service.close()
        service = self.make_service(
            FixtureDoseRuleProvider({"TEST_DRUG_A": {"max_single_dose": 1}}, "fixture-v1")
        )
        service.check_plan_safety(active["plan_id"], 1, self.local_now + timedelta(minutes=1))
        first = service.get_occurrence(rows[0]["occurrence_id"])
        second = service.get_occurrence(rows[1]["occurrence_id"])
        self.assertEqual(first["intake_status"], "confirmed_taken")
        self.assertEqual(second["intake_status"], "closed_unconfirmed")

    def test_scheduler_stale_block_cancels_future_and_never_requests_device(self):
        service = self.make_service(EmptySafetyRuleProvider("fixture-v1"))
        active = self.approve(service, self.draft(service))
        service.close()
        service = self.make_service(
            FixtureDoseRuleProvider({"TEST_DRUG_A": {"max_single_dose": 1}}, "fixture-v1")
        )
        result = service.run_scheduler_cycle(self.local_now + timedelta(minutes=1))
        self.assertEqual(result["reminders_claimed"], [])
        self.assertGreater(
            service.storage.fetchone(
                "SELECT COUNT(*) AS n FROM medication_occurrence WHERE intake_status='cancelled'"
            )["n"],
            0,
        )
        self.assertEqual(len(service.list_event_log("device.interaction.request")), 0)

    def test_scheduler_next_cycle_does_not_repeat_block_event(self):
        service = self.make_service(EmptySafetyRuleProvider("fixture-v1"))
        active = self.approve(service, self.draft(service))
        service.close()
        service = self.make_service(
            FixtureDoseRuleProvider({"TEST_DRUG_A": {"max_single_dose": 1}}, "fixture-v1")
        )
        service.run_scheduler_cycle(self.local_now + timedelta(minutes=1))
        before = len(service.list_event_log("medication.safety.blocked"))
        service.run_scheduler_cycle(self.local_now + timedelta(minutes=2))
        self.assertEqual(len(service.list_event_log("medication.safety.blocked")), before)
        self.assertEqual(len(service.list_event_log("medication.safety.future_occurrences_cancelled")), 1)

    def test_safety_block_cancellation_rolls_back_with_event_failure(self):
        service = self.make_service(EmptySafetyRuleProvider("fixture-v1"))
        active = self.approve(service, self.draft(service))
        service.close()
        service = self.make_service(
            FixtureDoseRuleProvider({"TEST_DRUG_A": {"max_single_dose": 1}}, "fixture-v1")
        )
        original = service._audit_and_enqueue

        def fail_cancellation(connection, event, dedup_key=None):
            if event["event_type"] == "medication.safety.future_occurrences_cancelled":
                raise RuntimeError("fixture event log failure")
            return original(connection, event, dedup_key)

        service._audit_and_enqueue = fail_cancellation
        with self.assertRaises(RuntimeError):
            service.check_plan_safety(active["plan_id"], 1, self.local_now + timedelta(minutes=1))
        latest = service.get_latest_safety_check(active["plan_id"], 1)
        self.assertEqual(latest["status"], "PASS")
        self.assertEqual(
            service.storage.fetchone(
                "SELECT COUNT(*) AS n FROM medication_occurrence WHERE intake_status='cancelled'"
            )["n"],
            0,
        )

    def test_empty_provider_pass_is_partial_not_clinically_complete(self):
        result = SafetyEngine(EmptySafetyRuleProvider("empty-v1")).check({
            "plan_id": "p-empty", "version": 1, "elder_id": "E001",
            "drug_name": "TEST_DRUG_A", "dosage_text": "5mg",
            "schedule_time": "08:00", "start_date": "2026-09-20",
        }).to_dict()
        self.assertEqual(result["status"], "PASS")
        self.assertFalse(result["coverage_complete"])
        self.assertEqual(result["coverage_status"], "partial")

    def test_full_fixture_coverage_is_complete(self):
        result = SafetyEngine(
            rule_provider=FullCoverageProvider(),
            dose_provider=FullCoverageProvider(),
            ddi_provider=FullCoverageProvider(),
            allergy_provider=FullCoverageProvider(),
            contraindication_provider=FullCoverageProvider(),
            ruleset_version="full-fixture-v1",
        ).check({
            "plan_id": "p-full", "version": 1, "elder_id": "E001",
            "drug_name": "TEST_DRUG_A", "dosage_text": "5mg",
            "schedule_time": "08:00", "start_date": "2026-09-20",
        }).to_dict()
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["coverage_complete"])
        self.assertEqual(result["coverage_status"], "complete")

    def test_safety_api_returns_fingerprint_and_coverage_summary(self):
        service = self.make_service(EmptySafetyRuleProvider("empty-v1"))
        active = self.approve(service, self.draft(service))
        status, payload = Application(service).handle(
            "GET", "/api/v1/medication/plans/%s/safety" % active["plan_id"]
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ruleset_fingerprint"])
        self.assertFalse(payload["coverage_complete"])
        self.assertEqual(payload["coverage_status"], "partial")

    def test_safety_web_copy_separates_basic_pass_from_clinical_coverage(self):
        web = Path(__file__).parents[1] / "web" / "assets" / "app.js"
        content = web.read_text(encoding="utf-8")
        self.assertIn("基础安全检查通过不等于临床安全已确认", content)
        self.assertIn("临床覆盖不完整", content)


if __name__ == "__main__":
    unittest.main()
