"""HTTP 接口层：路由、工作角色校验与 JSON 编解码。

角色通过 X-Actor-Role 请求头声明：
- clinician：医护（队列、预约、健康档案）
- rider：骑手（时间窗、改期、签到）
- verifier：身份核验员（身份证明核验，独立存储）
- union：工会（仅去标识化覆盖统计）
- admin：管理（全部接口与状态重置）
"""
import json
import re
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from .models import ApiError, bad_request, forbidden, unauthorized

ROLE_CLINICIAN = "clinician"
ROLE_RIDER = "rider"
ROLE_UNION = "union"
ROLE_VERIFIER = "verifier"
ROLE_ADMIN = "admin"

_STAFF = {ROLE_CLINICIAN, ROLE_RIDER, ROLE_ADMIN}
_CLINICAL = {ROLE_CLINICIAN, ROLE_ADMIN}
_VERIFIER = {ROLE_VERIFIER, ROLE_ADMIN}
_UNION = {ROLE_UNION, ROLE_ADMIN}
_ADMIN = {ROLE_ADMIN}


class _Ctx:
    def __init__(self, params, body, query):
        self.params = params
        self.body = body
        self.query = query

    def q(self, name, default=None):
        return self.query.get(name, default)


def build_handler(app, health_payload):
    routes = [
        ("GET", r"^/health$", None, lambda c: (200, health_payload())),
        ("GET", r"^/api/points$", _STAFF, lambda c: (200, {"points": app.list_points()})),
        ("GET", r"^/api/doctors$", _STAFF, lambda c: (200, {"doctors": app.list_doctors()})),
        ("GET", r"^/api/teams$", _STAFF, lambda c: (200, {"teams": app.list_teams()})),
        ("POST", r"^/api/riders$", _STAFF, lambda c: (201, app.register_rider(c.body))),
        ("GET", r"^/api/riders/(?P<rid>[^/]+)$", _STAFF,
         lambda c: (200, app.get_rider(c.params["rid"]))),
        ("POST", r"^/api/riders/(?P<rid>[^/]+)/windows$", _STAFF,
         lambda c: (201, app.submit_window(c.params["rid"], c.body))),
        ("GET", r"^/api/riders/(?P<rid>[^/]+)/appointments$", _STAFF,
         lambda c: (200, {"appointments": app.list_rider_appointments(c.params["rid"])})),
        ("GET", r"^/api/riders/(?P<rid>[^/]+)/followups$", _STAFF,
         lambda c: (200, {"followups": app.rider_followups(c.params["rid"])})),
        ("GET", r"^/api/riders/(?P<rid>[^/]+)/cost-estimate$", _STAFF,
         lambda c: (200, app.cost_estimate(c.params["rid"], c.q("service", "consult_15m")))),
        ("GET", r"^/api/appointments/(?P<aid>[^/]+)$", _STAFF,
         lambda c: (200, app.get_appointment(c.params["aid"]))),
        ("POST", r"^/api/appointments/(?P<aid>[^/]+)/reschedule$", _STAFF,
         lambda c: (200, app.reschedule(c.params["aid"], c.body))),
        ("POST", r"^/api/appointments/(?P<aid>[^/]+)/checkin$", _STAFF,
         lambda c: (200, app.checkin(c.params["aid"], c.body))),
        ("POST", r"^/api/appointments/(?P<aid>[^/]+)/outcome$", _STAFF,
         lambda c: (200, app.record_outcome(c.params["aid"], c.body))),
        ("POST", r"^/api/appointments/(?P<aid>[^/]+)/priority$", _STAFF,
         lambda c: (200, app.set_priority(c.params["aid"], c.body))),
        ("POST", r"^/api/shifts$", _STAFF, lambda c: (201, app.create_shift(c.body))),
        ("GET", r"^/api/shifts$", _STAFF,
         lambda c: (200, {"shifts": app.list_shifts(c.q("date"), c.q("point_id"))})),
        ("POST", r"^/api/shifts/(?P<sid>[^/]+)/change$", _STAFF,
         lambda c: (200, app.change_shift(c.params["sid"], c.body))),
        ("GET", r"^/api/queue$", _STAFF,
         lambda c: (200, app.queue(c.q("date"), c.q("point_id")))),
        ("POST", r"^/api/followups/(?P<fid>[^/]+)/complete$", _STAFF,
         lambda c: (200, app.complete_followup(c.params["fid"], c.body))),
        ("GET", r"^/api/teams/(?P<tid>[^/]+)/followups$", _STAFF,
         lambda c: (200, {"followups": app.team_followups(c.params["tid"], c.q("due_before"))})),
        ("POST", r"^/api/identity/verifications$", _VERIFIER,
         lambda c: (201, app.record_identity_verification(c.body))),
        ("GET", r"^/api/identity/(?P<rid>[^/]+)$", _VERIFIER,
         lambda c: (200, app.get_identity(c.params["rid"]))),
        ("GET", r"^/api/health-records/(?P<rid>[^/]+)$", _CLINICAL,
         lambda c: (200, app.get_health_record(c.params["rid"]))),
        ("GET", r"^/api/union/coverage$", _UNION,
         lambda c: (200, app.union_coverage(c.q("date")))),
        ("POST", r"^/api/reset$", _ADMIN, lambda c: (200, app.reset())),
    ]
    compiled = [(method, re.compile(pattern), roles, fn)
                for method, pattern, roles, fn in routes]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method):
            parsed = urlparse(self.path)
            try:
                for route_method, pattern, roles, fn in compiled:
                    if route_method != method:
                        continue
                    match = pattern.match(parsed.path)
                    if not match:
                        continue
                    if roles is not None:
                        role = self.headers.get("X-Actor-Role", "")
                        if not role:
                            raise unauthorized()
                        if role not in roles:
                            raise forbidden()
                    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                    body = self._read_body() if method == "POST" else {}
                    status, obj = fn(_Ctx(match.groupdict(), body, query))
                    self._send(status, obj)
                    return
                self._send(404, {"error": "not_found", "message": "接口不存在"})
            except ApiError as err:
                self._send(err.status, {"error": err.code, "message": err.message})
            except Exception as err:  # noqa: BLE001 - 兜底，避免连接悬挂
                self._send(500, {"error": "internal", "message": str(err)})

        def _read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise bad_request("请求体须为JSON")
            if not isinstance(body, dict):
                raise bad_request("请求体须为JSON对象")
            return body

        def _send(self, status, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    return Handler
