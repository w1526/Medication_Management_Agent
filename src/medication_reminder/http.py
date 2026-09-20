"""Small standard-library HTTP API for the MVP."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
from threading import Thread, Event
from urllib.parse import parse_qs, urlsplit

from .service import DomainError, MedicationService
from .semantic import MedicationSemanticAgent


WEB_ROOT = Path(__file__).resolve().parents[2] / "web"
LOGGER = logging.getLogger(__name__)


class Application:
    def __init__(self, service):
        self.service = service
        self.agent = MedicationSemanticAgent(service)

    def static_file(self, path):
        """Return a small allow-listed UI asset, keeping the MVP dependency-free."""
        files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/ui": ("index.html", "text/html; charset=utf-8"),
            "/ui/": ("index.html", "text/html; charset=utf-8"),
            "/elder": ("index.html", "text/html; charset=utf-8"),
            "/elder/": ("index.html", "text/html; charset=utf-8"),
            "/family": ("index.html", "text/html; charset=utf-8"),
            "/family/": ("index.html", "text/html; charset=utf-8"),
            "/doctor": ("index.html", "text/html; charset=utf-8"),
            "/doctor/": ("index.html", "text/html; charset=utf-8"),
            "/ui/elder": ("index.html", "text/html; charset=utf-8"),
            "/ui/elder/": ("index.html", "text/html; charset=utf-8"),
            "/ui/family": ("index.html", "text/html; charset=utf-8"),
            "/ui/family/": ("index.html", "text/html; charset=utf-8"),
            "/ui/doctor": ("index.html", "text/html; charset=utf-8"),
            "/ui/doctor/": ("index.html", "text/html; charset=utf-8"),
            "/assets/app.css": ("assets/app.css", "text/css; charset=utf-8"),
            "/assets/app.js": ("assets/app.js", "application/javascript; charset=utf-8"),
        }
        entry = files.get(path)
        if not entry:
            return None
        file_path = WEB_ROOT / entry[0]
        if not file_path.is_file():
            return None
        return 200, entry[1], file_path.read_bytes()

    def handle(self, method, path, body=None, query=None):
        body = body or {}
        query = query or {}
        try:
            if method == "GET" and path == "/api/v1/medication/agent/status":
                return 200, self.agent.status()
            if method == "POST" and path == "/api/v1/medication/agent/message":
                return 200, self.agent.handle(
                    body.get("elder_id"),
                    body.get("text"),
                    source=body.get("source", "chat_agent"),
                    created_by=body.get("created_by"),
                    interaction_id=body.get("interaction_id"),
                    event_id=body.get("event_id"),
                    trace_id=body.get("trace_id"),
                    occurred_at=body.get("occurred_at"),
                )
            if method == "GET" and path == "/health/live":
                return 200, {"status": "ok"}
            if method == "GET" and path == "/api/v1/medication/dashboard":
                limit = min(max(int(query.get("limit", "30")), 1), 100)
                dashboard = self.service.get_dashboard(
                    query.get("elder_id"), event_limit=limit
                )
                dashboard["health"] = {"status": "ok"}
                dashboard["agent"] = self.agent.status()
                return 200, dashboard
            if method == "GET" and path == "/health/ready":
                return 200, {"status": "ready"}
            if method == "POST" and path == "/api/v1/medication/plans/draft":
                return 201, self.service.create_draft(body)
            if method == "GET" and path == "/api/v1/medication/plans":
                return 200, {"items": self.service.list_plans(query.get("elder_id"))}
            plan_safety_prefix = "/api/v1/medication/plans/"
            if method == "GET" and path.startswith(plan_safety_prefix) and path.endswith("/safety/history"):
                plan_id = path[len(plan_safety_prefix):-len("/safety/history")]
                return 200, {"items": self.service.list_safety_history(plan_id)}
            if method == "GET" and path.startswith(plan_safety_prefix) and path.endswith("/safety"):
                plan_id = path[len(plan_safety_prefix):-len("/safety")]
                version = body.get("version") if body else query.get("version")
                safety = self.service.get_latest_safety_check(plan_id, version)
                if safety is None:
                    plan = self.service.get_plan(plan_id, version)
                    safety = {
                        "check_id": None,
                        "plan_id": plan_id,
                        "plan_version": plan["version"],
                        "status": "NOT_CHECKED",
                        "ruleset_version": self.service.safety_ruleset_version,
                        "checked_at": None,
                        "findings": [],
                        "coverage": {},
                        "trace_id": None,
                    }
                return 200, safety
            if method == "POST" and path.startswith(plan_safety_prefix) and path.endswith("/safety/check"):
                plan_id = path[len(plan_safety_prefix):-len("/safety/check")]
                return 200, self.service.check_plan_safety(
                    plan_id, body.get("version"), trace_id=body.get("trace_id")
                )
            safety_check_prefix = "/api/v1/medication/safety/checks/"
            if method == "GET" and path.startswith(safety_check_prefix):
                check_id = path[len(safety_check_prefix):]
                if check_id:
                    return 200, self.service.get_safety_check(check_id)
            if method == "POST" and path.endswith("/submit") and "/plans/" in path:
                plan_id = path.split("/plans/", 1)[1].rsplit("/submit", 1)[0]
                return 200, self.service.submit_plan(plan_id, body.get("version"))
            if method == "POST" and path.endswith("/approve") and "/plans/" in path:
                plan_id = path.split("/plans/", 1)[1].rsplit("/approve", 1)[0]
                return 200, self.service.approve_plan(
                    plan_id, body.get("approved_by"), body.get("version")
                )
            if method == "POST" and path.endswith("/pause") and "/plans/" in path:
                plan_id = path.split("/plans/", 1)[1].rsplit("/pause", 1)[0]
                return 200, self.service.pause_plan(plan_id, body.get("version"))
            if method == "POST" and path.endswith("/revise") and "/plans/" in path:
                plan_id = path.split("/plans/", 1)[1].rsplit("/revise", 1)[0]
                return 201, self.service.revise_plan(plan_id, body)
            if method == "GET" and path.startswith("/api/v1/medication/plans/"):
                plan_id = path.rsplit("/", 1)[1]
                return 200, {"items": self.service.list_plans_by_id(plan_id)}
            if method == "GET" and path == "/api/v1/medication/today":
                return 200, {"items": self.service.get_today(
                    query.get("elder_id"), query.get("date")
                )}
            if method == "GET" and path == "/api/v1/medication/notifications":
                return 200, {"items": self.service.list_active_notifications(
                    query.get("elder_id")
                )}
            if method == "GET" and path == "/api/v1/medication/escalations/summary":
                return 200, self.service.escalation_summary(query.get("elder_id"))
            if method == "GET" and path == "/api/v1/medication/escalations":
                limit = min(max(int(query.get("limit", "100")), 1), 1000)
                return 200, {"items": self.service.list_escalations(
                    query.get("elder_id"), query.get("status"), query.get("level"), limit
                )}
            if method == "POST" and path == "/api/v1/medication/escalations/run":
                clock = None
                if body.get("now"):
                    from .service import parse_datetime
                    clock = parse_datetime(body["now"])
                return 200, self.service.run_escalation_cycle(clock)
            escalation_prefix = "/api/v1/medication/escalations/"
            if method == "POST" and path.startswith(escalation_prefix) and path.endswith("/acknowledge"):
                escalation_id = path[len(escalation_prefix):-len("/acknowledge")]
                return 200, self.service.acknowledge_escalation(escalation_id, body)
            if method == "POST" and path.startswith(escalation_prefix) and path.endswith("/resolve"):
                escalation_id = path[len(escalation_prefix):-len("/resolve")]
                return 200, self.service.resolve_escalation(escalation_id, body)
            if method == "POST" and path.startswith(escalation_prefix) and path.endswith("/cancel"):
                escalation_id = path[len(escalation_prefix):-len("/cancel")]
                return 200, self.service.cancel_escalation(escalation_id, body)
            if method == "GET" and path.startswith(escalation_prefix):
                escalation_id = path[len(escalation_prefix):]
                if escalation_id:
                    return 200, self.service.get_escalation(escalation_id)
            if method == "POST" and path.startswith("/api/v1/medication/occurrences/") and path.endswith("/confirm"):
                occurrence_id = path[len("/api/v1/medication/occurrences/"):-len("/confirm")]
                return 200, self.service.record_manual_confirmation(occurrence_id, body)
            if method == "GET" and path.startswith("/api/v1/medication/occurrences/"):
                occurrence_id = path.rsplit("/", 1)[1]
                return 200, self.service.get_occurrence(occurrence_id)
            if method == "POST" and path == "/api/v1/medication/responses":
                return 200, self.service.process_user_response(body)
            if method == "POST" and path == "/api/v1/medication/device-events":
                return 200, self.service.process_device_event(body)
            if method == "POST" and path == "/api/v1/medication/scheduler/run":
                clock = None
                if body.get("now"):
                    from .service import parse_datetime
                    clock = parse_datetime(body["now"])
                return 200, self.service.run_scheduler_cycle(clock)
            if method == "GET" and path == "/api/v1/medication/events/outbox":
                return 200, {"items": self.service.list_outbox(query.get("status"))}
            if method == "GET" and path == "/api/v1/medication/events":
                limit = min(max(int(query.get("limit", "100")), 1), 1000)
                return 200, {"items": self.service.list_event_log(
                    query.get("event_type"), query.get("occurrence_id"),
                    limit, query.get("elder_id")
                )}
            raise DomainError("route not found", 404)
        except DomainError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DomainError("invalid request: %s" % exc, 400) from exc


def _query_values(query_string):
    parsed = parse_qs(query_string, keep_blank_values=False)
    return {key: values[-1] for key, values in parsed.items() if values}


class RequestHandler(BaseHTTPRequestHandler):
    app = None

    def _write_bytes(self, status, content_type, raw):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _write(self, status, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._write_bytes(status, "application/json; charset=utf-8", raw)

    def _dispatch(self, method):
        split = urlsplit(self.path)
        if method == "GET":
            static = self.app.static_file(split.path)
            if static:
                self._write_bytes(*static)
                return
        body = {}
        if method == "POST":
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1024 * 1024:
                self._write(413, {"error": "request body too large"})
                return
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._write(400, {"error": "invalid JSON: %s" % exc})
                return
            if not isinstance(body, dict):
                self._write(400, {"error": "JSON body must be an object"})
                return
        try:
            status, payload = self.app.handle(method, split.path, body, _query_values(split.query))
            self._write(status, payload)
        except DomainError as exc:
            response = {"error": exc.message}
            if exc.details:
                # Safety rejection is a client-visible conflict.  Keep the
                # audit identifiers at the top level while retaining the
                # generic nested details shape for existing callers.
                if exc.message in ("SAFETY_BLOCKED", "SAFETY_CHECK_FAILED"):
                    response.update(exc.details)
                response["details"] = exc.details
            self._write(exc.status, response)
        except Exception as exc:  # pragma: no cover - defensive HTTP boundary
            self._write(500, {"error": "internal server error", "detail": str(exc)})

    def finish(self):
        try:
            super().finish()
        finally:
            # ThreadingHTTPServer creates short-lived request threads. Release
            # their SQLite connection instead of accumulating one per request.
            if self.app is not None:
                self.app.service.storage.close_thread_connection()

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def log_message(self, format_string, *args):
        # Keep the MVP quiet in tests and containers.
        return


class SchedulerThread(Thread):
    def __init__(self, service, interval=1.0):
        super().__init__(daemon=True)
        self.service = service
        self.interval = float(interval)
        self.stop_event = Event()

    def run(self):
        while not self.stop_event.is_set():
            try:
                # Publishing has its own worker so a slow adapter never delays
                # occurrence claims and deadline processing.
                self.service.run_scheduler_cycle(publish=False)
            except Exception:
                # Durable state remains intact; next tick retries the worker.
                LOGGER.exception("scheduler cycle failed")
            self.stop_event.wait(self.interval)

    def stop(self):
        self.stop_event.set()


class OutboxPublisherThread(Thread):
    """Publish durable events independently from scheduling state changes."""

    def __init__(self, service, interval=0.25):
        super().__init__(daemon=True)
        self.service = service
        self.interval = float(interval)
        self.stop_event = Event()

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.service.publish_outbox()
            except Exception:
                LOGGER.exception("outbox publish cycle failed")
            self.stop_event.wait(self.interval)

    def stop(self):
        self.stop_event.set()


def serve(database, host="127.0.0.1", port=8080, interval=1.0):
    service = MedicationService(database)
    app = Application(service)
    RequestHandler.app = app
    server = ThreadingHTTPServer((host, int(port)), RequestHandler)
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print("Web UI: http://%s:%s/" % (display_host, server.server_address[1]), flush=True)
    worker = SchedulerThread(service, interval)
    publisher = OutboxPublisherThread(service, min(max(float(interval) / 2.0, 0.1), 0.5))
    worker.start()
    publisher.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop()
        publisher.stop()
        worker.join(timeout=max(float(interval), 1.0) + 1.0)
        publisher.join(timeout=2.0)
        server.server_close()
        service.close()

