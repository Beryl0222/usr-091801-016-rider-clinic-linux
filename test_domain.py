"""领域规则测试：匹配、容量、优先级、改期、费用、随访与数据隔离。"""
import json
import threading
import unittest

from riderclinic import models as M
from riderclinic.app import App

DAY = "2026-09-21"
DAY2 = "2026-09-22"


def make_app(**shift):
    app = App()
    base = {"id": "s-d1", "doctor_id": "d1", "point_id": "sp-center", "date": DAY,
            "start": "09:00", "end": "10:00", "services": ["consult_15m", "tcm_30m"]}
    base.update(shift)
    app.create_shift(base)
    return app


def add_rider(app, rid="r1", area="城东", team="team-a",
              insurance="employee", package="plus-v2"):
    return app.register_rider({"id": rid, "name": f"骑手{rid}", "area": area,
                               "team_id": team, "insurance": insurance,
                               "package": package})


def book(app, rid, start="09:00", end="09:45", service="consult_15m", **extra):
    payload = {"date": DAY, "start": start, "end": end, "area": "城东",
               "service": service}
    payload.update(extra)
    return app.submit_window(rid, payload)


class MatchingTest(unittest.TestCase):
    def test_match_uses_window_and_area(self):
        app = make_app()
        add_rider(app)
        result = book(app, "r1", end="09:30")
        appt = result["appointment"]
        self.assertTrue(result["matched"])
        self.assertEqual(appt["point_id"], "sp-center")
        self.assertEqual(appt["start"], "09:00")
        self.assertEqual(appt["minutes"], 15)
        self.assertEqual(appt["doctor_id"], "d1")
        self.assertEqual(appt["team_id"], "team-a")

    def test_window_must_fit_service_duration(self):
        app = make_app()
        add_rider(app)
        with self.assertRaises(M.ApiError) as ctx:
            book(app, "r1", end="09:15", service="tcm_30m")
        self.assertEqual(ctx.exception.status, 400)

    def test_tcm_occupies_two_slots(self):
        app = make_app()
        add_rider(app)
        add_rider(app, "r2", team="team-b")
        tcm = book(app, "r1", end="09:30", service="tcm_30m")["appointment"]
        self.assertEqual(tcm["minutes"], 30)
        self.assertEqual((tcm["start"], tcm["end"]), ("09:00", "09:30"))
        nxt = book(app, "r2", end="09:45")["appointment"]
        self.assertEqual(nxt["start"], "09:30")

    def test_capacity_exhaustion_goes_waiting(self):
        app = make_app()
        for i in range(1, 5):
            add_rider(app, f"r{i}")
            self.assertTrue(book(app, f"r{i}", end="10:00")["matched"])
        add_rider(app, "r5")
        result = book(app, "r5", end="10:00")
        self.assertFalse(result["matched"])
        self.assertEqual(result["appointment"]["status"], "waiting")

    def test_no_double_booking_per_doctor(self):
        app = make_app()
        for i in range(1, 5):
            add_rider(app, f"r{i}")
            book(app, f"r{i}", end="10:00")
        taken = set()
        for appt in app.store.appointments.values():
            start = M.to_min(appt.start)
            for minute in range(start, start + appt.minutes, M.GRID_MINUTES):
                self.assertNotIn((appt.doctor_id, minute), taken)
                taken.add((appt.doctor_id, minute))

    def test_contracted_team_preferred(self):
        app = make_app()
        app.create_shift({"id": "s-d3", "doctor_id": "d3", "point_id": "sp-center",
                          "date": DAY, "start": "09:00", "end": "10:00",
                          "services": ["consult_15m"]})
        add_rider(app, "r1", team="team-a")
        add_rider(app, "r2", team="team-b")
        self.assertEqual(book(app, "r1", end="09:30")["appointment"]["doctor_id"], "d1")
        self.assertEqual(book(app, "r2", end="09:30")["appointment"]["doctor_id"], "d3")

    def test_order_details_rejected(self):
        app = make_app()
        add_rider(app)
        with self.assertRaises(M.ApiError) as ctx:
            book(app, "r1", end="09:30", order_id="X123")
        self.assertEqual(ctx.exception.status, 400)


class PriorityTest(unittest.TestCase):
    def test_urgent_preempts_routine(self):
        app = make_app(id="s-d1", end="09:15", services=["consult_15m"])
        add_rider(app)
        add_rider(app, "r2")
        first = book(app, "r1", end="09:15")["appointment"]
        urgent = book(app, "r2", end="09:15", priority=2,
                      priority_reason="胸痛待排查")["appointment"]
        self.assertEqual(urgent["status"], "scheduled")
        self.assertEqual(urgent["start"], "09:00")
        routine = app.get_appointment(first["id"])
        self.assertEqual(routine["status"], "waiting")
        self.assertEqual(routine["history"][-1]["reason"], "preempted")

    def test_waitlist_ordered_by_medical_priority(self):
        app = make_app(id="s-d1", end="09:15", services=["consult_15m"])
        add_rider(app)
        add_rider(app, "r2")
        add_rider(app, "r3")
        book(app, "r1", end="09:15")
        routine = book(app, "r2", end="09:15")["appointment"]
        elevated = book(app, "r3", end="09:15", priority=1,
                        priority_reason="血压偏高复测")["appointment"]
        self.assertEqual(routine["status"], "waiting")
        self.assertEqual(elevated["status"], "scheduled")  # 医疗优先级置换普通排队
        self.assertEqual(app.get_appointment(routine["id"])["status"], "waiting")

    def test_drain_waitlist_on_new_capacity(self):
        app = make_app(id="s-d1", end="09:15", services=["consult_15m"])
        add_rider(app)
        add_rider(app, "r2")
        book(app, "r1", end="09:15")
        waiting = book(app, "r2", end="09:15")["appointment"]
        self.assertEqual(waiting["status"], "waiting")
        app.create_shift({"id": "s-d2", "doctor_id": "d2", "point_id": "sp-center",
                          "date": DAY, "start": "09:00", "end": "09:15",
                          "services": ["consult_15m"]})
        placed = app.get_appointment(waiting["id"])
        self.assertEqual(placed["status"], "scheduled")
        self.assertEqual(placed["doctor_id"], "d2")


class RescheduleTest(unittest.TestCase):
    def test_temp_order_releases_slot_and_moves(self):
        app = make_app(end="09:15", services=["consult_15m"])
        app.create_shift({"id": "s-pm", "doctor_id": "d1", "point_id": "sp-center",
                          "date": DAY, "start": "14:00", "end": "15:00",
                          "services": ["consult_15m"]})
        add_rider(app)
        add_rider(app, "r2")
        first = book(app, "r1", end="09:15")["appointment"]
        waiting = book(app, "r2", end="09:15")["appointment"]
        self.assertEqual(waiting["status"], "waiting")
        moved = app.reschedule(first["id"], {
            "reason": "temp_order",
            "window": {"date": DAY, "start": "14:00", "end": "14:30", "area": "城东"},
        })["appointment"]
        self.assertEqual(moved["start"], "14:00")
        self.assertEqual(moved["history"][-1]["reason"], "temp_order")
        placed = app.get_appointment(waiting["id"])
        self.assertEqual((placed["status"], placed["start"]), ("scheduled", "09:00"))

    def test_late_within_grace_keeps_slot(self):
        app = make_app()
        add_rider(app)
        appt = book(app, "r1", end="09:30")["appointment"]
        result = app.checkin(appt["id"], {"arrival": "09:09"})["appointment"]
        self.assertEqual(result["status"], "checked_in")
        self.assertTrue(result["late"])
        self.assertEqual(result["start"], "09:00")

    def test_late_beyond_grace_moves_to_next_slot(self):
        app = make_app()
        add_rider(app)
        add_rider(app, "r2")
        book(app, "r1", end="09:30")          # 09:00
        second = book(app, "r2", end="09:30")["appointment"]  # 09:15
        result = app.checkin(second["id"], {"arrival": "09:30"})["appointment"]
        self.assertEqual(result["status"], "checked_in")
        self.assertTrue(result["late"])
        self.assertEqual(result["start"], "09:30")
        self.assertEqual(result["history"][-1]["reason"], "rider_late")

    def test_doctor_cancel_reallocates_by_priority(self):
        app = make_app(end="09:30", services=["consult_15m"])
        add_rider(app)
        add_rider(app, "r2")
        routine = book(app, "r1", end="09:30")["appointment"]
        urgent = book(app, "r2", end="09:30", priority=2,
                      priority_reason="胸痛待排查")["appointment"]
        app.create_shift({"id": "s-d2", "doctor_id": "d2", "point_id": "sp-center",
                          "date": DAY, "start": "09:00", "end": "09:15",
                          "services": ["consult_15m"]})
        report = app.change_shift("s-d1", {"action": "cancel"})
        self.assertEqual(sorted(report["displaced"]), sorted([routine["id"], urgent["id"]]))
        urgent_now = app.get_appointment(urgent["id"])
        self.assertEqual((urgent_now["status"], urgent_now["start"]), ("scheduled", "09:00"))
        self.assertEqual(urgent_now["doctor_id"], "d2")
        self.assertEqual(app.get_appointment(routine["id"])["status"], "waiting")

    def test_doctor_reassign_keeps_time(self):
        app = make_app()
        add_rider(app)
        appt = book(app, "r1", end="09:30")["appointment"]
        app.change_shift("s-d1", {"action": "reassign", "doctor_id": "d3"})
        now = app.get_appointment(appt["id"])
        self.assertEqual(now["start"], "09:00")
        self.assertEqual(now["doctor_id"], "d3")
        self.assertEqual(now["team_id"], "team-b")

    def test_version_conflict(self):
        app = make_app()
        add_rider(app)
        appt = book(app, "r1", end="09:30")["appointment"]
        with self.assertRaises(M.ApiError) as ctx:
            app.reschedule(appt["id"], {"reason": "temp_order", "expected_version": 99,
                                        "window": {"date": DAY, "start": "09:30",
                                                   "end": "10:00", "area": "城东"}})
        self.assertEqual(ctx.exception.status, 409)

    def test_concurrent_reschedule_single_winner(self):
        app = make_app()
        app.create_shift({"id": "s-pm", "doctor_id": "d1", "point_id": "sp-center",
                          "date": DAY, "start": "14:00", "end": "15:00",
                          "services": ["consult_15m"]})
        add_rider(app)
        appt = book(app, "r1", end="09:30")["appointment"]
        barrier = threading.Barrier(8)
        outcomes = []

        def worker():
            barrier.wait()
            try:
                app.reschedule(appt["id"], {
                    "reason": "temp_order", "expected_version": 1,
                    "window": {"date": DAY, "start": "14:00", "end": "14:30", "area": "城东"}})
                outcomes.append("ok")
            except M.ApiError as err:
                outcomes.append(err.status)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes.count("ok"), 1)
        self.assertEqual(outcomes.count(409), 7)
        self.assertEqual(app.get_appointment(appt["id"])["version"], 2)

    def test_concurrent_booking_respects_capacity(self):
        app = make_app(end="09:15", services=["consult_15m"])
        add_rider(app)
        add_rider(app, "r2")
        barrier = threading.Barrier(2)
        results = {}

        def worker(rid):
            barrier.wait()
            results[rid] = book(app, rid, end="09:15")["matched"]

        threads = [threading.Thread(target=worker, args=(rid,)) for rid in ("r1", "r2")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results.values()), [False, True])


class BillingTest(unittest.TestCase):
    def test_employee_insurance_with_plus_package(self):
        app = App()
        add_rider(app)
        estimate = app.cost_estimate("r1", "tcm_30m")
        self.assertEqual(estimate["gross"], 80.0)
        self.assertEqual(estimate["package_cover"], 40.0)
        self.assertEqual(estimate["insurance_cover"], 28.0)
        self.assertEqual(estimate["out_of_pocket"], 12.0)
        text = "".join(estimate["sources"])
        self.assertIn("职工医保", text)
        self.assertIn("plus-v2", text)

    def test_resident_insurance_with_basic_package(self):
        app = App()
        add_rider(app, insurance="resident", package="basic-v1")
        estimate = app.cost_estimate("r1", "consult_15m")
        self.assertEqual(estimate["package_cover"], 30.0)
        self.assertEqual(estimate["out_of_pocket"], 0.0)
        self.assertIn("居民医保", "".join(estimate["sources"]))

    def test_no_insurance_full_out_of_pocket(self):
        app = App()
        add_rider(app, insurance="none", package="none")
        estimate = app.cost_estimate("r1", "consult_15m")
        self.assertEqual(estimate["out_of_pocket"], 30.0)
        self.assertIn("无医保", "".join(estimate["sources"]))

    def test_estimate_total_consistent(self):
        app = App()
        add_rider(app)
        for service in M.SERVICES:
            estimate = app.cost_estimate("r1", service)
            total = (estimate["package_cover"] + estimate["insurance_cover"]
                     + estimate["out_of_pocket"])
            self.assertAlmostEqual(total, estimate["gross"])


class FollowUpTest(unittest.TestCase):
    def test_referral_creates_team_followup_across_stations(self):
        app = make_app()
        app.create_shift({"id": "s-d4", "doctor_id": "d4", "point_id": "sp-union",
                          "date": DAY2, "start": "14:00", "end": "16:00",
                          "services": ["outreach", "consult_15m"]})
        add_rider(app)
        appt = book(app, "r1", end="09:30")["appointment"]
        app.checkin(appt["id"], {"arrival": "09:02"})
        outcome = app.record_outcome(appt["id"], {
            "kind": "formal_referral", "referral_target": "市一医院心内科",
            "diagnosis": "高血压2级"})
        self.assertEqual(len(outcome["followups"]), 1)
        task = outcome["followups"][0]
        self.assertEqual(task["kind"], "referral_check")
        self.assertEqual(task["team_id"], "team-a")
        self.assertEqual(task["due_date"], M.add_days(DAY, 3))
        # 次日跨站点到诊（工会驿站），随访仍归属原签约团队
        day2 = app.submit_window("r1", {"date": DAY2, "start": "14:00", "end": "15:00",
                                        "area": "城东", "service": "outreach"})
        self.assertEqual(day2["appointment"]["point_id"], "sp-union")
        team_tasks = app.team_followups("team-a", due_before=M.add_days(DAY, 4))
        self.assertEqual([t["id"] for t in team_tasks], [task["id"]])
        done = app.complete_followup(task["id"], {"note": "已电话随访"})
        self.assertEqual(done["followup"]["status"], "done")

    def test_cross_day_due_date(self):
        app = App()
        app.create_shift({"id": "s1", "doctor_id": "d1", "point_id": "sp-center",
                          "date": "2026-09-30", "start": "09:00", "end": "10:00",
                          "services": ["consult_15m"]})
        add_rider(app)
        appt = app.submit_window("r1", {"date": "2026-09-30", "start": "09:00",
                                        "end": "09:30", "area": "城东"})["appointment"]
        outcome = app.record_outcome(appt["id"], {"kind": "formal_referral",
                                                  "referral_target": "市一医院"})
        self.assertEqual(outcome["followups"][0]["due_date"], "2026-10-03")

    def test_tcm_outcome_creates_review_task(self):
        app = make_app()
        add_rider(app)
        appt = book(app, "r1", end="09:30", service="tcm_30m")["appointment"]
        outcome = app.record_outcome(appt["id"], {"kind": "station_intervention"})
        self.assertEqual(outcome["followups"][0]["kind"], "tcm_review")
        self.assertEqual(outcome["followups"][0]["due_date"], M.add_days(DAY, 7))


class PrivacyTest(unittest.TestCase):
    def test_identity_stored_separately_from_health(self):
        app = make_app()
        add_rider(app)
        app.record_identity_verification({"rider_id": "r1", "method": "医保电子凭证",
                                          "result": "verified"})
        appt = book(app, "r1", end="09:30")["appointment"]
        app.record_outcome(appt["id"], {"kind": "in_hospital", "diagnosis": "腰椎间盘突出"})
        identity = app.get_identity("r1")
        health = app.get_health_record("r1")
        self.assertNotIn("diagnosis", identity)
        self.assertEqual(health["entries"][0]["diagnosis"], "腰椎间盘突出")
        self.assertNotIn(identity["id"], json.dumps(health, ensure_ascii=False))
        self.assertTrue(app.get_rider("r1")["insurance_verified"])

    def test_union_coverage_is_deidentified(self):
        app = make_app()
        add_rider(app)
        appt = book(app, "r1", end="09:30")["appointment"]
        app.record_outcome(appt["id"], {"kind": "in_hospital", "diagnosis": "腰椎间盘突出"})
        coverage = app.union_coverage(DAY)
        text = json.dumps(coverage, ensure_ascii=False)
        self.assertNotIn("r1", text)
        self.assertNotIn("骑手", text)
        self.assertNotIn("腰椎间盘突出", text)
        self.assertEqual(coverage["points"]["sp-center"]["booked"], 1)
        self.assertEqual(coverage["points"]["sp-center"]["completed"], 1)


class QueueTest(unittest.TestCase):
    def test_executable_queue_order_and_capacity(self):
        app = make_app()
        add_rider(app)
        add_rider(app, "r2")
        book(app, "r1", end="09:45")
        book(app, "r2", end="09:45", priority=1, priority_reason="血压偏高复测")
        queue = app.queue(DAY, "sp-center")
        point = queue["points"][0]
        starts = [entry["start"] for entry in point["entries"]]
        self.assertEqual(starts, ["09:00", "09:15"])
        capacity = point["capacity"]
        self.assertEqual(capacity["total_slots"], 4)
        self.assertEqual(capacity["booked_slots"], 2)
        self.assertEqual(capacity["bookable"]["consult_15m"], 2)
        self.assertEqual(capacity["bookable"]["tcm_30m"], 1)


class IdempotencyTest(unittest.TestCase):
    def test_replayed_request_returns_same_result(self):
        app = make_app()
        add_rider(app)
        payload = {"date": DAY, "start": "09:00", "end": "09:30", "area": "城东",
                   "request_id": "req-1"}
        first = app.submit_window("r1", payload)
        second = app.submit_window("r1", dict(payload))
        self.assertEqual(first["appointment"]["id"], second["appointment"]["id"])
        self.assertEqual(len(app.store.appointments), 1)

    def test_replayed_reschedule_after_state_change(self):
        app = make_app()
        app.create_shift({"id": "s-pm", "doctor_id": "d1", "point_id": "sp-center",
                          "date": DAY, "start": "14:00", "end": "15:00",
                          "services": ["consult_15m"]})
        add_rider(app)
        appt = book(app, "r1", end="09:30")["appointment"]
        payload = {"reason": "temp_order", "expected_version": 1, "request_id": "req-9",
                   "window": {"date": DAY, "start": "14:00", "end": "14:30", "area": "城东"}}
        first = app.reschedule(appt["id"], payload)
        self.assertEqual(first["appointment"]["version"], 2)
        # 状态已变化（版本号不再匹配），重放仍返回首次结果而非 409
        second = app.reschedule(appt["id"], dict(payload))
        self.assertEqual(second["appointment"]["id"], first["appointment"]["id"])
        self.assertEqual(app.get_appointment(appt["id"])["version"], 2)


if __name__ == "__main__":
    unittest.main()
