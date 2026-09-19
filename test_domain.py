"""领域场景测试：匹配、容量、优先级、重排、费用、处置、随访与重放核对。"""

import unittest

from clinic import catalog
from clinic.clock import day_key, parse_minutes
from clinic.engine import ClinicService
from clinic.errors import ClinicError, Conflict
from clinic.replay import audit

from scenarios import seed, t

DAY = "2026-09-20"


def open_baseline_slots(svc):
    """A 片区中心与站各开一个接诊号源，站开一个中医号源；容量均为 1。"""
    svc.open_slot("slot-center-am", "loc-center-a", "consult", "doc-li",
                  t(f"{DAY}T09:00"), t(f"{DAY}T12:00"), 1)
    svc.open_slot("slot-station-am", "loc-station-a", "consult", "doc-wang",
                  t(f"{DAY}T09:00"), t(f"{DAY}T12:00"), 1)
    svc.open_slot("slot-tcm-am", "loc-station-a", "tcm", "doc-zhao",
                  t(f"{DAY}T09:00"), t(f"{DAY}T12:00"), 1)
    svc.open_slot("slot-center-pm", "loc-center-a", "consult", "doc-li",
                  t(f"{DAY}T13:00"), t(f"{DAY}T17:00"), 1)


def register_and_verify(svc, rider_id, zone="A", insurance="employee",
                        package="none", team="team-1"):
    svc.register_rider(rider_id, zone, insurance, package, team)
    svc.verify_identity(rider_id, f"ID-{rider_id}-ABCD", "face",
                        t(f"{DAY}T08:00"), name_masked="张*")
    return svc.store.profiles[rider_id]


def submit_and_match(svc, request_id, rider_id, service="consult",
                     ws="09:00", we="10:00"):
    svc.submit_request(request_id, rider_id, service,
                       t(f"{DAY}T{ws}"), t(f"{DAY}T{we}"))
    return svc.match_request(request_id)["appointment"]


class MatchingTest(unittest.TestCase):
    def setUp(self):
        self.svc = seed()
        open_baseline_slots(self.svc)
        register_and_verify(self.svc, "r1")

    def test_matches_zone_location_and_duration(self):
        appt = submit_and_match(self.svc, "q1", "r1")
        self.assertEqual(appt["slot_id"], "slot-center-am")
        self.assertEqual(appt["end"] - appt["start"], 15)
        self.assertEqual(appt["status"], catalog.ST_BOOKED)

    def test_tcm_cannot_be_held_at_union_post(self):
        with self.assertRaises(ClinicError):
            self.svc.open_slot("bad-tcm", "loc-post-a", "tcm", "doc-zhao",
                               t(f"{DAY}T09:00"), t(f"{DAY}T11:00"), 1)

    def test_other_zone_slot_not_used(self):
        register_and_verify(self.svc, "r2", zone="B")
        # B 片区无号源：不可跨片区占用 A 的号
        self.svc.submit_request("q2", "r2", "consult",
                                t(f"{DAY}T09:00"), t(f"{DAY}T10:00"))
        with self.assertRaises(ClinicError) as ctx:
            self.svc.match_request("q2")
        self.assertEqual(ctx.exception.code, "no_capacity")

    def test_unverified_identity_blocks_match(self):
        self.svc.register_rider("rx", "A", "employee", "none", "team-1")
        self.svc.submit_request("qx", "rx", "consult",
                                t(f"{DAY}T09:00"), t(f"{DAY}T10:00"))
        with self.assertRaises(ClinicError) as ctx:
            self.svc.match_request("qx")
        self.assertEqual(ctx.exception.code, "identity_unverified")

    def test_daily_queue_is_executable_and_urgent_first(self):
        register_and_verify(self.svc, "r2")
        # r1 宽窗口占中心 09:30；r2 窄窗口与其冲突，落到站点号源
        a1 = submit_and_match(self.svc, "q1", "r1", ws="09:30", we="10:30")
        a2 = submit_and_match(self.svc, "q2", "r2", ws="09:30", we="09:45")
        self.assertEqual(a1["slot_id"], "slot-center-am")
        self.assertEqual(a2["slot_id"], "slot-station-am")

        self.svc.submit_request("q3", "r1", "tcm",
                                t(f"{DAY}T09:00"), t(f"{DAY}T10:00"))
        self.svc.triage("q3", catalog.PRIORITY_URGENT)
        a3 = self.svc.match_request("q3")["appointment"]
        self.assertEqual(a3["service_code"], "tcm")

        queue = self.svc.daily_queue("A", DAY)
        self.assertEqual(len(queue["slots"]), 4)  # 中心上下午、站接诊、站中医
        for slot in queue["slots"]:
            keys = [(a["start"], 0 if a["priority"] == "urgent" else 1)
                    for a in slot["appointments"]]
            self.assertEqual(keys, sorted(keys))
            # 同一开始时间，紧急必须排在普通之前
            for earlier, later in zip(slot["appointments"], slot["appointments"][1:]):
                if earlier["start"] == later["start"]:
                    self.assertEqual(earlier["priority"], catalog.PRIORITY_URGENT)
        self.assertEqual(queue["needs_resolution"], [])
        # 队列条目不含证件与诊断
        self.assertNotIn("id_ref", str(queue))
        self.assertNotIn("diagnosis", str(queue))


class CapacityAndPriorityTest(unittest.TestCase):
    def setUp(self):
        self.svc = seed()
        open_baseline_slots(self.svc)
        register_and_verify(self.svc, "r1")
        register_and_verify(self.svc, "r2")
        register_and_verify(self.svc, "r3")

    def test_capacity_blocks_third_normal(self):
        # 窗口恰为一个服务单元，两号源各只能放一个
        submit_and_match(self.svc, "q1", "r1", ws="09:00", we="09:15")
        submit_and_match(self.svc, "q2", "r2", ws="09:00", we="09:15")
        self.svc.submit_request("q3", "r3", "consult",
                                t(f"{DAY}T09:00"), t(f"{DAY}T09:15"))
        with self.assertRaises(ClinicError) as ctx:
            self.svc.match_request("q3")
        self.assertEqual(ctx.exception.code, "no_capacity")

    def test_urgent_bumps_normal_and_victim_is_rebooked(self):
        # r1 窗口较宽落在中心 09:00；r2 窄窗口落在站点 09:00
        a1 = submit_and_match(self.svc, "q1", "r1", ws="09:00", we="09:30")
        a2 = submit_and_match(self.svc, "q2", "r2", ws="09:00", we="09:15")
        self.assertEqual(a1["slot_id"], "slot-center-am")
        self.assertEqual(a2["slot_id"], "slot-station-am")

        # 中心 09:00 被普通约诊占用；r3 紧急窄窗，只能驱逐 r1
        self.svc.submit_request("q3", "r3", "consult",
                                t(f"{DAY}T09:00"), t(f"{DAY}T09:15"))
        self.svc.triage("q3", catalog.PRIORITY_URGENT)
        result = self.svc.match_request("q3")
        urgent_appt = result["appointment"]
        self.assertEqual(urgent_appt["priority"], catalog.PRIORITY_URGENT)
        self.assertEqual(urgent_appt["slot_id"], "slot-center-am")
        self.assertEqual(urgent_appt["start"], t(f"{DAY}T09:00"))
        # 被驱逐的 r1 按其宽窗口自动重排到 09:15，绝不静默丢失
        self.assertEqual(len(result["displaced"]), 1)
        self.assertTrue(result["displaced"][0]["rebooked"])
        r1_req = self.svc.store.requests["q1"]
        self.assertEqual(r1_req["status"], "matched")
        rebooked = self.svc.store.appointments[result["displaced"][0]["appointment_id"]]
        self.assertEqual(rebooked["start"], t(f"{DAY}T09:15"))
        # 驱逐原因留痕
        self.assertIn("bumped_by_urgent", str(self.svc.store.appointments[a1["appt_id"]]["history"]))

    def test_urgent_cannot_be_bumped_when_only_urgent_present(self):
        # 两个紧急请求先后占满中心与站点号源（窗口恰为一个单元，无法错峰）
        self.svc.submit_request("q1", "r1", "consult",
                                t(f"{DAY}T09:00"), t(f"{DAY}T09:15"))
        self.svc.triage("q1", catalog.PRIORITY_URGENT)
        a1 = self.svc.match_request("q1")["appointment"]
        self.assertEqual(a1["slot_id"], "slot-center-am")

        self.svc.submit_request("q2", "r2", "consult",
                                t(f"{DAY}T09:00"), t(f"{DAY}T09:15"))
        self.svc.triage("q2", catalog.PRIORITY_URGENT)
        a2 = self.svc.match_request("q2")["appointment"]
        self.assertEqual(a2["slot_id"], "slot-station-am")

        # 第三个紧急请求：两号源同段均为紧急，无普通可驱逐
        self.svc.submit_request("q3", "r3", "consult",
                                t(f"{DAY}T09:00"), t(f"{DAY}T09:15"))
        self.svc.triage("q3", catalog.PRIORITY_URGENT)
        with self.assertRaises(ClinicError) as ctx:
            self.svc.match_request("q3")
        self.assertEqual(ctx.exception.code, "no_capacity")
        # 原两个紧急约诊原样在位
        self.assertEqual(self.svc.store.appointments[a1["appt_id"]]["status"],
                         catalog.ST_BOOKED)
        self.assertEqual(self.svc.store.appointments[a2["appt_id"]]["status"],
                         catalog.ST_BOOKED)


class RescheduleTest(unittest.TestCase):
    def setUp(self):
        self.svc = seed()
        open_baseline_slots(self.svc)
        register_and_verify(self.svc, "r1")

    def test_late_within_grace_is_placed(self):
        appt = submit_and_match(self.svc, "q1", "r1", ws="09:00", we="09:45")
        out = self.svc.record_arrival(appt["appt_id"], t(f"{DAY}T09:03"))
        self.assertEqual(out["result"], "placed")
        self.assertEqual(self.svc.store.appointments[appt["appt_id"]]["status"],
                         catalog.ST_ARRIVED)

    def test_late_within_limit_rebooks_after_arrival(self):
        appt = submit_and_match(self.svc, "q1", "r1", ws="09:00", we="09:45")
        out = self.svc.record_arrival(appt["appt_id"], t(f"{DAY}T09:10"))
        self.assertEqual(out["result"], "rebooked_late")
        new_appt = self.svc.store.appointments[out["appointment_id"]]
        self.assertGreaterEqual(new_appt["start"], t(f"{DAY}T09:10"))
        self.assertEqual(new_appt["end"] - new_appt["start"], 15)
        self.assertEqual(new_appt["status"], catalog.ST_ARRIVED)

    def test_late_beyond_limit_is_no_show_and_releases_capacity(self):
        appt = submit_and_match(self.svc, "q1", "r1", ws="09:00", we="09:45")
        out = self.svc.record_arrival(appt["appt_id"], t(f"{DAY}T09:21"))
        self.assertEqual(out["result"], "no_show")
        self.assertEqual(self.svc.store.appointments[appt["appt_id"]]["status"],
                         catalog.ST_NOSHOW)
        # 号源释放：同号源同段可再排
        register_and_verify(self.svc, "r2")
        appt2 = submit_and_match(self.svc, "q2", "r2", ws="09:00", we="09:30")
        self.assertEqual(appt2["slot_id"], "slot-center-am")

    def test_rider_grab_order_requeues_with_new_window(self):
        appt = submit_and_match(self.svc, "q1", "r1", ws="09:00", we="09:30")
        out = self.svc.rider_grab_order(
            appt["appt_id"],
            t(f"{DAY}T13:00"), t(f"{DAY}T14:00"))
        self.assertEqual(out["result"], "requeued")
        self.assertTrue(out["rebooked"])
        new_appt = self.svc.store.appointments[out["appointment_id"]]
        self.assertEqual(new_appt["slot_id"], "slot-center-pm")

    def test_rider_grab_order_without_window_cancels(self):
        appt = submit_and_match(self.svc, "q1", "r1", ws="09:00", we="09:30")
        out = self.svc.rider_grab_order(appt["appt_id"])
        self.assertEqual(out["result"], "cancelled")
        self.assertEqual(self.svc.store.appointments[appt["appt_id"]]["status"],
                         catalog.ST_CANCELLED)

    def test_practitioner_reschedule_rebooks_affected(self):
        appt = submit_and_match(self.svc, "q1", "r1", ws="09:00", we="10:00")
        version = self.svc.store.slots["slot-center-am"]["version"]
        out = self.svc.adjust_slot(
            "slot-center-am",
            new_start=t(f"{DAY}T10:30"), new_end=t(f"{DAY}T12:00"),
            expected_version=version)
        # 原窗口 9-10 与新号源 10:30-12 不相交 → 落到站点号源
        results = out["rebookings"]
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["rebooked"])
        new_appt = self.svc.store.appointments[results[0]["appointment_id"]]
        self.assertEqual(new_appt["slot_id"], "slot-station-am")

    def test_stale_slot_version_conflicts(self):
        submit_and_match(self.svc, "q1", "r1", ws="09:00", we="10:00")
        self.svc.adjust_slot("slot-center-am",
                             new_start=t(f"{DAY}T09:30"), new_end=t(f"{DAY}T12:00"))
        with self.assertRaises(Conflict):
            self.svc.adjust_slot(
                "slot-center-am",
                new_start=t(f"{DAY}T10:00"), new_end=t(f"{DAY}T12:00"),
                expected_version=1)


class DispositionFeeAndPrivacyTest(unittest.TestCase):
    def setUp(self):
        self.svc = seed()
        open_baseline_slots(self.svc)

    def _arrive(self, rider_id, insurance="employee", package="none"):
        register_and_verify(self.svc, rider_id, insurance=insurance, package=package)
        appt = submit_and_match(self.svc, f"q-{rider_id}", rider_id,
                                ws="09:00", we="09:30")
        self.svc.record_arrival(appt["appt_id"], t(f"{DAY}T09:02"))
        return appt["appt_id"]

    def test_fee_snapshot_differs_by_insurance_and_package(self):
        a_emp = self._arrive("e1", insurance="employee")
        a_res = self._arrive("e2", insurance="resident")
        a_pkg = self._arrive("e3", insurance="employee", package="fd-v1")
        self.assertEqual(
            self.svc.store.appointments[a_emp]["fee_snapshot"]["self_fen"], 300)
        self.assertEqual(self.svc.store.appointments[a_res]["fee_snapshot"]["covered_fen"], 900)
        pkg_snap = self.svc.store.appointments[a_pkg]["fee_snapshot"]
        self.assertEqual(pkg_snap["self_fen"], 100)
        self.assertIn("服务包", pkg_snap["basis"])
        self.assertIn("package:fd-v1", pkg_snap["source"])

    def test_dispositions_and_referral_fee_line(self):
        appt_id = self._arrive("e1")
        enc = self.svc.complete_encounter(
            appt_id, catalog.DISPO_REFERRAL, "J06.9",
            at=t(f"{DAY}T09:15"))
        self.assertEqual(enc["disposition"], catalog.DISPO_REFERRAL)
        kinds = [line["kind"] for line in enc["fee_lines"]]
        self.assertEqual(kinds, ["primary", "referral"])
        for line in enc["fee_lines"]:
            self.assertTrue(line["source"])
            self.assertTrue(line["basis"])
            self.assertEqual(line["covered_fen"] + line["self_fen"], line["total_fen"])

        appt2 = self._arrive("e2")
        enc2 = self.svc.complete_encounter(
            appt2, catalog.DISPO_POST, "M54.5", at=t(f"{DAY}T10:15"))
        self.assertEqual(enc2["disposition"], catalog.DISPO_POST)

    def test_identity_and_health_records_are_separate(self):
        appt_id = self._arrive("e1")
        self.svc.complete_encounter(appt_id, catalog.DISPO_INHOUSE, "J02.9",
                                    at=t(f"{DAY}T09:20"))
        identity = self.svc.identity_view("e1")
        health = self.svc.health_view("e1")
        self.assertIn("id_ref", identity)
        self.assertNotIn("diagnosis", str(identity))
        self.assertNotIn("id_ref", str(health))
        self.assertEqual(health["encounters"][0]["diagnosis_code"], "J02.9")
        # 两个存储物理分离
        self.assertNotIn("id_ref", self.svc.store.profiles["e1"])
        self.assertNotIn("diagnosis_code", self.svc.store.identities["e1"])

    def test_union_sees_only_deidentified_coverage(self):
        appt_id = self._arrive("e1")
        self.svc.complete_encounter(appt_id, catalog.DISPO_INHOUSE, "J02.9",
                                    at=t(f"{DAY}T09:20"))
        stats = self.svc.union_coverage(
            t(f"{DAY}T00:00"), t("2026-09-21T00:00"))
        self.assertEqual(stats["visits"], 1)
        self.assertEqual(stats["distinct_riders"], 1)
        self.assertEqual(stats["by_service"]["consult"], 1)
        self.assertNotIn("e1", str(stats))
        self.assertNotIn("J02.9", str(stats))


class FollowupContinuityTest(unittest.TestCase):
    def setUp(self):
        self.svc = seed()
        open_baseline_slots(self.svc)
        register_and_verify(self.svc, "r1")
        self.appt = submit_and_match(self.svc, "q1", "r1", ws="09:00", we="09:30")
        self.svc.record_arrival(self.appt["appt_id"], t(f"{DAY}T09:02"))
        self.enc = self.svc.complete_encounter(
            self.appt["appt_id"], catalog.DISPO_FOLLOWUP, "I10",
            at=t(f"{DAY}T09:20"))

    def test_followup_due_next_day_stays_with_same_team_across_sites(self):
        fu = self.svc.schedule_followup(
            self.appt["appt_id"], "consult", t("2026-09-21T09:00"))
        self.assertEqual(fu["team_id"], "team-1")
        self.assertEqual(day_key(fu["due"]), "2026-09-21")

        # 骑手跨站点到 B 片区
        self.svc.transfer_rider_site("r1", "B")
        self.assertEqual(self.svc.store.profiles["r1"]["zone"], "B")
        fu_after = self.svc.store.followups[fu["followup_id"]]
        self.assertEqual(fu_after["team_id"], "team-1")       # 团队不变
        self.assertEqual(fu_after["site_zone"], "B")         # 站点标签跟随
        change = [h for h in fu_after["history"] if h["event"] == "site_changed"]
        self.assertEqual(change[-1]["from_zone"], "A")
        self.assertTrue(change[-1]["team_unchanged"])

        # 跨日仍由同团队任务列表接续
        team_view = self.svc.team_followups("team-1")
        self.assertEqual([f["followup_id"] for f in team_view], [fu["followup_id"]])
        self.svc.complete_followup(fu["followup_id"], t("2026-09-21T09:05"))
        self.assertEqual(self.svc.store.followups[fu["followup_id"]]["status"],
                         "completed")


class ReplayAuditTest(unittest.TestCase):
    def test_full_scenario_audit_passes(self):
        svc = seed()
        open_baseline_slots(svc)
        register_and_verify(svc, "r1")
        register_and_verify(svc, "r2", insurance="resident")
        a1 = submit_and_match(svc, "q1", "r1", ws="09:00", we="09:30")
        a2 = submit_and_match(svc, "q2", "r2", ws="13:00", we="14:00")
        svc.record_arrival(a1["appt_id"], t(f"{DAY}T09:02"))
        svc.complete_encounter(a1["appt_id"], catalog.DISPO_FOLLOWUP, "I10",
                               at=t(f"{DAY}T09:20"))
        svc.schedule_followup(a1["appt_id"], "consult", t("2026-09-21T09:00"))
        svc.transfer_rider_site("r1", "B")
        report = audit(svc.store.events)
        self.assertTrue(report["ok"], report)
        for name, section in report["sections"].items():
            self.assertTrue(section["ok"], (name, section["findings"]))

    def test_audit_detects_capacity_and_priority_violations(self):
        events = [
            {"seq": 1, "type": "rider_registered", "payload": {
                "rider_id": "u1", "zone": "A", "insurance": "employee",
                "package_version": "none", "team_id": "team-1"}},
            {"seq": 2, "type": "rider_registered", "payload": {
                "rider_id": "u2", "zone": "A", "insurance": "employee",
                "package_version": "none", "team_id": "team-1"}},
            {"seq": 3, "type": "location_registered", "payload": {
                "location_id": "l1", "name": "中心", "type": "center", "zone": "A"}},
            {"seq": 4, "type": "practitioner_registered", "payload": {
                "practitioner_id": "d1", "name": "李医生", "team_id": "team-1"}},
            {"seq": 5, "type": "slot_opened", "payload": {
                "slot_id": "s1", "location_id": "l1", "service_code": "consult",
                "practitioner_id": "d1", "start": t(f"{DAY}T09:00"),
                "end": t(f"{DAY}T12:00"), "capacity": 1}},
        ]
        snap = {**catalog.fee_rule("consult", "employee", "none"),
                "service_name": "十五分钟接诊"}
        seq = 5
        for i, rider in enumerate(("u1", "u2"), start=6):
            seq += 1
            events.append({"seq": seq, "type": "request_submitted", "payload": {
                "request_id": f"q{i}", "rider_id": rider, "service_code": "consult",
                "window_start": t(f"{DAY}T09:00"), "window_end": t(f"{DAY}T10:00"),
                "zone": "A"}})
            seq += 1
            events.append({"seq": seq, "type": "appointment_matched", "payload": {
                "appt_id": f"a{i}", "request_id": f"q{i}", "rider_id": rider,
                "slot_id": "s1", "service_code": "consult",
                "priority": catalog.PRIORITY_NORMAL,
                "start": t(f"{DAY}T09:00"), "end": t(f"{DAY}T09:15"),
                "window_start": t(f"{DAY}T09:00"), "window_end": t(f"{DAY}T10:00"),
                "zone": "A", "fee_snapshot": snap, "reason": "initial"}})
        # 紧急请求滞留队列
        seq += 1
        events.append({"seq": seq, "type": "request_submitted", "payload": {
            "request_id": "q9", "rider_id": "u1", "service_code": "consult",
            "window_start": t(f"{DAY}T09:00"), "window_end": t(f"{DAY}T10:00"),
            "zone": "A"}})
        seq += 1
        events.append({"seq": seq, "type": "request_triaged", "payload": {
            "request_id": "q9", "priority": catalog.PRIORITY_URGENT}})

        report = audit(events)
        self.assertFalse(report["ok"])
        self.assertFalse(report["sections"]["capacity"]["ok"])
        self.assertFalse(report["sections"]["priority"]["ok"])
        self.assertTrue(report["sections"]["fee_basis"]["ok"])

    def test_replay_is_deterministic(self):
        svc = seed()
        open_baseline_slots(svc)
        register_and_verify(svc, "r1")
        submit_and_match(svc, "q1", "r1", ws="09:00", we="09:30")
        events = list(svc.store.events)
        r1 = audit(events)
        r2 = audit(events)
        self.assertEqual(r1["ok"], r2["ok"])
        self.assertTrue(r1["sections"]["replay_determinism"]["ok"])


class OutreachAndCancellationTest(unittest.TestCase):
    def setUp(self):
        self.svc = seed()
        register_and_verify(self.svc, "r1", insurance="resident")

    def test_outreach_at_post_is_free(self):
        self.svc.open_slot("slot-outreach", "loc-post-a", "outreach", "doc-li",
                           t(f"{DAY}T09:00"), t(f"{DAY}T12:00"), 2)
        self.svc.submit_request("q1", "r1", "outreach",
                                t(f"{DAY}T09:00"), t(f"{DAY}T10:00"))
        appt = self.svc.match_request("q1")["appointment"]
        self.assertEqual(appt["slot_id"], "slot-outreach")
        snap = appt["fee_snapshot"]
        self.assertEqual((snap["total_fen"], snap["self_fen"], snap["covered_fen"]),
                         (0, 0, 0))
        self.assertIn("外展义诊", snap["basis"])

    def test_cancelled_slot_with_no_alternative_needs_resolution(self):
        self.svc.open_slot("slot-only", "loc-center-a", "consult", "doc-li",
                           t(f"{DAY}T09:00"), t(f"{DAY}T10:00"), 1)
        appt = submit_and_match(self.svc, "q1", "r1", ws="09:00", we="09:15")
        out = self.svc.adjust_slot("slot-only", cancelled=True)
        self.assertFalse(out["rebookings"][0]["rebooked"])
        req = self.svc.store.requests["q1"]
        self.assertEqual(req["status"], "needs_reschedule")
        # 出现在队列的待处理清单
        queue = self.svc.daily_queue("A", DAY)
        self.assertEqual([r["request_id"] for r in queue["needs_resolution"]], ["q1"])
        # 号源恢复（医生重新出诊）后，医护与骑手确认新窗口再匹配
        self.svc.open_slot("slot-recovered", "loc-center-a", "consult", "doc-li",
                           t(f"{DAY}T11:00"), t(f"{DAY}T12:00"), 1)
        self.svc.update_request_window("q1", t(f"{DAY}T11:00"), t(f"{DAY}T11:30"))
        result = self.svc.match_request("q1")
        self.assertEqual(result["appointment"]["start"], t(f"{DAY}T11:00"))


class ReplayAuditNegativeTest(unittest.TestCase):
    def _base_events(self):
        DAY = "2026-09-20"
        return [
            {"seq": 1, "type": "rider_registered", "payload": {
                "rider_id": "u1", "zone": "A", "insurance": "employee",
                "package_version": "none", "team_id": "team-1"}},
            {"seq": 2, "type": "location_registered", "payload": {
                "location_id": "l1", "name": "中心", "type": "center", "zone": "A"}},
            {"seq": 3, "type": "practitioner_registered", "payload": {
                "practitioner_id": "d1", "name": "李医生", "team_id": "team-1"}},
            {"seq": 4, "type": "slot_opened", "payload": {
                "slot_id": "s1", "location_id": "l1", "service_code": "consult",
                "practitioner_id": "d1", "start": t(f"{DAY}T09:00"),
                "end": t(f"{DAY}T12:00"), "capacity": 1}},
        ], DAY

    def test_audit_detects_fee_tampering(self):
        events, DAY = self._base_events()
        bad_snap = {**catalog.fee_rule("consult", "employee", "none"),
                    "self_fen": 999, "covered_fen": 501}  # 与依据目录不符
        events += [
            {"seq": 5, "type": "request_submitted", "payload": {
                "request_id": "q1", "rider_id": "u1", "service_code": "consult",
                "window_start": t(f"{DAY}T09:00"), "window_end": t(f"{DAY}T10:00"),
                "zone": "A"}},
            {"seq": 6, "type": "appointment_matched", "payload": {
                "appt_id": "a1", "request_id": "q1", "rider_id": "u1",
                "slot_id": "s1", "service_code": "consult",
                "priority": "normal",
                "start": t(f"{DAY}T09:00"), "end": t(f"{DAY}T09:15"),
                "window_start": t(f"{DAY}T09:00"), "window_end": t(f"{DAY}T10:00"),
                "zone": "A", "fee_snapshot": bad_snap, "reason": "initial"}},
        ]
        report = audit(events)
        self.assertFalse(report["sections"]["fee_basis"]["ok"])
        self.assertTrue(any("self_fen" in f for f in
                            report["sections"]["fee_basis"]["findings"]))

    def test_audit_detects_followup_team_break(self):
        events, DAY = self._base_events()
        events += [
            {"seq": 5, "type": "request_submitted", "payload": {
                "request_id": "q1", "rider_id": "u1", "service_code": "consult",
                "window_start": t(f"{DAY}T09:00"), "window_end": t(f"{DAY}T10:00"),
                "zone": "A"}},
            {"seq": 6, "type": "appointment_matched", "payload": {
                "appt_id": "a1", "request_id": "q1", "rider_id": "u1",
                "slot_id": "s1", "service_code": "consult",
                "priority": "normal",
                "start": t(f"{DAY}T09:00"), "end": t(f"{DAY}T09:15"),
                "window_start": t(f"{DAY}T09:00"), "window_end": t(f"{DAY}T10:00"),
                "zone": "A",
                "fee_snapshot": {**catalog.fee_rule("consult", "employee", "none")},
                "reason": "initial"}},
            {"seq": 7, "type": "encounter_completed", "payload": {
                "appt_id": "a1", "team_id": "team-1", "disposition": "followup_only",
                "diagnosis_code": "I10", "note": "", "at": t(f"{DAY}T09:20"),
                "fee_lines": [{**catalog.fee_rule("consult", "employee", "none"),
                               "kind": "primary"}]}},
            # 随访被错误地挂到别的团队
            {"seq": 8, "type": "followup_scheduled", "payload": {
                "followup_id": "f1", "rider_id": "u1", "team_id": "team-OTHER",
                "encounter_id": "a1", "service_code": "consult",
                "due": t("2026-09-21T09:00"), "site_zone": "A"}},
        ]
        report = audit(events)
        self.assertFalse(report["sections"]["followup_continuity"]["ok"])


if __name__ == "__main__":
    unittest.main()
