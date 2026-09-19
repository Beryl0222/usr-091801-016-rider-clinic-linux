"""可执行演示：临时派单与并发改期事件的重放核对。

运行：python3 demo_audit.py
构造一条包含紧急驱逐、骑手临时接单、医生改期（含过期版本冲突）、
转诊处置与跨日跨站点随访的事件流，然后输出四项核对报告。
"""

from clinic import catalog
from clinic.clock import parse_minutes
from clinic.engine import ClinicService
from clinic.errors import Conflict
from clinic.replay import audit
from scenarios import seed

DAY = "2026-09-20"
t = lambda s: parse_minutes(f"{DAY}T{s}") if "T" not in s else parse_minutes(s)


def build_events():
    svc = seed()
    svc.open_slot("slot-center-am", "loc-center-a", "consult", "doc-li",
                  t("09:00"), t("12:00"), 1)
    svc.open_slot("slot-station-am", "loc-station-a", "consult", "doc-wang",
                  t("09:00"), t("12:00"), 1)
    svc.open_slot("slot-tcm-am", "loc-station-a", "tcm", "doc-zhao",
                  t("09:00"), t("12:00"), 1)
    svc.open_slot("slot-center-pm", "loc-center-a", "consult", "doc-li",
                  t("13:00"), t("17:00"), 1)

    svc.register_rider("r1", "A", catalog.INS_EMPLOYEE, "fd-v1", "team-1")
    svc.verify_identity("r1", "ID-R1-AAAA", "face", t("08:00"), "张*")
    svc.register_rider("r2", "A", catalog.INS_RESIDENT, "none", "team-1")
    svc.verify_identity("r2", "ID-R2-BBBB", "face", t("08:00"), "李*")
    svc.register_rider("r3", "A", catalog.INS_EMPLOYEE, "none", "team-1")
    svc.verify_identity("r3", "ID-R3-CCCC", "face", t("08:00"), "王*")
    svc.register_rider("r4", "A", catalog.INS_RESIDENT, "none", "team-1")
    svc.verify_identity("r4", "ID-R4-DDDD", "face", t("08:00"), "周*")

    # 普通约诊：r1（带服务包）中心 09:00；r4 站点 09:00；r2 中医 09:30
    svc.submit_request("q1", "r1", "consult", t("09:00"), t("09:30"))
    svc.match_request("q1")
    svc.submit_request("q4", "r4", "consult", t("09:00"), t("09:15"))
    svc.match_request("q4")
    svc.submit_request("q2", "r2", "tcm", t("09:30"), t("10:30"))
    svc.match_request("q2")

    # 临时派单：r3 紧急请求，两号源 09:00 皆满 → 驱逐中心的 r1；r1 按规则重排到 09:15
    svc.submit_request("q3", "r3", "consult", t("09:00"), t("09:15"))
    svc.triage("q3", catalog.PRIORITY_URGENT)
    svc.match_request("q3")

    # 并发改期（中医号源）：先成功一次，再用过期版本应得 409
    svc.adjust_slot("slot-tcm-am", new_start=t("10:00"), new_end=t("12:00"))
    try:
        svc.adjust_slot("slot-tcm-am",
                        new_start=t("10:30"), new_end=t("12:00"),
                        expected_version=1)
    except Conflict:
        pass  # 预期冲突，不入事件流

    # r1 临时接配送单：取其当前约诊，带新窗口重排到下午
    r1_appt = [a for a in svc.store.appointments.values()
               if a["rider_id"] == "r1" and a["status"] == "booked"][0]
    svc.rider_grab_order(r1_appt["appt_id"], t("13:00"), t("14:00"))

    # 到诊与处置：r3 到诊后正式转诊（产生两条费用行）；r1 下午到诊仅随访
    r3_appt = [a for a in svc.store.appointments.values()
               if a["rider_id"] == "r3"][0]
    svc.record_arrival(r3_appt["appt_id"], t("09:02"))
    svc.complete_encounter(r3_appt["appt_id"], catalog.DISPO_REFERRAL, "J06.9",
                           at=t("09:20"))

    r1_pm = [a for a in svc.store.appointments.values()
             if a["rider_id"] == "r1" and a["start"] >= t("13:00")][0]
    svc.record_arrival(r1_pm["appt_id"], t("13:03"))
    svc.complete_encounter(r1_pm["appt_id"], catalog.DISPO_FOLLOWUP, "I10",
                           at=t("13:20"))
    # 跨日随访
    svc.schedule_followup(r1_pm["appt_id"], "consult", t("2026-09-21T09:00"))
    # 骑手跨站点：同团队接续
    svc.transfer_rider_site("r1", "B")
    return svc.store.events


def main():
    events = build_events()
    print(f"事件数: {len(events)}")
    report = audit(events)
    for name, section in report["sections"].items():
        mark = "PASS" if section["ok"] else "FAIL"
        print(f"[{mark}] {name}")
        for finding in section["findings"]:
            print(f"    - {finding}")
    print("总体:", "OK" if report["ok"] else "存在问题")
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
