"""HTTP 接口端到端测试：完整流程、工作角色与错误码。"""
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from riderclinic.app import App
from riderclinic.http_api import build_handler
from service import health_payload

DAY = "2026-09-21"


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = App()
        handler = build_handler(cls.app, health_payload)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.app.reset()

    def call(self, method, path, body=None, role="clinician"):
        request = Request(self.base + path, method=method)
        if role is not None:
            request.add_header("X-Actor-Role", role)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, data=data, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as err:
            return err.code, json.load(err)

    def seed_shift(self, **extra):
        payload = {"id": "s1", "doctor_id": "d1", "point_id": "sp-center",
                   "date": DAY, "start": "09:00", "end": "10:00",
                   "services": ["consult_15m", "tcm_30m"]}
        payload.update(extra)
        status, _ = self.call("POST", "/api/shifts", payload)
        self.assertEqual(status, 201)

    def seed_rider(self, rid="r1", **extra):
        payload = {"id": rid, "name": "骑手一", "area": "城东", "team_id": "team-a",
                   "insurance": "employee", "package": "plus-v2"}
        payload.update(extra)
        status, _ = self.call("POST", "/api/riders", payload, role="rider")
        self.assertEqual(status, 201)

    def test_full_visit_flow(self):
        self.seed_shift()
        self.seed_rider()
        status, matched = self.call("POST", "/api/riders/r1/windows", {
            "date": DAY, "start": "09:00", "end": "09:30", "area": "城东"}, role="rider")
        self.assertEqual(status, 201)
        self.assertTrue(matched["matched"])
        appt = matched["appointment"]
        self.assertEqual(appt["start"], "09:00")

        status, queue = self.call("GET", f"/api/queue?date={DAY}&point_id=sp-center")
        self.assertEqual(status, 200)
        entries = queue["points"][0]["entries"]
        self.assertEqual([e["appointment_id"] for e in entries], [appt["id"]])

        status, checked = self.call("POST", f"/api/appointments/{appt['id']}/checkin",
                                    {"arrival": "09:04"})
        self.assertEqual(checked["appointment"]["status"], "checked_in")

        status, outcome = self.call("POST", f"/api/appointments/{appt['id']}/outcome", {
            "kind": "formal_referral", "referral_target": "市一医院心内科",
            "diagnosis": "高血压2级"})
        self.assertEqual(status, 200)
        self.assertEqual(outcome["followups"][0]["team_id"], "team-a")

        status, followups = self.call("GET", "/api/teams/team-a/followups")
        self.assertEqual(len(followups["followups"]), 1)

        status, health = self.call("GET", "/api/health-records/r1")
        self.assertEqual(health["entries"][0]["diagnosis"], "高血压2级")
        self.assertEqual(health["referrals"][0]["target"], "市一医院心内科")

        status, estimate = self.call("GET", "/api/riders/r1/cost-estimate?service=tcm_30m")
        self.assertEqual(estimate["out_of_pocket"], 12.0)
        self.assertIn("职工医保", "".join(estimate["sources"]))

        status, coverage = self.call("GET", f"/api/union/coverage?date={DAY}", role="union")
        self.assertEqual(status, 200)
        self.assertEqual(coverage["points"]["sp-center"]["completed"], 1)

    def test_role_enforcement(self):
        self.seed_shift()
        self.seed_rider()
        status, matched = self.call("POST", "/api/riders/r1/windows", {
            "date": DAY, "start": "09:00", "end": "09:30", "area": "城东"}, role="rider")
        appt_id = matched["appointment"]["id"]
        self.call("POST", f"/api/appointments/{appt_id}/outcome",
                  {"kind": "in_hospital", "diagnosis": "肩周炎"})

        status, _ = self.call("GET", "/api/queue?date=" + DAY, role=None)
        self.assertEqual(status, 401)
        status, _ = self.call("GET", "/api/health-records/r1", role="union")
        self.assertEqual(status, 403)
        status, _ = self.call("GET", f"/api/queue?date={DAY}", role="union")
        self.assertEqual(status, 403)
        status, _ = self.call("GET", "/api/identity/r1", role="clinician")
        self.assertEqual(status, 403)
        status, _ = self.call("GET", "/api/health-records/r1", role="verifier")
        self.assertEqual(status, 403)

        status, identity = self.call("POST", "/api/identity/verifications", {
            "rider_id": "r1", "method": "医保电子凭证", "result": "verified"},
            role="verifier")
        self.assertEqual(status, 201)
        status, fetched = self.call("GET", "/api/identity/r1", role="verifier")
        self.assertEqual(fetched["result"], "verified")
        status, coverage = self.call("GET", f"/api/union/coverage?date={DAY}", role="union")
        self.assertEqual(status, 200)
        self.assertNotIn("肩周炎", json.dumps(coverage, ensure_ascii=False))

    def test_validation_errors(self):
        self.seed_shift()
        self.seed_rider()
        status, err = self.call("POST", "/api/riders/r1/windows", {
            "date": DAY, "start": "9点", "end": "09:30", "area": "城东"}, role="rider")
        self.assertEqual(status, 400)
        status, err = self.call("POST", "/api/riders/r1/windows", {
            "date": DAY, "start": "09:00", "end": "09:30", "area": "城东",
            "order_id": "X123"}, role="rider")
        self.assertEqual(status, 400)
        self.assertIn("不读取配送订单", err["message"])
        status, _ = self.call("GET", "/api/riders/ghost")
        self.assertEqual(status, 404)
        status, _ = self.call("GET", "/api/queue")
        self.assertEqual(status, 400)

    def test_stale_version_conflict_over_http(self):
        self.seed_shift()
        self.seed_rider()
        _, matched = self.call("POST", "/api/riders/r1/windows", {
            "date": DAY, "start": "09:00", "end": "09:30", "area": "城东"}, role="rider")
        appt_id = matched["appointment"]["id"]
        window = {"date": DAY, "start": "09:30", "end": "10:00", "area": "城东"}
        status, _ = self.call("POST", f"/api/appointments/{appt_id}/reschedule",
                              {"reason": "temp_order", "window": window}, role="rider")
        self.assertEqual(status, 200)
        status, err = self.call("POST", f"/api/appointments/{appt_id}/reschedule",
                                {"reason": "temp_order", "expected_version": 1,
                                 "window": window}, role="rider")
        self.assertEqual(status, 409)

    def test_health_and_unknown_route(self):
        status, body = self.call("GET", "/health", role=None)
        self.assertEqual((status, body), (200, health_payload()))
        status, _ = self.call("GET", "/unknown", role=None)
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
