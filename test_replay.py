"""场景重放核对：容量、费用依据、医疗优先级与跨日随访连续性。"""
import json
import os
import unittest

from riderclinic import models as M
from riderclinic.app import App
from riderclinic.replay import load_scenario, run_scenario

SCENARIOS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenarios")


def run(name):
    return run_scenario(App(), load_scenario(os.path.join(SCENARIOS, name)))


def assert_no_overlap(test_case, appointments):
    taken = set()
    for appt in appointments:
        if appt["status"] not in ("scheduled", "checked_in", "completed"):
            continue
        start = M.to_min(appt["start"])
        for minute in range(start, start + appt["minutes"], M.GRID_MINUTES):
            key = (appt["doctor_id"], appt["date"], minute)
            test_case.assertNotIn(key, taken, f"槽位冲突：{key}")
            taken.add(key)


def find_appointment(summary, rider_id, status=None):
    for appt in summary["appointments"]:
        if appt["rider_id"] == rider_id and (status is None or appt["status"] == status):
            return appt
    return None


class TempOrdersReplayTest(unittest.TestCase):
    def test_temp_order_replay(self):
        summary = run("temp_orders.json")
        assert_no_overlap(self, summary["appointments"])
        moved = find_appointment(summary, "r1")
        self.assertEqual(moved["start"], "10:00")
        self.assertEqual(moved["history"][-1]["reason"], "temp_order")
        # 释放的 09:00 槽位回流给后来的骑手
        backfill = find_appointment(summary, "r4")
        self.assertEqual((backfill["status"], backfill["start"]), ("scheduled", "09:00"))
        capacity = summary["queues"]["2026-09-21"]["points"][0]["capacity"]
        self.assertEqual(capacity["total_slots"], 8)
        self.assertEqual(capacity["booked_slots"], 5)
        # 费用依据：职工医保与服务包版本在 sources 中注明来源
        estimates = [e["result"] for e in summary["log"] if e["op"] == "cost_estimate"]
        consult = estimates[0]
        self.assertEqual(consult["out_of_pocket"], 0.0)
        self.assertIn("职工医保", "".join(consult["sources"]))
        self.assertIn("plus-v2", "".join(consult["sources"]))
        tcm = estimates[1]
        self.assertEqual(tcm["out_of_pocket"], 40.0)  # 居民医保报销减免后余额的50%
        self.assertIn("居民医保", "".join(tcm["sources"]))


class DoctorChangeReplayTest(unittest.TestCase):
    def test_medical_priority_survives_doctor_change(self):
        summary = run("doctor_change.json")
        assert_no_overlap(self, summary["appointments"])
        urgent = find_appointment(summary, "r4")
        elevated = find_appointment(summary, "r3")
        self.assertEqual((urgent["status"], urgent["start"]), ("scheduled", "09:00"))
        self.assertEqual((elevated["status"], elevated["start"]), ("scheduled", "09:15"))
        self.assertEqual(urgent["doctor_id"], "d2")
        # 普通排队让位于医疗优先级：两位普通预约进入候补
        self.assertIsNotNone(find_appointment(summary, "r1", status="waiting"))
        self.assertIsNotNone(find_appointment(summary, "r2", status="waiting"))
        queue = summary["queues"]["2026-09-22"]["points"][0]
        self.assertEqual([e["rider_id"] for e in queue["entries"]], ["r4", "r3"])
        self.assertEqual([e["rider_id"] for e in queue["waiting"]], ["r1", "r2"])


class CrossDayFollowUpReplayTest(unittest.TestCase):
    def test_followup_continuity_across_days_and_points(self):
        summary = run("cross_day_followup.json")
        assert_no_overlap(self, summary["appointments"])
        self.assertEqual(len(summary["followups"]), 1)
        task = summary["followups"][0]
        self.assertEqual(task["kind"], "referral_check")
        self.assertEqual(task["team_id"], "team-a")       # 同一家庭医生团队接续
        self.assertEqual(task["due_date"], "2026-10-03")  # 跨日随访
        day2 = find_appointment(summary, "r1", status="scheduled")
        self.assertEqual(day2["point_id"], "sp-union")    # 跨站点到诊
        self.assertEqual(day2["date"], "2026-10-01")
        coverage_text = json.dumps(summary["coverage"], ensure_ascii=False)
        self.assertNotIn("高血压", coverage_text)          # 工会统计不含诊断


class ConcurrentRescheduleReplayTest(unittest.TestCase):
    def test_concurrent_reschedule_has_single_winner(self):
        summary = run("concurrent_reschedule.json")
        parallel = summary["log"][-1]
        self.assertEqual(parallel["op"], "parallel")
        oks = [r for r in parallel["results"] if r["ok"]]
        conflicts = [r for r in parallel["results"] if not r["ok"]]
        self.assertEqual(len(oks), 1)
        self.assertEqual(len(conflicts), 3)
        self.assertTrue(all(r["status"] == 409 for r in conflicts))
        appt = summary["appointments"][0]
        self.assertEqual(appt["version"], 2)
        self.assertEqual(appt["status"], "scheduled")
        assert_no_overlap(self, summary["appointments"])


if __name__ == "__main__":
    unittest.main()
