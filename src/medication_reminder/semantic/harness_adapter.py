"""DeepSeek Harness adapter for constrained medication semantics.

This module deliberately imports the optional SDK lazily. The deterministic
medication service can still run without the Harness extra, while the agent
endpoint returns a clear configuration error until Python 3.10+, the SDK and
DEEPSEEK_API_KEY are available.
"""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import uuid

from ..service import DomainError, SHANGHAI


def load_local_env(path):
    """Load simple KEY=VALUE entries for this process only.

    Existing process environment variables win. This keeps the local project
    file convenient without changing the parent shell or any other project.
    """
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        if item.startswith("export "):
            item = item[7:].lstrip()
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


class HarnessSemanticAdapter:
    """Call one isolated Harness session and require a JSON-only result."""

    def __init__(self, project_root=None, model=None, profile=None):
        self.project_root = Path(project_root or Path(__file__).resolve().parents[3])
        load_local_env(self.project_root / ".env")
        self.model = model or os.environ.get("DSH_MODEL", "deepseek-v4-flash")
        self.profile = profile or os.environ.get("DSH_PROFILE", "sdk-minimal")
        self.workspace = Path(
            os.environ.get(
                "DSH_WORKSPACE",
                str(self.project_root / ".harness-workspace"),
            )
        ).resolve()
        self.dsh_home = Path(
            os.environ.get("DSH_HOME", str(self.project_root / ".dsh-home"))
        ).resolve()

    def status(self):
        try:
            import deepseek_harness  # noqa: F401
            sdk_installed = True
        except ImportError:
            sdk_installed = False
        return {
            "sdk_installed": sdk_installed,
            "key_configured": bool(os.environ.get("DEEPSEEK_API_KEY")),
            "model": self.model,
            "profile": self.profile,
            "ready": sdk_installed and bool(os.environ.get("DEEPSEEK_API_KEY")),
        }

    def parse_plan(self, text, elder_id):
        today = datetime.now(SHANGHAI).date().isoformat()
        prompt = """You are the semantic extraction component of a medication reminder system.
Do not call tools. Do not edit files. Do not approve or activate anything.
Extract only what the user explicitly said and return exactly one JSON object.
The JSON schema is:
{
  "kind": "plan_draft",
  "elder_id": "string",
  "drug_name": "string or null",
  "dosage_text": "string or null",
  "schedule_type": "FIXED_TIME | MEAL_RELATION | INTERVAL | WEEKLY | CYCLE | PRN | null",
  "schedule_config": "object or null",
  "schedule_time": "HH:MM or null (legacy FIXED_TIME compatibility)",
  "relation_to_meal": "餐前 | 餐后 | null",
  "route": "oral or another explicit route or null",
  "start_date": "YYYY-MM-DD or null",
  "timezone": "Asia/Shanghai",
  "missing_fields": ["drug_name", "dosage_text", "schedule_config"],
  "confidence": 0.0
}
Rules: use the supplied elder_id; default timezone is Asia/Shanghai; today is %s;
never invent a drug, dose, meal anchor, interval anchor, weekday, cycle date,
or time; all schedule_type and schedule_config values must be copied from the
user's explicit words.  Use MEAL_RELATION such as
{"meal":"BREAKFAST","relation":"AFTER","offset_minutes":30} for
"早餐后半小时".  Use INTERVAL only when both interval_hours and anchor_at
are explicit.  Use WEEKLY only with explicit ISO weekdays (1=Monday, 7=Sunday).
Use CYCLE only with explicit cycle_start_date, days_on, days_off and times.
Use PRN with condition_text only and never turn it into a daily reminder.
"一天两次" or "饭后吃" without concrete parameters is incomplete: leave
schedule_config null and include schedule_config in missing_fields.  Never
default to 08:00/20:00 or infer breakfast/bedtime.  If start_date is omitted,
return null and include it in missing_fields (the deterministic caller will
default it to today). Other missing required values go in missing_fields.

elder_id: %s
user_text: %s
""" % (today, elder_id, text)
        result = self._run_json(prompt)
        result["kind"] = "plan_draft"
        result["elder_id"] = elder_id
        result.setdefault("timezone", "Asia/Shanghai")
        return result

    def parse_response(self, text):
        prompt = """You are the response classifier for one already-open medication reminder.
Do not call tools. Return exactly one JSON object and no Markdown.
Allowed schema:
{"kind":"medication_response","action":"CONFIRM_TAKEN|DELAY|SKIP|REPEAT",
"delay_minutes": null or positive integer,"confidence":0.0}
Rules: "吃了/已经吃了" is CONFIRM_TAKEN; "跳过/今天不吃" is SKIP;
"再说一遍" is REPEAT; a future delay such as "半小时后" is DELAY with minutes.
Do not output occurrence_id or interaction_id.

user_text: %s
""" % text
        result = self._run_json(prompt)
        result["kind"] = "medication_response"
        return result

    def _run_json(self, prompt):
        if not os.environ.get("DEEPSEEK_API_KEY"):
            raise DomainError(
                "DEEPSEEK_API_KEY is not configured; set it in the environment",
                503,
            )
        try:
            from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig
        except ImportError as exc:
            raise DomainError(
                "DeepSeek Harness SDK is not installed in the Python 3.10+ environment",
                503,
            ) from exc

        self.workspace.mkdir(parents=True, exist_ok=True)
        self.dsh_home.mkdir(parents=True, exist_ok=True)
        config = DeepSeekHarnessConfig(
            provider="deepseek-official",
            model=self.model,
            max_tokens=4096,
            cwd=str(self.workspace),
            runtime_cwd=str(self.workspace),
            dsh_bin=os.environ.get("DSH_BIN"),
            profile=self.profile,
            dsh_home=str(self.dsh_home),
            base_url=os.environ.get("DEEPSEEK_BASE_URL") or None,
            initialize_timeout_seconds=30.0,
            request_timeout_seconds=60.0,
        )
        try:
            with DeepSeekHarness(config) as harness:
                result = harness.run(
                    prompt,
                    session_id="medication-semantic-%s" % uuid.uuid4().hex,
                )
        except Exception as exc:
            # Do not allow a provider exception to echo the credential.
            message = str(exc)
            key = os.environ.get("DEEPSEEK_API_KEY")
            if key:
                message = message.replace(key, "[redacted]")
            raise DomainError("DeepSeek Harness request failed: %s" % message, 502) from exc
        return self._extract_json(result.final_response)

    @staticmethod
    def _extract_json(response):
        text = (response or "").strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end <= start:
                raise DomainError("Harness returned no JSON object", 502)
            try:
                value = json.loads(text[start:end + 1])
            except json.JSONDecodeError as exc:
                raise DomainError("Harness returned invalid JSON", 502) from exc
        if not isinstance(value, dict):
            raise DomainError("Harness response must be a JSON object", 502)
        return value

