"""Deterministic medication safety checks and safety freeze primitives.

This module intentionally contains no clinical knowledge.  A production
installation starts with an empty rule provider and therefore only evaluates
the structural shape of a plan.  Dose and interaction findings are emitted
only when an explicit rule pack/provider supplies the corresponding rule.

The safety engine is deliberately independent from the semantic agent.  It
accepts a plain plan snapshot and returns a reproducible result that can be
persisted and audited by :mod:`medication_reminder.service`.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import itertools
import json
import os
from pathlib import Path
import re


UTC = timezone.utc

STATUS_PASS = "PASS"
STATUS_WARN = "WARN"
STATUS_BLOCK = "BLOCK"
STATUS_CHECK_FAILED = "CHECK_FAILED"

SEVERITY_INFO = "INFO"
SEVERITY_WARN = "WARN"
SEVERITY_BLOCK = "BLOCK"

COVERAGE_CHECKED = "checked"
COVERAGE_NOT_CONFIGURED = "not_configured"
COVERAGE_NO_CONTEXT = "no_context"
COVERAGE_FAILED = "failed"

_SEVERITY_RANK = {
    SEVERITY_INFO: 0,
    SEVERITY_WARN: 1,
    SEVERITY_BLOCK: 2,
}


class SafetyProviderError(Exception):
    """A configured safety provider failed while checking a plan."""


class SafetyProviderNotConfigured(Exception):
    """Raised internally when a provider has no configured rule source."""


def _utc_now():
    return datetime.now(UTC)


def _iso(value):
    if value is None:
        value = _utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical(value):
    """Return JSON-compatible canonical data for deterministic hashing."""

    if isinstance(value, dict):
        return {
            str(key): _canonical(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, set):
        return sorted((_canonical(item) for item in value), key=lambda item: _json(item))
    return value


def _sha256_json(value):
    return hashlib.sha256(_json(_canonical(value)).encode("utf-8")).hexdigest()


def _provider_class_name(provider):
    cls = type(provider)
    return "%s.%s" % (cls.__module__, cls.__name__)


def _provider_fingerprint(provider):
    """Read a provider fingerprint without requiring a concrete provider type."""

    if provider is None:
        return _sha256_json({"provider_name": "none", "ruleset_version": "none"})
    supplied = getattr(provider, "ruleset_fingerprint", None)
    if supplied is not None:
        value = supplied() if callable(supplied) else supplied
        if value:
            return str(value)
    material = getattr(provider, "fingerprint_material", None)
    material = material() if callable(material) else None
    if material is None:
        material = {
            "provider_name": getattr(provider, "provider_name", None) or _provider_class_name(provider),
            "ruleset_version": str(getattr(provider, "ruleset_version", "unknown")),
        }
    return _sha256_json(material)


def _combined_provider_fingerprint(version, providers):
    return _sha256_json({
        "ruleset_version": str(version),
        "providers": {
            role: _provider_fingerprint(provider)
            for role, provider in sorted(providers.items())
        },
    })


def _coverage_summary(coverage):
    """Summarize coverage separately from PASS/WARN/BLOCK findings."""

    coverage = dict(coverage or {})
    domains = ("structural", "dose_rules", "ddi", "allergy", "contraindication")
    values = [str(coverage.get(domain) or "unknown") for domain in domains]
    if COVERAGE_FAILED in values or any(
        str(value) == COVERAGE_FAILED for value in coverage.values()
    ):
        status = "failed"
    elif all(value == COVERAGE_CHECKED for value in values):
        status = "complete"
    else:
        status = "partial"
    return {
        "coverage_complete": status == "complete",
        "coverage_status": status,
    }


def _truthy(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("", "0", "false", "no", "off", "none")


def _severity(value, default=SEVERITY_BLOCK):
    value = str(value or default).strip().upper()
    if value not in _SEVERITY_RANK:
        raise ValueError("unsupported safety severity: %s" % value)
    return value


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _finding_id(category, code, evidence, rule_id=None, index=0):
    material = "%s|%s|%s|%s|%s" % (
        category, code, _json(evidence or {}), rule_id or "", index,
    )
    return "finding_%s" % hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _plan_medications(plan):
    """Return normalized medication entries without inventing medication data.

    The current MVP stores one medication directly on ``medication_plan``.
    The optional ``medications``/``medications_json`` shapes are accepted by
    the engine so a future multi-medication plan can use the same safety
    contract without changing the engine state machine.
    """

    if isinstance(plan, dict):
        raw = plan.get("medications")
        if raw is None and plan.get("medications_json"):
            try:
                raw = json.loads(plan["medications_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                raw = None
        if raw is not None:
            if isinstance(raw, dict):
                raw = [raw]
            if isinstance(raw, list):
                return [dict(item) if isinstance(item, dict) else {"name": item} for item in raw]

        # Preserve an explicitly supplied medication-shaped entry, including
        # empty values, so M2 can report the structural finding at approval.
        if any(key in plan for key in (
            "drug_name", "medication_name", "dosage_text", "dose_value", "dose_unit",
        )):
            return [{
                "name": plan.get("drug_name", plan.get("medication_name")),
                "drug_name": plan.get("drug_name", plan.get("medication_name")),
                "dosage_text": plan.get("dosage_text"),
                "dose": plan.get("dose"),
                "dose_value": plan.get("dose_value"),
                "dose_unit": plan.get("dose_unit"),
                "value": plan.get("value"),
                "unit": plan.get("unit"),
            }]
    return []


def _medication_name(entry):
    value = entry.get("name", entry.get("drug_name", entry.get("medication_name")))
    return str(value).strip() if value is not None else ""


def _dose_text(entry):
    for key in ("dosage_text", "dose", "dosage"):
        if entry.get(key) is not None:
            return str(entry.get(key)).strip()
    return ""


def parse_dose(entry):
    """Parse an explicitly supplied numeric dose and unit.

    This is syntax validation only.  It does not interpret whether a unit is
    clinically appropriate for a medication.
    """

    value = entry.get("dose_value", entry.get("value"))
    unit = entry.get("dose_unit", entry.get("unit"))
    numeric = _safe_float(value) if value is not None and str(value).strip() else None
    if numeric is not None:
        return numeric, str(unit).strip() if unit is not None else ""
    text = _dose_text(entry)
    match = re.fullmatch(r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*([^\d\s].*?)\s*", text)
    if not match:
        return None, str(unit).strip() if unit is not None else ""
    return _safe_float(match.group(1)), (str(unit).strip() if unit is not None else match.group(2).strip())


def _frequency_per_day(plan, entry):
    for source in (entry, plan):
        for key in ("frequency_per_day", "times_per_day", "daily_frequency"):
            if source.get(key) is not None:
                value = _safe_float(source.get(key))
                if value is not None and value > 0:
                    return value
    return 1.0


@dataclass(frozen=True)
class SafetyFinding:
    category: str
    severity: str
    code: str
    message: str
    evidence: dict = field(default_factory=dict)
    rule_id: str = None
    rule_version: str = None
    finding_id: str = None

    def __post_init__(self):
        severity = _severity(self.severity)
        object.__setattr__(self, "severity", severity)
        evidence = dict(self.evidence or {})
        object.__setattr__(self, "evidence", evidence)
        if not self.finding_id:
            object.__setattr__(
                self,
                "finding_id",
                _finding_id(self.category, self.code, evidence, self.rule_id),
            )

    def to_dict(self):
        return {
            "finding_id": self.finding_id,
            "category": self.category,
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "evidence": dict(self.evidence),
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
        }


@dataclass
class SafetyCheckResult:
    plan_id: str
    plan_version: int
    status: str
    ruleset_version: str
    checked_at: str
    ruleset_fingerprint: str = None
    findings: list = field(default_factory=list)
    coverage: dict = field(default_factory=dict)
    trace_id: str = None
    check_id: str = None
    check_failed: bool = False
    error: str = None

    def __post_init__(self):
        if self.status not in (STATUS_PASS, STATUS_WARN, STATUS_BLOCK, STATUS_CHECK_FAILED):
            raise ValueError("unsupported safety check status: %s" % self.status)
        self.findings = [
            item if isinstance(item, SafetyFinding) else SafetyFinding(**item)
            for item in (self.findings or [])
        ]
        self.coverage = dict(self.coverage or {})

    def to_dict(self):
        result = {
            "check_id": self.check_id,
            "plan_id": self.plan_id,
            "plan_version": int(self.plan_version),
            "status": self.status,
            "ruleset_version": self.ruleset_version,
            "ruleset_fingerprint": self.ruleset_fingerprint,
            "checked_at": self.checked_at,
            "findings": [finding.to_dict() for finding in self.findings],
            "coverage": dict(self.coverage),
            "trace_id": self.trace_id,
        }
        result.update(_coverage_summary(self.coverage))
        if self.check_failed or self.status == STATUS_CHECK_FAILED:
            result["check_failed"] = True
        if self.error:
            result["error"] = self.error
        return result


@dataclass
class ProviderCheck:
    findings: list = field(default_factory=list)
    coverage: str = COVERAGE_NOT_CONFIGURED


class SafetyRuleProvider:
    """Provider contract used by the deterministic M2 engine.

    Implementations may be backed by a local JSON rule pack, a future
    pharmacy database, or a test fixture.  The engine does not depend on any
    particular SDK.  A provider may alternatively override ``check(plan)``
    as one combined deterministic contract.
    """

    configured = False
    ruleset_version = "empty-v1"
    provider_name = None

    def fingerprint_material(self):
        return {
            "provider_name": self.provider_name or _provider_class_name(self),
            "ruleset_version": str(getattr(self, "ruleset_version", "unknown")),
        }

    @property
    def ruleset_fingerprint(self):
        return _sha256_json(self.fingerprint_material())

    def check(self, plan):
        return None

    def check_dose(self, plan, medication):
        return ProviderCheck(coverage=COVERAGE_NOT_CONFIGURED)

    def check_ddi(self, plan, medications):
        return ProviderCheck(coverage=COVERAGE_NOT_CONFIGURED)

    def check_allergy(self, plan, medications):
        return ProviderCheck(coverage=COVERAGE_NOT_CONFIGURED)

    def check_contraindication(self, plan, medications):
        return ProviderCheck(coverage=COVERAGE_NOT_CONFIGURED)


class DoseRuleProvider(SafetyRuleProvider):
    """Narrow interface seam for explicit dose rule sources."""


class DDIProvider(SafetyRuleProvider):
    """Narrow interface seam for explicit pairwise interaction sources."""


class EmptySafetyRuleProvider(SafetyRuleProvider):
    """Production-safe default: no clinical rules are claimed as checked."""

    configured = False
    provider_name = "empty"

    def __init__(self, ruleset_version="empty-v1"):
        self.ruleset_version = str(ruleset_version or "empty-v1")


class DisabledSafetyRuleProvider(EmptySafetyRuleProvider):
    pass


class EmptyDDIProvider(EmptySafetyRuleProvider):
    pass


class DisabledDDIProvider(EmptyDDIProvider):
    pass


class AllergyProvider(SafetyRuleProvider):
    """Interface seam for a future patient allergy context provider."""

    def check_allergy(self, plan, medications):
        return ProviderCheck(coverage=COVERAGE_NO_CONTEXT)


class ContraindicationProvider(SafetyRuleProvider):
    """Interface seam for a future patient contraindication provider."""

    def check_contraindication(self, plan, medications):
        return ProviderCheck(coverage=COVERAGE_NO_CONTEXT)


def _normalize_pair(left, right):
    return tuple(sorted((str(left), str(right))))


class FixtureDoseRuleProvider(DoseRuleProvider):
    """Explicit fictional dose rules used by tests and local demonstrations."""

    configured = True
    provider_name = "fixture_dose"

    def __init__(self, rules=None, ruleset_version="fixture-rules-v1"):
        self.rules = dict(rules or {})
        self.ruleset_version = str(ruleset_version)

    def fingerprint_material(self):
        return {
            "provider_name": self.provider_name,
            "ruleset_version": self.ruleset_version,
            "dose_rules": self.rules,
        }

    def check_dose(self, plan, medication):
        name = _medication_name(medication)
        rule = self.rules.get(name)
        if rule is None:
            return ProviderCheck(coverage=COVERAGE_NOT_CONFIGURED)
        if not isinstance(rule, dict):
            raise SafetyProviderError("dose rule for %s is not an object" % name)
        value, unit = parse_dose(medication)
        if value is None:
            return ProviderCheck(coverage=COVERAGE_CHECKED)
        findings = []
        rule_id = str(rule.get("rule_id") or "dose:%s" % name)
        rule_version = str(rule.get("rule_version") or self.ruleset_version)
        expected_unit = str(rule.get("unit") or "").strip()
        evidence_base = {
            "medication": name,
            "dose_value": value,
            "dose_unit": unit,
        }
        if expected_unit and unit and expected_unit != unit:
            findings.append(SafetyFinding(
                category="dose",
                severity=SEVERITY_BLOCK,
                code="DOSE_UNIT_MISMATCH",
                message="explicit dose rule unit does not match the plan dose unit",
                evidence=dict(evidence_base, expected_unit=expected_unit),
                rule_id=rule_id,
                rule_version=rule_version,
            ))
            return ProviderCheck(findings=findings, coverage=COVERAGE_CHECKED)
        max_single = _safe_float(rule.get("max_single_dose"))
        if max_single is not None and value > max_single:
            findings.append(SafetyFinding(
                category="dose",
                severity=_severity(rule.get("max_single_severity"), SEVERITY_BLOCK),
                code=str(rule.get("max_single_code") or "DOSE_MAX_SINGLE_EXCEEDED"),
                message=str(rule.get("max_single_message") or "explicit maximum single dose exceeded"),
                evidence=dict(evidence_base, maximum_single_dose=max_single),
                rule_id=rule_id,
                rule_version=rule_version,
            ))
        max_daily = _safe_float(rule.get("max_daily_dose"))
        daily_value = value * _frequency_per_day(plan, medication)
        if max_daily is not None and daily_value > max_daily:
            findings.append(SafetyFinding(
                category="dose",
                severity=_severity(rule.get("max_daily_severity"), SEVERITY_BLOCK),
                code=str(rule.get("max_daily_code") or "DOSE_MAX_DAILY_EXCEEDED"),
                message=str(rule.get("max_daily_message") or "explicit maximum daily dose exceeded"),
                evidence=dict(evidence_base, daily_dose=daily_value, maximum_daily_dose=max_daily),
                rule_id=rule_id,
                rule_version=rule_version,
            ))
        min_single = _safe_float(rule.get("min_single_dose"))
        if min_single is not None and value < min_single:
            findings.append(SafetyFinding(
                category="dose",
                severity=_severity(rule.get("min_single_severity"), SEVERITY_WARN),
                code=str(rule.get("min_single_code") or "DOSE_MIN_SINGLE_NOT_MET"),
                message=str(rule.get("min_single_message") or "explicit minimum single dose not met"),
                evidence=dict(evidence_base, minimum_single_dose=min_single),
                rule_id=rule_id,
                rule_version=rule_version,
            ))
        return ProviderCheck(findings=findings, coverage=COVERAGE_CHECKED)


class FixtureDDIProvider(DDIProvider):
    """Pairwise fictional interaction provider for architecture tests."""

    configured = True
    provider_name = "fixture_ddi"

    def __init__(self, rules=None, ruleset_version="fixture-rules-v1"):
        self.ruleset_version = str(ruleset_version)
        self.rules = {}
        for key, rule in (rules or {}).items() if isinstance(rules, dict) else []:
            if isinstance(key, str) and "+" in key:
                pair = tuple(part.strip() for part in key.split("+", 1))
            elif isinstance(key, (tuple, list)) and len(key) == 2:
                pair = tuple(key)
            else:
                continue
            self.rules[_normalize_pair(pair[0], pair[1])] = dict(rule or {})
        if isinstance(rules, list):
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                drugs = rule.get("drugs") or rule.get("medications") or rule.get("pair")
                if isinstance(drugs, (list, tuple)) and len(drugs) == 2:
                    self.rules[_normalize_pair(drugs[0], drugs[1])] = dict(rule)

    def check_ddi(self, plan, medications):
        findings = []
        matched = False
        for left, right in itertools.combinations(medications, 2):
            pair = _normalize_pair(_medication_name(left), _medication_name(right))
            rule = self.rules.get(pair)
            if rule is None:
                continue
            matched = True
            severity = _severity(rule.get("severity"), SEVERITY_WARN)
            findings.append(SafetyFinding(
                category="ddi",
                severity=severity,
                code=str(rule.get("code") or "DDI_EXPLICIT_RULE_MATCH"),
                message=str(rule.get("message") or "explicit fixture interaction rule matched"),
                evidence={
                    "medications": [pair[0], pair[1]],
                    "rule": dict(rule),
                },
                rule_id=str(rule.get("rule_id") or "ddi:%s+%s" % pair),
                rule_version=str(rule.get("rule_version") or self.ruleset_version),
            ))
        return ProviderCheck(
            findings=findings,
            coverage=COVERAGE_CHECKED if matched or self.rules else COVERAGE_NOT_CONFIGURED,
        )

    def fingerprint_material(self):
        rules = [
            {"pair": list(pair), "rule": rule}
            for pair, rule in sorted(self.rules.items(), key=lambda item: str(item[0]))
        ]
        return {
            "provider_name": self.provider_name,
            "ruleset_version": self.ruleset_version,
            "ddi_rules": rules,
        }


class FixtureSafetyRuleProvider(FixtureDoseRuleProvider):
    """Combined fixture provider convenient for service-level tests."""

    provider_name = "fixture"

    def __init__(self, dose_rules=None, ddi_rules=None, ruleset_version="fixture-rules-v1"):
        super().__init__(dose_rules, ruleset_version)
        self.ddi_provider = FixtureDDIProvider(ddi_rules, ruleset_version)

    def fingerprint_material(self):
        return {
            "provider_name": self.provider_name,
            "ruleset_version": self.ruleset_version,
            "dose_rules": self.rules,
            "ddi_rules": self.ddi_provider.fingerprint_material()["ddi_rules"],
        }

    def check_ddi(self, plan, medications):
        return self.ddi_provider.check_ddi(plan, medications)


class JsonSafetyRuleProvider(FixtureSafetyRuleProvider):
    """Load only explicitly supplied local rule-pack data."""

    provider_name = "json_rule_pack"

    def __init__(self, rule_pack, ruleset_version=None):
        if isinstance(rule_pack, (str, os.PathLike)):
            path = Path(rule_pack)
            with path.open("r", encoding="utf-8") as handle:
                rule_pack = json.load(handle)
        if not isinstance(rule_pack, dict):
            raise SafetyProviderError("safety rule pack must be a JSON object")
        version = ruleset_version or rule_pack.get("ruleset_version") or "rule-pack-v1"
        super().__init__(
            rule_pack.get("dose_rules") or rule_pack.get("dose") or {},
            rule_pack.get("ddi_rules") or rule_pack.get("ddi") or [],
            version,
        )


def load_rule_pack(value):
    if isinstance(value, dict):
        return value
    if value:
        path = Path(str(value))
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    return None


def build_safety_rule_provider(config=None):
    config = config or {}
    supplied = config.get("safety_rule_provider") or config.get("safety_provider") or config.get("rule_provider")
    if supplied is not None and not isinstance(supplied, str):
        return supplied
    provider_name = str(
        supplied or os.environ.get("SAFETY_RULE_PROVIDER", "empty")
    ).strip().lower()
    version = str(
        config.get("safety_ruleset_version")
        or config.get("ruleset_version")
        or os.environ.get("SAFETY_RULESET_VERSION", "empty-v1")
    )
    if provider_name in ("", "empty", "disabled", "none"):
        return EmptySafetyRuleProvider(version)
    if provider_name in ("fixture", "test", "fixture_safety"):
        pack = load_rule_pack(config.get("safety_rule_pack") or config.get("safety_rule_file")) or {}
        return FixtureSafetyRuleProvider(
            pack.get("dose_rules") or {}, pack.get("ddi_rules") or [],
            pack.get("ruleset_version") or version,
        )
    if provider_name in ("json", "rule_pack", "local"):
        pack = load_rule_pack(config.get("safety_rule_pack") or config.get("safety_rule_file"))
        if pack is None:
            raise SafetyProviderError("SAFETY_RULE_FILE is required for provider=%s" % provider_name)
        # A pack carries its own immutable version.  An explicit
        # ruleset_version override may still be supplied under the dedicated
        # config key, but the service default must not mask the pack version.
        return JsonSafetyRuleProvider(pack, config.get("ruleset_version"))
    raise SafetyProviderError("unsupported SAFETY_RULE_PROVIDER: %s" % provider_name)


def _provider_check(provider, method, *args):
    if provider is None:
        return ProviderCheck(coverage=COVERAGE_NOT_CONFIGURED)
    function = getattr(provider, method, None)
    if function is None:
        return ProviderCheck(coverage=COVERAGE_NOT_CONFIGURED)
    value = function(*args)
    if value is None:
        return ProviderCheck(coverage=COVERAGE_CHECKED)
    if isinstance(value, ProviderCheck):
        return value
    if isinstance(value, dict):
        return ProviderCheck(
            findings=value.get("findings") or [],
            coverage=value.get("coverage", COVERAGE_CHECKED),
        )
    if isinstance(value, (list, tuple)):
        return ProviderCheck(findings=list(value), coverage=COVERAGE_CHECKED)
    raise SafetyProviderError("provider returned unsupported check result")


class SafetyEngine:
    """Pure, deterministic M2 checker."""

    def __init__(self, rule_provider=None, dose_provider=None, ddi_provider=None,
                 allergy_provider=None, contraindication_provider=None,
                 ruleset_version=None, ruleset_fingerprint=None):
        self.rule_provider = rule_provider or EmptySafetyRuleProvider(
            ruleset_version or "empty-v1"
        )
        self.dose_provider = dose_provider or self.rule_provider
        self.ddi_provider = ddi_provider or self.rule_provider
        self.allergy_provider = allergy_provider
        self.contraindication_provider = contraindication_provider
        self.ruleset_version = str(
            ruleset_version or getattr(self.rule_provider, "ruleset_version", None)
            or "empty-v1"
        )
        self.provider_name = str(
            getattr(self.rule_provider, "provider_name", None)
            or _provider_class_name(self.rule_provider)
        )
        self.ruleset_fingerprint = str(ruleset_fingerprint or _combined_provider_fingerprint(
            self.ruleset_version,
            {
                "rule": self.rule_provider,
                "dose": self.dose_provider,
                "ddi": self.ddi_provider,
                "allergy": self.allergy_provider,
                "contraindication": self.contraindication_provider,
            },
        ))

    def check(self, plan, checked_at=None, trace_id=None):
        plan = dict(plan or {})
        plan_id = str(plan.get("plan_id") or "unknown")
        try:
            plan_version = int(plan.get("version", plan.get("plan_version", 1)))
        except (TypeError, ValueError):
            plan_version = 1
        checked = _iso(checked_at)
        trace = trace_id or "safety:%s:v%s" % (plan_id, plan_version)
        findings = []
        coverage = {
            "structural": COVERAGE_CHECKED,
            "dose_rules": COVERAGE_NOT_CONFIGURED,
            "ddi": COVERAGE_NOT_CONFIGURED,
            "allergy": COVERAGE_NOT_CONFIGURED,
            "contraindication": COVERAGE_NOT_CONFIGURED,
        }
        try:
            medications = _plan_medications(plan)
            findings.extend(self._structural_findings(plan, medications))
            custom_check = getattr(type(self.rule_provider), "check", None)
            if custom_check is not None and custom_check is not SafetyRuleProvider.check:
                combined = _provider_check(self.rule_provider, "check", plan)
                findings.extend(self._coerce_findings(combined.findings))
                coverage["rule_provider"] = combined.coverage
            dose_coverages = []
            for medication in medications:
                result = _provider_check(self.dose_provider, "check_dose", plan, medication)
                findings.extend(self._coerce_findings(result.findings))
                dose_coverages.append(result.coverage)
            if dose_coverages:
                coverage["dose_rules"] = self._merge_coverage(dose_coverages)

            ddi_result = _provider_check(self.ddi_provider, "check_ddi", plan, medications)
            findings.extend(self._coerce_findings(ddi_result.findings))
            coverage["ddi"] = ddi_result.coverage

            allergy_result = _provider_check(
                self.allergy_provider, "check_allergy", plan, medications
            )
            findings.extend(self._coerce_findings(allergy_result.findings))
            coverage["allergy"] = allergy_result.coverage

            contraindication_result = _provider_check(
                self.contraindication_provider,
                "check_contraindication",
                plan,
                medications,
            )
            findings.extend(self._coerce_findings(contraindication_result.findings))
            coverage["contraindication"] = contraindication_result.coverage

            status = self._aggregate(findings)
            return SafetyCheckResult(
                plan_id=plan_id,
                plan_version=plan_version,
                status=status,
                ruleset_version=self.ruleset_version,
                ruleset_fingerprint=self.ruleset_fingerprint,
                checked_at=checked,
                findings=findings,
                coverage=coverage,
                trace_id=trace,
            )
        except Exception as exc:  # fail closed; never turn provider failure into PASS
            failure = SafetyFinding(
                category="system",
                severity=SEVERITY_BLOCK,
                code="SAFETY_CHECK_FAILED",
                message="safety check failed; plan cannot enter scheduling",
                evidence={"error_type": type(exc).__name__},
                rule_id="m2.engine",
                rule_version=self.ruleset_version,
            )
            return SafetyCheckResult(
                plan_id=plan_id,
                plan_version=plan_version,
                status=STATUS_BLOCK,
                ruleset_version=self.ruleset_version,
                ruleset_fingerprint=self.ruleset_fingerprint,
                checked_at=checked,
                findings=findings + [failure],
                coverage=dict(coverage, safety_engine=COVERAGE_FAILED),
                trace_id=trace,
                check_failed=True,
                error="%s: %s" % (type(exc).__name__, str(exc)),
            )

    @staticmethod
    def _merge_coverage(values):
        values = [value for value in values if value]
        if not values:
            return COVERAGE_NOT_CONFIGURED
        if COVERAGE_FAILED in values:
            return COVERAGE_FAILED
        if COVERAGE_CHECKED in values:
            return COVERAGE_CHECKED
        if COVERAGE_NO_CONTEXT in values:
            return COVERAGE_NO_CONTEXT
        return COVERAGE_NOT_CONFIGURED

    @staticmethod
    def _coerce_findings(items):
        findings = []
        for item in items or []:
            if isinstance(item, SafetyFinding):
                findings.append(item)
            elif isinstance(item, dict):
                findings.append(SafetyFinding(**item))
            else:
                raise SafetyProviderError("provider returned unsupported finding")
        return findings

    @staticmethod
    def _aggregate(findings):
        if any(item.severity == SEVERITY_BLOCK for item in findings):
            return STATUS_BLOCK
        if any(item.severity == SEVERITY_WARN for item in findings):
            return STATUS_WARN
        return STATUS_PASS

    def _structural_findings(self, plan, medications):
        findings = []
        if not medications:
            findings.append(SafetyFinding(
                category="structural",
                severity=SEVERITY_BLOCK,
                code="PLAN_MEDICATION_REQUIRED",
                message="plan must contain at least one medication",
                evidence={},
                rule_id="m2.structural.medication",
                rule_version=self.ruleset_version,
            ))
        seen = set()
        for index, medication in enumerate(medications):
            name = _medication_name(medication)
            if not name:
                findings.append(SafetyFinding(
                    category="structural",
                    severity=SEVERITY_BLOCK,
                    code="MEDICATION_NAME_REQUIRED",
                    message="medication name is required",
                    evidence={"index": index},
                    rule_id="m2.structural.medication_name",
                    rule_version=self.ruleset_version,
                ))
            value, unit = parse_dose(medication)
            dose_text = _dose_text(medication)
            if value is None:
                findings.append(SafetyFinding(
                    category="structural",
                    severity=SEVERITY_BLOCK,
                    code="DOSE_VALUE_REQUIRED",
                    message="dose must contain a numeric value",
                    evidence={"index": index, "dose": dose_text},
                    rule_id="m2.structural.dose_value",
                    rule_version=self.ruleset_version,
                ))
            elif value <= 0:
                findings.append(SafetyFinding(
                    category="structural",
                    severity=SEVERITY_BLOCK,
                    code="DOSE_VALUE_MUST_BE_POSITIVE",
                    message="dose value must be greater than zero",
                    evidence={"index": index, "dose_value": value, "dose_unit": unit},
                    rule_id="m2.structural.dose_positive",
                    rule_version=self.ruleset_version,
                ))
            if not unit:
                findings.append(SafetyFinding(
                    category="structural",
                    severity=SEVERITY_BLOCK,
                    code="DOSE_UNIT_REQUIRED",
                    message="dose unit is required",
                    evidence={"index": index, "dose": dose_text},
                    rule_id="m2.structural.dose_unit",
                    rule_version=self.ruleset_version,
                ))
            canonical = (
                name,
                value,
                unit,
                dose_text,
                str(medication.get("route") or "").strip(),
                str(medication.get("schedule_time") or plan.get("schedule_time") or "").strip(),
            )
            if canonical in seen:
                findings.append(SafetyFinding(
                    category="structural",
                    severity=SEVERITY_BLOCK,
                    code="DUPLICATE_MEDICATION_ENTRY",
                    message="plan contains a duplicate medication entry",
                    evidence={"index": index, "medication": name},
                    rule_id="m2.structural.duplicate_medication",
                    rule_version=self.ruleset_version,
                ))
            seen.add(canonical)

        schedule_type = str(plan.get("schedule_type") or "").upper()
        # Phase 4 validates advanced schedule structure before M2.  The old
        # schedule_time field remains the compatibility check for fixed-time
        # plans, but a meal/interval/PRN plan must not be blocked merely
        # because it has no single legacy clock value.
        requires_legacy_time = schedule_type in (
            "", "DAILY", "FIXED_TIME", "WEEKLY", "CYCLE"
        )
        schedule_time = plan.get("schedule_time")
        if requires_legacy_time and not re.fullmatch(
            r"(?:[01]\d|2[0-3]):[0-5]\d", str(schedule_time or "")
        ):
            findings.append(SafetyFinding(
                category="structural",
                severity=SEVERITY_BLOCK,
                code="SCHEDULE_TIME_INVALID",
                message="schedule_time must be a valid HH:MM time",
                evidence={"schedule_time": schedule_time},
                rule_id="m2.structural.schedule_time",
                rule_version=self.ruleset_version,
            ))
        for field_name in ("elder_id", "start_date"):
            if not str(plan.get(field_name) or "").strip():
                findings.append(SafetyFinding(
                    category="structural",
                    severity=SEVERITY_BLOCK,
                    code="PLAN_%s_REQUIRED" % field_name.upper(),
                    message="%s is required" % field_name,
                    evidence={"field": field_name},
                    rule_id="m2.structural.%s" % field_name,
                    rule_version=self.ruleset_version,
                ))
        return findings


class SafetyFreezeError(Exception):
    """A protected M2 mutation was requested."""


class SafetyFreezeService:
    """Hard-coded guardrails for safety-sensitive configuration/mutations."""

    PROTECTED_KEYS = frozenset({
        "m2.check.disable",
        "m2.block.override",
        "m2.severity.relax",
        "plan.medication.dose",
        "plan.medication.identity",
    })
    PLAN_LIFECYCLE_SENSITIVE_FIELDS = frozenset({
        "elder_id", "drug_name", "medication_name", "dosage_text", "dose",
        "dose_value", "dose_unit", "frequency", "frequency_per_day",
        "times_per_day", "schedule_type", "schedule_time", "timezone",
        "route", "relation_to_meal", "instruction", "start_date", "end_date",
        "schedule_config", "schedule_config_json",
        "device_sn", "confirmation_window_minutes", "max_snooze_count",
    })

    def assert_safety_enabled(self, enabled):
        if not _truthy(enabled):
            raise SafetyFreezeError("m2.check.disable is frozen; M2 cannot be disabled")

    def assert_change_allowed(self, key, old=None, new=None, source="system",
                              from_severity=None, to_severity=None):
        key = str(key or "")
        if key == "m2.check.disable" and _truthy(new):
            raise SafetyFreezeError("m2.check.disable is frozen")
        if key == "m2.block.override" and _truthy(new):
            raise SafetyFreezeError("m2.block.override is frozen")
        if key == "m2.severity.relax":
            before = from_severity or old
            after = to_severity or new
            if before is not None and after is not None:
                if _SEVERITY_RANK.get(str(after).upper(), -1) < _SEVERITY_RANK.get(str(before).upper(), -1):
                    raise SafetyFreezeError("m2.severity.relax is frozen")
            elif str(after or "").upper() in ("PASS", "WARN"):
                raise SafetyFreezeError("m2.severity.relax is frozen")
        if key in ("plan.medication.dose", "plan.medication.identity"):
            source_text = str(source or "").lower()
            if any(marker in source_text for marker in ("llm", "agent", "harness", "model")):
                raise SafetyFreezeError("%s cannot be changed by an agent" % key)
        return True

    def assert_plan_write_allowed(self, current_status, changes=None, operation="update"):
        """Reject direct active writes and direct status promotion.

        Draft creation and ``revise_plan`` remain allowed; they create a new
        draft that must still pass Submit -> Safety -> Approve.
        """

        changes = dict(changes or {})
        if str(changes.get("status") or "").lower() == "active":
            raise SafetyFreezeError(
                "direct activation is frozen; use Submit -> Safety -> Approve"
            )
        if str(current_status or "").lower() == "active":
            changed = self.PLAN_LIFECYCLE_SENSITIVE_FIELDS.intersection(changes)
            if changed:
                raise SafetyFreezeError(
                    "active plan changes require revise_plan: %s"
                    % ",".join(sorted(changed))
                )
        return True

    # Short aliases make the freeze easy to use from adapters and tests.
    assert_mutation_allowed = assert_change_allowed
    guard = assert_change_allowed

    def reject_override(self, *args, **kwargs):
        return self.assert_change_allowed("m2.block.override", new=True)


def build_safety_engine(config=None):
    config = config or {}
    provider = build_safety_rule_provider(config)
    supplied_provider = (
        config.get("safety_rule_provider")
        or config.get("safety_provider")
        or config.get("rule_provider")
    )
    if not config.get("ruleset_version") and getattr(provider, "configured", False):
        version = str(getattr(provider, "ruleset_version", None) or "empty-v1")
    elif supplied_provider is not None and not isinstance(supplied_provider, str) and not config.get("ruleset_version"):
        version = str(getattr(provider, "ruleset_version", None) or "empty-v1")
    else:
        version = str(
            config.get("safety_ruleset_version")
            or getattr(provider, "ruleset_version", None)
            or "empty-v1"
        )
    ddi_provider = config.get("ddi_provider")
    if ddi_provider is None and isinstance(provider, (EmptySafetyRuleProvider, DisabledSafetyRuleProvider)):
        ddi_provider = EmptyDDIProvider(version)
    dose_provider = config.get("dose_rule_provider") or config.get("dose_provider") or provider
    ddi_provider = ddi_provider or provider
    return SafetyEngine(
        rule_provider=provider,
        dose_provider=dose_provider,
        ddi_provider=ddi_provider,
        allergy_provider=config.get("allergy_provider"),
        contraindication_provider=config.get("contraindication_provider"),
        ruleset_version=version,
    )


__all__ = [
    "AllergyProvider", "ContraindicationProvider", "DDIProvider", "DisabledDDIProvider",
    "DoseRuleProvider",
    "DisabledSafetyRuleProvider", "EmptyDDIProvider", "EmptySafetyRuleProvider",
    "FixtureDDIProvider", "FixtureDoseRuleProvider", "FixtureSafetyRuleProvider",
    "JsonSafetyRuleProvider", "ProviderCheck", "SafetyCheckResult", "SafetyEngine",
    "SafetyFinding", "SafetyFreezeError", "SafetyFreezeService", "SafetyProviderError",
    "SafetyRuleProvider", "STATUS_BLOCK", "STATUS_CHECK_FAILED", "STATUS_PASS",
    "STATUS_WARN", "SEVERITY_BLOCK", "SEVERITY_INFO", "SEVERITY_WARN",
    "build_safety_engine", "build_safety_rule_provider", "load_rule_pack", "parse_dose",
]
