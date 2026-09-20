import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from medication_reminder.http import Application
from medication_reminder.safety import (
    EmptySafetyRuleProvider,
    FixtureDDIProvider,
    FixtureDoseRuleProvider,
    FixtureSafetyRuleProvider,
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


if __name__ == "__main__":
    unittest.main()
