"""HTTP 接口层。

角色（请求头 X-Role）：
  staff     医护：队列、档案、排班改期、处置、随访
  verifier  核验岗：仅身份核验结果
  union     工会：仅去标识化覆盖统计
  rider     骑手：提交时间窗与自身相关操作
任何角色都无法越权访问另一角色的视图。
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import catalog
from .clock import format_minutes, parse_minutes
from .engine import ClinicService
from .errors import ClinicError
from .replay import audit as replay_audit

ROLE_STAFF = "staff"
ROLE_VERIFIER = "verifier"
ROLE_UNION = "union"
ROLE_RIDER = "rider"

# 分钟制字段在出参时统一格式化为时间字符串
_TIME_KEYS = {
    "start", "end", "window_start", "window_end", "due", "at",
    "verified_at", "arrival_minute", "new_start", "new_end",
    "day_start", "day_end_exclusive",
}


def _dto(value, time_keys=_TIME_KEYS):
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in time_keys and isinstance(v, int):
                out[k] = format_minutes(v)
            else:
                out[k] = _dto(v, time_keys)
        return out
    if isinstance(value, (list, tuple)):
        return [_dto(v, time_keys) for v in value]
    return value


class ApiState:
    def __init__(self, service=None, health_extra=None):
        self.service = service or ClinicService()
        self.health_extra = health_extra or {}


def build_handler_class(state):
    """每个服务器绑定独立状态，避免多实例共享类属性。"""

    class ApiHandler(BaseHTTPRequestHandler):
        def _send(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode())
            except json.JSONDecodeError:
                raise ClinicError("bad_json", "请求体不是合法 JSON", 400)

        def _role(self):
            return self.headers.get("X-Role", ROLE_RIDER)

        def _require(self, *roles):
            if self._role() not in roles:
                raise ClinicError("forbidden",
                                  f"该接口需要角色: {', '.join(roles)}", 403)

        def _minutes(self, body, key, required=True):
            if key not in body:
                if required:
                    raise ClinicError("validation_failed", f"缺少字段: {key}", 422)
                return None
            try:
                return parse_minutes(body[key])
            except (ValueError, TypeError):
                raise ClinicError("validation_failed", f"时间格式错误: {key}", 422)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            try:
                self._route(method, path, query)
            except ClinicError as error:
                self._send(error.status, {"error": error.code, "message": error.message})
            except Exception as error:  # 兜底，避免裸 500 无结构
                self._send(500, {"error": "internal", "message": str(error)})

        # ===== 路由 ======================================================

        def _route(self, method, path, query):
            svc = self.state.service
            body = self._read_json() if method == "POST" else {}

            if method == "GET" and path == "/health":
                return self._send(200, {"status": "ok", **self.state.health_extra})

            if method == "POST" and path == "/admin/locations":
                self._require(ROLE_STAFF)
                return self._send(201, _dto(svc.register_location(
                    body["location_id"], body["name"], body["type"], body["zone"])))
            if method == "POST" and path == "/admin/practitioners":
                self._require(ROLE_STAFF)
                return self._send(201, _dto(svc.register_practitioner(
                    body["practitioner_id"], body["name"], body["team_id"])))
            if method == "POST" and path == "/admin/slots":
                self._require(ROLE_STAFF)
                return self._send(201, _dto(svc.open_slot(
                    body["slot_id"], body["location_id"], body["service_code"],
                    body["practitioner_id"],
                    self._minutes(body, "start"), self._minutes(body, "end"),
                    int(body["capacity"]))))
            if method == "POST" and path == "/admin/riders":
                self._require(ROLE_STAFF)
                return self._send(201, _dto(svc.register_rider(
                    body["rider_id"], body["zone"], body["insurance"],
                    body.get("package_version", catalog.PACKAGE_NONE),
                    body.get("team_id"))))

            if path.startswith("/riders/") and path.endswith("/identity"):
                rider_id = path.split("/")[2]
                if method == "POST":
                    self._require(ROLE_VERIFIER, ROLE_STAFF)
                    return self._send(201, _dto(svc.verify_identity(
                        rider_id, body["id_ref"], body["method"],
                        self._minutes(body, "verified_at"),
                        body.get("name_masked", ""), body.get("verified", True))))
                self._require(ROLE_VERIFIER, ROLE_STAFF)
                return self._send(200, _dto(svc.identity_view(rider_id)))

            if path.startswith("/riders/") and path.endswith("/health"):
                rider_id = path.split("/")[2]
                self._require(ROLE_STAFF)
                return self._send(200, _dto(svc.health_view(rider_id)))

            if path.startswith("/riders/") and path.endswith("/transfer-site") and method == "POST":
                rider_id = path.split("/")[2]
                self._require(ROLE_STAFF, ROLE_RIDER)
                return self._send(200, _dto(svc.transfer_rider_site(rider_id, body["new_zone"])))

            if method == "POST" and path == "/requests":
                self._require(ROLE_STAFF, ROLE_RIDER)
                req = svc.submit_request(
                    body["request_id"], body["rider_id"], body["service_code"],
                    self._minutes(body, "window_start"), self._minutes(body, "window_end"))
                return self._send(201, _dto(req))
            if path.startswith("/requests/") and path.endswith("/triage") and method == "POST":
                request_id = path.split("/")[2]
                self._require(ROLE_STAFF)
                return self._send(200, _dto(svc.triage(request_id, body["priority"])))
            if path.startswith("/requests/") and path.endswith("/window") and method == "POST":
                request_id = path.split("/")[2]
                self._require(ROLE_STAFF, ROLE_RIDER)
                return self._send(200, _dto(svc.update_request_window(
                    request_id,
                    self._minutes(body, "window_start"),
                    self._minutes(body, "window_end"))))
            if path.startswith("/requests/") and path.endswith("/match") and method == "POST":
                request_id = path.split("/")[2]
                self._require(ROLE_STAFF)
                return self._send(200, _dto(svc.match_request(request_id)))

            if method == "GET" and path == "/queue":
                self._require(ROLE_STAFF)
                return self._send(200, _dto(svc.daily_queue(query["zone"], query["day"])))

            if path.startswith("/slots/") and path.endswith("/adjust") and method == "POST":
                slot_id = path.split("/")[2]
                self._require(ROLE_STAFF)
                return self._send(200, _dto(svc.adjust_slot(
                    slot_id,
                    self._minutes(body, "new_start", required=False),
                    self._minutes(body, "new_end", required=False),
                    bool(body.get("cancelled", False)),
                    body.get("reason", "practitioner_reschedule"),
                    body.get("expected_version"))))

            if path.startswith("/appointments/") and path.endswith("/arrival") and method == "POST":
                appt_id = path.split("/")[2]
                self._require(ROLE_STAFF)
                return self._send(200, _dto(svc.record_arrival(
                    appt_id, self._minutes(body, "arrived_at"))))
            if path.startswith("/appointments/") and path.endswith("/grab-order") and method == "POST":
                appt_id = path.split("/")[2]
                self._require(ROLE_STAFF, ROLE_RIDER)
                return self._send(200, _dto(svc.rider_grab_order(
                    appt_id,
                    self._minutes(body, "new_window_start", required=False),
                    self._minutes(body, "new_window_end", required=False))))
            if path.startswith("/appointments/") and path.endswith("/complete") and method == "POST":
                appt_id = path.split("/")[2]
                self._require(ROLE_STAFF)
                return self._send(200, _dto(svc.complete_encounter(
                    appt_id, body["disposition"], body["diagnosis_code"],
                    body.get("note", ""), self._minutes(body, "at"))))
            if path.startswith("/appointments/") and path.endswith("/followups") and method == "POST":
                appt_id = path.split("/")[2]
                self._require(ROLE_STAFF)
                return self._send(201, _dto(svc.schedule_followup(
                    appt_id, body["service_code"], self._minutes(body, "due"))))
            if path.startswith("/followups/") and path.endswith("/complete") and method == "POST":
                followup_id = path.split("/")[2]
                self._require(ROLE_STAFF)
                return self._send(200, _dto(svc.complete_followup(
                    followup_id, self._minutes(body, "at"))))

            if method == "GET" and path.startswith("/teams/") and path.endswith("/followups"):
                team_id = path.split("/")[2]
                self._require(ROLE_STAFF)
                return self._send(200, _dto(svc.team_followups(team_id)))

            if method == "GET" and path == "/union/coverage":
                self._require(ROLE_UNION, ROLE_STAFF)
                return self._send(200, _dto(svc.union_coverage(
                    self._minutes(query, "start"), self._minutes(query, "end"))))

            if method == "GET" and path == "/events":
                self._require(ROLE_STAFF)
                return self._send(200, {"events": svc.store.events})
            if method == "POST" and path == "/replay":
                # 重放核对可由 staff 触发；输入自带事件序列，不接触线上状态
                self._require(ROLE_STAFF)
                events = body.get("events")
                if not isinstance(events, list):
                    raise ClinicError("validation_failed", "需要 events 数组", 422)
                return self._send(200, _dto(replay_audit(events)))

            raise ClinicError("not_found", f"未知路由: {method} {path}", 404)

        def log_message(self, *_args):
            return

    ApiHandler.state = state
    return ApiHandler


def make_server(port=0, host="127.0.0.1", service=None, state=None):
    state = state or ApiState(service, health_extra={
        "service": "rider-clinic", "name": "骑手碎片时间诊疗协同"})
    return ThreadingHTTPServer((host, port), build_handler_class(state))
