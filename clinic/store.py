"""事件存储：所有状态变更以追加事件表达，可清空后逐条重放重建。

注意三类存储在数据结构上即分开：
- identities：身份证明核验记录（核验岗才可访问）
- profiles/encounters：健康档案侧（医护才可访问，绝不含证件信息）
- slots/appointments/requests/followups：排班协同侧
"""

import threading

from . import catalog
from .clock import overlaps

ACTIVE = ("booked", "arrived")  # 占用容量的约诊状态


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        self.seq = 0
        self.events = []
        # 身份侧
        self.identities = {}
        # 健康档案侧
        self.profiles = {}      # rider_id -> {rider_id, zone, insurance, package_version, team_id}
        self.encounters = {}    # appt_id -> 就诊记录（含诊断、处置）
        # 资源侧
        self.locations = {}     # location_id -> {location_id, name, type, zone}
        self.practitioners = {} # practitioner_id -> {..., team_id}
        self.slots = {}         # slot_id -> 号源会话
        # 协同侧
        self.requests = {}      # request_id -> 骑手时间窗提交
        self.appointments = {}  # appt_id -> 约诊
        self.followups = {}     # followup_id -> 随访任务

    # ---- 事件追加与重放 -------------------------------------------------

    def append(self, event_type, payload):
        event = {"seq": self.seq + 1, "type": event_type, "payload": payload}
        self.seq += 1
        self.events.append(event)
        self._apply(event)
        return event

    def replay(self, events):
        """用给定事件序列重建状态（用于场景重放与独立核对）。"""
        self.reset()
        for event in events:
            self.seq = event["seq"]
            self.events.append(event)
            self._apply(event)

    # ---- 应用单个事件（无校验，校验在服务层命令中完成） -----------------

    def _apply(self, event):
        p = event["payload"]
        kind = event["type"]
        seq = event["seq"]

        if kind == "rider_registered":
            self.profiles[p["rider_id"]] = {
                "rider_id": p["rider_id"], "zone": p["zone"],
                "insurance": p["insurance"], "package_version": p["package_version"],
                "team_id": p["team_id"], "registered_seq": seq,
            }
        elif kind == "identity_verified":
            self.identities[p["rider_id"]] = {
                "rider_id": p["rider_id"], "id_ref": p["id_ref"],
                "name_masked": p.get("name_masked", ""),
                "verified": p["verified"], "method": p["method"],
                "verified_at": p["verified_at"], "version": seq,
            }
        elif kind == "location_registered":
            self.locations[p["location_id"]] = dict(p)
        elif kind == "practitioner_registered":
            self.practitioners[p["practitioner_id"]] = dict(p)
        elif kind == "slot_opened":
            self.slots[p["slot_id"]] = {
                "slot_id": p["slot_id"], "location_id": p["location_id"],
                "service_code": p["service_code"], "practitioner_id": p["practitioner_id"],
                "start": p["start"], "end": p["end"], "capacity": p["capacity"],
                "status": "open", "version": seq,
            }
        elif kind == "slot_adjusted":
            slot = self.slots[p["slot_id"]]
            if p.get("cancelled"):
                slot["status"] = "cancelled"
            else:
                slot["start"] = p["new_start"]
                slot["end"] = p["new_end"]
                slot["status"] = "adjusted"
            slot["version"] = seq
            slot["change_reason"] = p["reason"]
        elif kind == "request_submitted":
            self.requests[p["request_id"]] = {
                "request_id": p["request_id"], "rider_id": p["rider_id"],
                "service_code": p["service_code"],
                "window_start": p["window_start"], "window_end": p["window_end"],
                "zone": p["zone"], "priority": catalog.PRIORITY_NORMAL,
                "status": "queued", "appointment_id": None,
                "submitted_seq": seq, "history": [{"seq": seq, "event": "submitted"}],
            }
        elif kind == "request_triaged":
            req = self.requests[p["request_id"]]
            req["priority"] = p["priority"]
            req["history"].append({"seq": seq, "event": f"triaged:{p['priority']}"})
        elif kind == "request_window_updated":
            req = self.requests[p["request_id"]]
            req["window_start"] = p["window_start"]
            req["window_end"] = p["window_end"]
            req["history"].append({"seq": seq, "event": "window_updated"})
        elif kind == "appointment_matched":
            self.requests[p["request_id"]]["status"] = "matched"
            self.requests[p["request_id"]]["appointment_id"] = p["appt_id"]
            self.requests[p["request_id"]]["history"].append(
                {"seq": seq, "event": "matched", "slot_id": p["slot_id"]})
            self.appointments[p["appt_id"]] = {
                "appt_id": p["appt_id"], "request_id": p["request_id"],
                "rider_id": p["rider_id"], "slot_id": p["slot_id"],
                "service_code": p["service_code"], "priority": p["priority"],
                "start": p["start"], "end": p["end"],
                "window_start": p["window_start"], "window_end": p["window_end"],
                "zone": p["zone"], "status": catalog.ST_BOOKED,
                "arrival_minute": None, "version": seq,
                "fee_snapshot": p["fee_snapshot"],
                "history": [{"seq": seq, "event": "matched", "reason": p.get("reason", "initial"),
                             "slot_id": p["slot_id"], "start": p["start"]}],
            }
        elif kind == "appointment_rescheduled":
            appt = self.appointments[p["appt_id"]]
            appt["slot_id"] = p["slot_id"]
            appt["start"] = p["start"]
            appt["end"] = p["end"]
            appt["status"] = catalog.ST_BOOKED if p.get("rebooked") else catalog.ST_PENDING_RESCHEDULE
            appt["version"] = seq
            appt["history"].append({"seq": seq, "event": "rescheduled", "reason": p["reason"],
                                    "slot_id": p["slot_id"], "start": p["start"]})
            req = self.requests.get(appt["request_id"])
            if req and not p.get("rebooked"):
                req["status"] = "needs_reschedule"
        elif kind == "appointment_cancelled":
            appt = self.appointments[p["appt_id"]]
            appt["status"] = catalog.ST_CANCELLED
            appt["version"] = seq
            appt["history"].append({"seq": seq, "event": "cancelled", "reason": p["reason"]})
            req = self.requests.get(appt["request_id"])
            if req:
                if p.get("requeue_request"):
                    req["status"] = "needs_reschedule"
                    if p.get("new_window_start") is not None:
                        req["window_start"] = p["new_window_start"]
                        req["window_end"] = p["new_window_end"]
                else:
                    req["status"] = "cancelled"
        elif kind == "arrival_recorded":
            appt = self.appointments[p["appt_id"]]
            appt["arrival_minute"] = p["arrived_at"]
            if p.get("placed", True) and appt["status"] in (catalog.ST_BOOKED, catalog.ST_PENDING_RESCHEDULE):
                appt["status"] = catalog.ST_ARRIVED
            appt["version"] = seq
            appt["history"].append({"seq": seq, "event": "arrival", "at": p["arrived_at"],
                                    "placed": p.get("placed", True)})
        elif kind == "appointment_noshow":
            appt = self.appointments[p["appt_id"]]
            appt["status"] = catalog.ST_NOSHOW
            appt["version"] = seq
            appt["history"].append({"seq": seq, "event": "no_show"})
            self.requests[appt["request_id"]]["status"] = "expired"
        elif kind == "encounter_completed":
            appt = self.appointments[p["appt_id"]]
            appt["status"] = catalog.ST_COMPLETED
            appt["version"] = seq
            appt["history"].append({"seq": seq, "event": "completed"})
            self.requests[appt["request_id"]]["status"] = "completed"
            self.encounters[p["appt_id"]] = {
                "appt_id": p["appt_id"], "rider_id": appt["rider_id"],
                "team_id": p["team_id"], "disposition": p["disposition"],
                "diagnosis_code": p["diagnosis_code"], "note": p.get("note", ""),
                "at": p["at"], "fee_lines": p["fee_lines"],
            }
        elif kind == "followup_scheduled":
            self.followups[p["followup_id"]] = {
                "followup_id": p["followup_id"], "rider_id": p["rider_id"],
                "team_id": p["team_id"], "encounter_id": p.get("encounter_id"),
                "service_code": p["service_code"], "due": p["due"],
                "site_zone": p["site_zone"], "status": "open",
                "history": [{"seq": seq, "event": "scheduled", "site_zone": p["site_zone"], "due": p["due"]}],
            }
        elif kind == "rider_transferred":
            profile = self.profiles[p["rider_id"]]
            old_zone = profile["zone"]
            profile["zone"] = p["new_zone"]
            for f in self.followups.values():
                if f["rider_id"] == p["rider_id"] and f["status"] == "open":
                    f["site_zone"] = p["new_zone"]
                    f["history"].append(
                        {"seq": seq, "event": "site_changed", "from_zone": old_zone,
                         "to_zone": p["new_zone"], "team_unchanged": True})
        elif kind == "followup_completed":
            f = self.followups[p["followup_id"]]
            f["status"] = "completed"
            f["history"].append({"seq": seq, "event": "completed", "at": p["at"]})
        else:
            raise ValueError(f"未知事件类型: {kind}")

    # ---- 容量查询 -------------------------------------------------------

    def occupancy_ok(self, slot_id, start, end, capacity, ignore_appt=None):
        """判断 [start,end) 是否能在该号源内容量内放入（逐分钟检测）。"""
        for minute in range(start, end):
            used = 0
            for appt in self.appointments.values():
                if appt["slot_id"] != slot_id or appt["appt_id"] == ignore_appt:
                    continue
                if appt["status"] not in ACTIVE:
                    continue
                if minute >= appt["start"] and minute < appt["end"]:
                    used += 1
                    if used >= capacity:
                        return False
        return True

    def overlapping_appointments(self, slot_id, start, end):
        return [a for a in self.appointments.values()
                if a["slot_id"] == slot_id and a["status"] in ACTIVE
                and overlaps(a["start"], a["end"], start, end)]
