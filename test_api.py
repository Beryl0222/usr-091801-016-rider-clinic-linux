"""HTTP 契约测试：角色控制、端到端流程、并发改期 409、重放核对端点。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from clinic.engine import ClinicService
from clinic.server import ApiState, build_handler_class
from service import SERVICE_ID, SERVICE_NAME, health_payload

DAY = "2026-09-20"


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        state = ApiState(health_extra={"service": SERVICE_ID, "name": SERVICE_NAME})
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler_class(state))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.service = state.service

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, method, path, body=None, role="staff", query=None):
        url = f"{self.base}{path}"
        if query:
            url += "?" + urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"X-Role": role}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def post(self, path, body, role="staff"):
        return self.call("POST", path, body, role)

    def get(self, path, role="staff", query=None):
        return self.call("GET", path, None, role, query)

    def _seed(self):
        for loc in (("loc-center-a", "A中心", "center", "A"),
                    ("loc-post-a", "A驿站", "post", "A"),
                    ("loc-station-a", "A站", "station", "A")):
            self.post("/admin/locations",
                      dict(zip(("location_id", "name", "type", "zone"), loc)))
        self.post("/admin/practitioners",
                  {"practitioner_id": "doc-li", "name": "李医生", "team_id": "team-1"})
        self.post("/admin/slots", {
            "slot_id": "s1", "location_id": "loc-center-a",
            "service_code": "consult", "practitioner_id": "doc-li",
            "start": f"{DAY}T09:00", "end": f"{DAY}T12:00", "capacity": 1})
        self.post("/admin/riders", {
            "rider_id": "r1", "zone": "A", "insurance": "employee",
            "package_version": "none", "team_id": "team-1"})

    def test_01_health_shape_unchanged(self):
        status, body = self.get("/health", role="rider")
        self.assertEqual(status, 200)
        self.assertEqual(body, health_payload())

    def test_02_end_to_end_flow(self):
        self._seed()
        # 核验岗写入身份
        status, _ = self.post("/riders/r1/identity", {
            "id_ref": "ID-R1-9988", "method": "face",
            "verified_at": f"{DAY}T08:00", "name_masked": "张*"}, role="verifier")
        self.assertEqual(status, 201)

        # 骑手提交时间窗（不含任何订单信息）
        status, req = self.post("/requests", {
            "request_id": "q1", "rider_id": "r1", "service_code": "consult",
            "window_start": f"{DAY}T09:00", "window_end": f"{DAY}T10:00"},
            role="rider")
        self.assertEqual(status, 201)
        self.assertNotIn("order", json.dumps(req))

        status, matched = self.post("/requests/q1/match", {})
        self.assertEqual(status, 200)
        appt = matched["appointment"]
        self.assertEqual(appt["slot_id"], "s1")
        # 费用快照带来源与依据
        self.assertIn("basis", appt["fee_snapshot"])
        self.assertIn("source", appt["fee_snapshot"])

        # 开诊前可执行队列
        status, queue = self.get("/queue", query={"zone": "A", "day": DAY})
        self.assertEqual(status, 200)
        self.assertEqual(queue["slots"][0]["appointments"][0]["appt_id"],
                         appt["appt_id"])

        # 到诊 -> 转诊处置
        self.post(f"/appointments/{appt['appt_id']}/arrival",
                  {"arrived_at": f"{DAY}T09:02"})
        status, enc = self.post(f"/appointments/{appt['appt_id']}/complete", {
            "disposition": "formal_referral", "diagnosis_code": "J06.9",
            "at": f"{DAY}T09:20"})
        self.assertEqual(status, 200)
        self.assertEqual(len(enc["fee_lines"]), 2)

        # 跨日随访
        status, fu = self.post(f"/appointments/{appt['appt_id']}/followups", {
            "service_code": "consult", "due": "2026-09-21T09:00"})
        self.assertEqual(status, 201)
        followup_id = fu["followup_id"]

        # 跨站点：同团队接续
        status, _ = self.post("/riders/r1/transfer-site", {"new_zone": "B"},
                              role="rider")
        self.assertEqual(status, 200)
        status, team = self.get("/teams/team-1/followups")
        self.assertEqual(status, 200)
        self.assertEqual(team[0]["followup_id"], followup_id)
        self.assertEqual(team[0]["site_zone"], "B")

    def test_03_role_isolation(self):
        # 工会看不到档案
        status, err = self.get("/riders/r1/health", role="union")
        self.assertEqual(status, 403)
        self.assertEqual(err["error"], "forbidden")
        # 工会看不到核验
        status, _ = self.get("/riders/r1/identity", role="union")
        self.assertEqual(status, 403)
        # 核验岗看不到队列
        status, _ = self.get("/queue", role="verifier",
                             query={"zone": "A", "day": DAY})
        self.assertEqual(status, 403)
        # 骑手不能改号源
        status, _ = self.post("/slots/s1/adjust",
                              {"new_start": f"{DAY}T10:00",
                               "new_end": f"{DAY}T12:00"}, role="rider")
        self.assertEqual(status, 403)

    def test_04_union_coverage_is_deidentified(self):
        status, stats = self.get("/union/coverage", role="union", query={
            "start": f"{DAY}T00:00", "end": "2026-09-21T00:00"})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(stats["visits"], 1)
        blob = json.dumps(stats, ensure_ascii=False)
        self.assertNotIn("r1", blob)
        self.assertNotIn("J06.9", blob)

    def test_05_concurrent_slot_adjust_conflicts(self):
        status, slot = self.post("/admin/slots", {
            "slot_id": "s2", "location_id": "loc-center-a",
            "service_code": "consult", "practitioner_id": "doc-li",
            "start": f"{DAY}T13:00", "end": f"{DAY}T15:00", "capacity": 1})
        self.assertEqual(status, 201)
        version = slot["version"]
        status, _ = self.post("/slots/s2/adjust", {
            "new_start": f"{DAY}T13:30", "new_end": f"{DAY}T15:00",
            "expected_version": version})
        self.assertEqual(status, 200)
        # 带旧版本的并发改期必须 409
        status, err = self.post("/slots/s2/adjust", {
            "new_start": f"{DAY}T14:00", "new_end": f"{DAY}T15:00",
            "expected_version": version})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "slot_version_conflict")

    def test_06_replay_endpoint_audits_event_log(self):
        status, events = self.get("/events")
        self.assertEqual(status, 200)
        status, report = self.post("/replay", {"events": events["events"]})
        self.assertEqual(status, 200)
        self.assertTrue(report["ok"], report)
        self.assertTrue(report["sections"]["capacity"]["ok"])
        self.assertTrue(report["sections"]["fee_basis"]["ok"])
        self.assertTrue(report["sections"]["priority"]["ok"])
        self.assertTrue(report["sections"]["followup_continuity"]["ok"])

    def test_07_unknown_route_404(self):
        status, err = self.get("/nope", role="staff")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"], "not_found")


if __name__ == "__main__":
    unittest.main()
