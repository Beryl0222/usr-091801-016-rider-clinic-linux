"""诊疗协同核心引擎：排班匹配、显式重排规则、医疗优先级与随访接续。

重排规则（全部落入约诊 history，可重放核对）：
  R1 骑手迟到：迟到 <=5 分钟直接接诊；<=15 分钟在本人时间窗内顺延到最近容量；
     超过 15 分钟按爽约处理，号源释放。
  R2 骑手临时接单：原约诊取消（原因 rider_order），若给出新时间窗则按普通匹配重新排队。
  R3 紧急约诊无空位：只能驱逐普通约诊；被驱逐者立即按原时间窗重新匹配，
     无可用位置时回到待处理队列，绝不静默丢失。
  R4 医生出诊变化：受影响约诊逐个自动重排；号源取消或无处可去时进入待处理队列。
医疗优先级只能覆盖普通排队，普通约诊永远不能驱逐紧急约诊。
"""

from . import catalog
from .clock import day_key
from .errors import Conflict, NotFound, ValidationFailed
from .store import ACTIVE, Store

LATE_GRACE = 5      # 迟到宽限（分钟）
LATE_LIMIT = 15     # 迟到上限（分钟）
STEP = 5            # 排班对齐粒度

REASON_RIDER_ORDER = "rider_order"
REASON_LATE = "late_rebook"
REASON_BUMPED = "bumped_by_urgent"
REASON_SLOT_MOVED = "practitioner_reschedule"
REASON_SLOT_CANCELLED = "practitioner_cancelled"


def _fee_snapshot(service_code, rider):
    rule = catalog.fee_rule(service_code, rider["insurance"], rider["package_version"])
    snap = dict(rule)
    snap["service_name"] = catalog.SERVICES.get(
        service_code, {}).get("name", "正式转诊" if service_code == catalog.REFERRAL else service_code)
    return snap


class ClinicService:
    def __init__(self, store=None, now_minute=None):
        self.store = store or Store()
        self._now = now_minute

    # 测试/重放辅助：引擎可注入时钟，缺省取最近事件时间之后
    def now(self):
        if self._now is not None:
            return self._now
        return self.store.seq  # 仅用于无时钟场景的兜底；命令应显式传时间

    # ===== 资源注册（开诊准备） ==========================================

    def register_location(self, location_id, name, loc_type, zone):
        if loc_type not in catalog.LOCATION_TYPES:
            raise ValidationFailed(f"未知服务点类型: {loc_type}")
        self.store.append("location_registered", {
            "location_id": location_id, "name": name, "type": loc_type, "zone": zone})
        return self.store.locations[location_id]

    def register_practitioner(self, practitioner_id, name, team_id):
        self.store.append("practitioner_registered", {
            "practitioner_id": practitioner_id, "name": name, "team_id": team_id})
        return self.store.practitioners[practitioner_id]

    def open_slot(self, slot_id, location_id, service_code, practitioner_id, start, end, capacity):
        if service_code not in catalog.SERVICES:
            raise ValidationFailed(f"未知服务: {service_code}")
        if location_id not in self.store.locations:
            raise NotFound(f"服务点不存在: {location_id}")
        if practitioner_id not in self.store.practitioners:
            raise NotFound(f"医护不存在: {practitioner_id}")
        loc = self.store.locations[location_id]
        if loc["type"] not in catalog.SERVICES[service_code]["locations"]:
            raise ValidationFailed(
                f"{catalog.SERVICES[service_code]['name']}不能在{loc['name']}开展")
        if end - start < catalog.SERVICES[service_code]["duration"]:
            raise ValidationFailed("号源时长不足一个服务单元")
        if slot_id in self.store.slots:
            raise ValidationFailed(f"号源已存在: {slot_id}")
        self.store.append("slot_opened", {
            "slot_id": slot_id, "location_id": location_id,
            "service_code": service_code, "practitioner_id": practitioner_id,
            "start": start, "end": end, "capacity": capacity})
        return self.store.slots[slot_id]

    # ===== 骑手注册与身份核验（两侧存储物理分离） ========================

    def register_rider(self, rider_id, zone, insurance, package_version=catalog.PACKAGE_NONE, team_id=None):
        if insurance not in catalog.INSURANCE_TYPES:
            raise ValidationFailed(f"未知医保身份: {insurance}")
        if package_version not in catalog.PACKAGES:
            raise ValidationFailed(f"未知服务包版本: {package_version}")
        if rider_id in self.store.profiles:
            raise ValidationFailed(f"骑手已注册: {rider_id}")
        self.store.append("rider_registered", {
            "rider_id": rider_id, "zone": zone, "insurance": insurance,
            "package_version": package_version, "team_id": team_id})
        return self.store.profiles[rider_id]

    def verify_identity(self, rider_id, id_ref, method, verified_at, name_masked="", verified=True):
        """写入身份侧记录。核验结果与健康档案绝不放在同一记录中。"""
        if rider_id not in self.store.profiles:
            raise NotFound(f"骑手不存在: {rider_id}")
        if not id_ref or len(id_ref) < 4:
            raise ValidationFailed("证件引用缺失或不合规")
        self.store.append("identity_verified", {
            "rider_id": rider_id, "id_ref": id_ref, "name_masked": name_masked,
            "method": method, "verified": bool(verified), "verified_at": verified_at})
        return self.identity_view(rider_id)

    # ===== 时间窗提交与分诊 ==============================================

    def submit_request(self, request_id, rider_id, service_code, window_start, window_end):
        if service_code not in catalog.SERVICES:
            raise ValidationFailed(f"未知服务: {service_code}")
        rider = self._rider(rider_id)
        if window_end - window_start < catalog.SERVICES[service_code]["duration"]:
            raise ValidationFailed("时间窗短于服务时长，无法安排")
        if request_id in self.store.requests:
            raise ValidationFailed(f"请求已存在: {request_id}")
        self.store.append("request_submitted", {
            "request_id": request_id, "rider_id": rider_id,
            "service_code": service_code,
            "window_start": window_start, "window_end": window_end,
            "zone": rider["zone"]})
        return self.store.requests[request_id]

    def triage(self, request_id, priority):
        if priority not in catalog.PRIORITIES:
            raise ValidationFailed(f"未知优先级: {priority}")
        req = self._request(request_id)
        self.store.append("request_triaged",
                          {"request_id": request_id, "priority": priority})
        return self.store.requests[request_id]

    def update_request_window(self, request_id, window_start, window_end):
        """待处理（爽约/号源取消后）请求经与骑手确认，更新可服务时间窗。"""
        req = self._request(request_id)
        if window_end - window_start < catalog.SERVICES[req["service_code"]]["duration"]:
            raise ValidationFailed("时间窗短于服务时长")
        self.store.append("request_window_updated", {
            "request_id": request_id,
            "window_start": window_start, "window_end": window_end})
        return self.store.requests[request_id]

    # ===== 匹配 ==========================================================

    def _candidate_slots(self, req):
        duration = catalog.SERVICES[req["service_code"]]["duration"]
        allowed = catalog.SERVICES[req["service_code"]]["locations"]
        out = []
        for slot in self.store.slots.values():
            if slot["status"] == "cancelled":
                continue
            if slot["service_code"] != req["service_code"]:
                continue
            loc = self.store.locations[slot["location_id"]]
            if loc["zone"] != req["zone"] or loc["type"] not in allowed:
                continue
            if min(slot["end"], req["window_end"]) - max(slot["start"], req["window_start"]) < duration:
                continue
            out.append(slot)
        out.sort(key=lambda s: s["start"])
        return out

    def _first_placement(self, slots, req, ignore_appt=None, not_before=None):
        """在候选号源中找最早可行位置，返回 (slot, start, end) 或 None。"""
        duration = catalog.SERVICES[req["service_code"]]["duration"]
        floor = req["window_start"] if not_before is None else max(req["window_start"], not_before)
        for slot in slots:
            start = max(slot["start"], floor)
            end_limit = min(slot["end"], req["window_end"])
            while start + duration <= end_limit:
                if self.store.occupancy_ok(slot["slot_id"], start, start + duration,
                                           slot["capacity"], ignore_appt=ignore_appt):
                    return slot, start, start + duration
                start += STEP
        return None

    def match_request(self, request_id):
        req = self._request(request_id)
        if req["status"] not in ("queued", "needs_reschedule"):
            raise ValidationFailed(f"请求当前状态不可匹配: {req['status']}")
        rider = self._rider(req["rider_id"])
        identity = self.store.identities.get(req["rider_id"])
        if not identity or not identity["verified"]:
            raise ValidationFailed("身份未核验通过，不能生成约诊", code="identity_unverified")

        slots = self._candidate_slots(req)
        placement = self._first_placement(slots, req)
        displaced = []
        if placement is None:
            if req["priority"] != catalog.PRIORITY_URGENT:
                raise ValidationFailed("时间窗内无可用容量", code="no_capacity")
            # R3：紧急约诊可驱逐普通约诊，选驱逐人数最少的方案
            placement, displaced = self._placement_with_bump(slots, req)
            if placement is None:
                raise ValidationFailed("时间窗内无可用容量（含重排普通约诊）", code="no_capacity")

        slot, start, end = placement
        appt_id = f"appt-{request_id}-{self.store.seq + 1}"
        payload = {
            "appt_id": appt_id, "request_id": request_id,
            "rider_id": req["rider_id"], "slot_id": slot["slot_id"],
            "service_code": req["service_code"], "priority": req["priority"],
            "start": start, "end": end,
            "window_start": req["window_start"], "window_end": req["window_end"],
            "zone": req["zone"], "fee_snapshot": _fee_snapshot(req["service_code"], rider),
            "reason": "initial",
        }
        self.store.append("appointment_matched", payload)

        # 先落地紧急约诊，再为被驱逐者重新匹配
        rebook_results = []
        for victim in displaced:
            self.store.append("appointment_cancelled", {
                "appt_id": victim["appt_id"], "reason": REASON_BUMPED,
                "requeue_request": True})
            victim_req = self.store.requests[victim["request_id"]]
            result = self._try_rebook(victim_req, reason=REASON_BUMPED)
            rebook_results.append({"appt_id": victim["appt_id"], **result})

        return {"appointment": self.store.appointments[appt_id],
                "displaced": rebook_results}

    def _placement_with_bump(self, slots, req):
        """返回 (placement, 需驱逐的普通约诊列表)；驱逐集合最小者优先。"""
        duration = catalog.SERVICES[req["service_code"]]["duration"]
        best = None  # (victims_count, slot, start, victims)
        for slot in slots:
            start = max(slot["start"], req["window_start"])
            end_limit = min(slot["end"], req["window_end"])
            while start + duration <= end_limit:
                victims = self._required_bump_victims(slot, start, start + duration)
                if victims is not False:
                    if best is None or len(victims) < len(best[3]):
                        best = (slot, start, start + duration, victims)
                start += STEP
        if best is None:
            return None, []
        return (best[0], best[1], best[2]), best[3]

    def _required_bump_victims(self, slot, start, end):
        """计算放入 [start,end) 需移除的普通约诊最小集合；遇无法让位的紧急约诊则不可行。"""
        victims = set()
        duration_present = True
        for minute in range(start, end):
            present = [a for a in self.store.appointments.values()
                       if a["slot_id"] == slot["slot_id"] and a["status"] in ACTIVE
                       and a["appt_id"] not in victims
                       and minute >= a["start"] and minute < a["end"]]
            # 放入新约诊后该分钟需占用一个位置
            need_to_remove = len(present) - slot["capacity"] + 1
            if need_to_remove <= 0:
                continue
            removable = sorted(
                (a for a in present if a["priority"] == catalog.PRIORITY_NORMAL),
                key=lambda a: a["version"], reverse=True)  # 最晚预约者先让位
            if len(removable) < need_to_remove:
                return False  # 剩余为紧急约诊：普通排队不能覆盖医疗优先级
            for a in removable[:need_to_remove]:
                victims.add(a["appt_id"])
        return [self.store.appointments[i] for i in victims]

    # ===== 重排：迟到 / 临时接单 / 医生出诊变化 ==========================

    def _try_rebook(self, req, reason, not_before=None):
        """按原时间窗重新匹配；已取消（被驱逐）则新建约诊链，待重排/在约则沿用原约诊。"""
        slots = self._candidate_slots(req)
        appt = self.store.appointments.get(req["appointment_id"]) if req.get("appointment_id") else None
        reusable = appt and appt["status"] in ACTIVE + (catalog.ST_PENDING_RESCHEDULE,)
        placement = self._first_placement(
            slots, req,
            ignore_appt=appt["appt_id"] if reusable else None,
            not_before=not_before)
        if placement is None:
            if appt and appt["status"] in ACTIVE:
                self.store.append("appointment_rescheduled", {
                    "appt_id": appt["appt_id"], "slot_id": appt["slot_id"],
                    "start": appt["start"], "end": appt["end"],
                    "rebooked": False, "reason": reason})
            return {"rebooked": False, "reason": reason}
        slot, start, end = placement
        if not reusable:
            # 被驱逐/已取消：生成新约诊，保留前驱链
            rider = self._rider(req["rider_id"])
            new_id = f"appt-{req['request_id']}-{self.store.seq + 1}"
            self.store.append("appointment_matched", {
                "appt_id": new_id, "request_id": req["request_id"],
                "rider_id": req["rider_id"], "slot_id": slot["slot_id"],
                "service_code": req["service_code"],
                "priority": req["priority"], "start": start, "end": end,
                "window_start": req["window_start"], "window_end": req["window_end"],
                "zone": req["zone"], "fee_snapshot": _fee_snapshot(req["service_code"], rider),
                "reason": reason})
            return {"rebooked": True, "reason": reason, "appointment_id": new_id}
        self.store.append("appointment_rescheduled", {
            "appt_id": appt["appt_id"], "slot_id": slot["slot_id"],
            "start": start, "end": end, "rebooked": True, "reason": reason})
        return {"rebooked": True, "reason": reason, "appointment_id": appt["appt_id"]}

    def record_arrival(self, appt_id, arrived_at):
        appt = self._appt(appt_id)
        if appt["status"] not in ACTIVE:
            raise ValidationFailed(f"约诊当前状态不接诊: {appt['status']}")
        gap = arrived_at - appt["start"]
        self.store.append("arrival_recorded",
                          {"appt_id": appt_id, "arrived_at": arrived_at, "placed": gap <= LATE_GRACE})
        if gap <= LATE_GRACE:
            return {"result": "placed", "appointment": self.store.appointments[appt_id]}
        if gap <= LATE_LIMIT:
            # R1：在本人时间窗内顺延，且新开始时间不得早于实际到院时刻
            req = self.store.requests[appt["request_id"]]
            result = self._try_rebook(req, reason=REASON_LATE, not_before=arrived_at)
            if result["rebooked"]:
                new_appt = self.store.appointments[result["appointment_id"]]
                self.store.append("arrival_recorded", {
                    "appt_id": new_appt["appt_id"], "arrived_at": arrived_at, "placed": True})
                return {"result": "rebooked_late", **result}
            return {"result": "needs_reschedule", **result}
        # 超过迟到上限：爽约，释放号源
        self.store.append("appointment_noshow", {"appt_id": appt_id})
        return {"result": "no_show", "appointment": self.store.appointments[appt_id]}

    def rider_grab_order(self, appt_id, new_window_start=None, new_window_end=None):
        """R2：骑手临时接单。给新窗口则带窗重排，否则取消。"""
        appt = self._appt(appt_id)
        if new_window_start is None:
            self.store.append("appointment_cancelled", {
                "appt_id": appt_id, "reason": REASON_RIDER_ORDER})
            return {"result": "cancelled"}
        if new_window_end - new_window_start < catalog.SERVICES[appt["service_code"]]["duration"]:
            raise ValidationFailed("新时间窗短于服务时长")
        self.store.append("appointment_cancelled", {
            "appt_id": appt_id, "reason": REASON_RIDER_ORDER, "requeue_request": True,
            "new_window_start": new_window_start, "new_window_end": new_window_end})
        req = self.store.requests[appt["request_id"]]
        return {"result": "requeued", **self._try_rebook(req, reason=REASON_RIDER_ORDER)}

    def adjust_slot(self, slot_id, new_start=None, new_end=None, cancelled=False,
                    reason=REASON_SLOT_MOVED, expected_version=None):
        """R4：医生出诊变化（含取消），自动重排受影响约诊。"""
        slot = self._slot(slot_id)
        if expected_version is not None and slot["version"] != expected_version:
            raise Conflict("slot_version_conflict",
                           f"号源已被其他改期更新（版本 {slot['version']}）")
        if not cancelled and (new_start is None or new_end is None):
            raise ValidationFailed("改期需提供新的开始与结束时间")
        affected = [a for a in self.store.appointments.values()
                    if a["slot_id"] == slot_id and a["status"] in ACTIVE]
        self.store.append("slot_adjusted", {
            "slot_id": slot_id,
            "cancelled": cancelled,
            "new_start": new_start or slot["start"], "new_end": new_end or slot["end"],
            "reason": reason})
        results = []
        for appt in sorted(affected, key=lambda a: (a["priority"] != catalog.PRIORITY_URGENT, a["start"])):
            req = self.store.requests[appt["request_id"]]
            results.append({"appt_id": appt["appt_id"],
                            **self._try_rebook(req, reason=REASON_SLOT_CANCELLED if cancelled
                                               else REASON_SLOT_MOVED)})
        return {"slot": self.store.slots[slot_id], "rebookings": results}

    def cancel_appointment(self, appt_id, reason):
        self.store.append("appointment_cancelled",
                          {"appt_id": appt_id, "reason": reason})
        return self.store.appointments[appt_id]

    # ===== 到诊完成：处置、费用行、随访接续 ==============================

    def complete_encounter(self, appt_id, disposition, diagnosis_code, note="", at=None):
        appt = self._appt(appt_id)
        if appt["status"] != catalog.ST_ARRIVED:
            raise ValidationFailed(f"仅已到诊可完成诊疗，当前: {appt['status']}")
        if disposition not in catalog.DISPOSITIONS:
            raise ValidationFailed(f"未知处置: {disposition}")
        rider = self._rider(appt["rider_id"])
        fee_lines = [dict(appt["fee_snapshot"], kind="primary")]
        if disposition == catalog.DISPO_REFERRAL:
            fee_lines.append(dict(_fee_snapshot(catalog.REFERRAL, rider), kind="referral"))
        # 费用行与诊断同在档案侧；身份侧不含任何诊疗信息
        self.store.append("encounter_completed", {
            "appt_id": appt_id, "team_id": rider["team_id"],
            "disposition": disposition, "diagnosis_code": diagnosis_code,
            "note": note, "at": at,
            "fee_lines": fee_lines})
        return self.store.encounters[appt_id]

    def schedule_followup(self, appt_id, service_code, due):
        """随访责任绑定骑手签约家庭医生团队，不随服务点变化。"""
        enc = self.store.encounters.get(appt_id)
        if not enc:
            raise NotFound("需先完成诊疗才能安排随访")
        if service_code not in catalog.SERVICES:
            raise ValidationFailed(f"未知服务: {service_code}")
        rider = self._rider(enc["rider_id"])
        followup_id = f"fu-{appt_id}-{self.store.seq + 1}"
        self.store.append("followup_scheduled", {
            "followup_id": followup_id, "rider_id": enc["rider_id"],
            "team_id": rider["team_id"], "encounter_id": appt_id,
            "service_code": service_code, "due": due,
            "site_zone": rider["zone"]})
        return self.store.followups[followup_id]

    def transfer_rider_site(self, rider_id, new_zone):
        """骑手跨站点：片区更新，未完成随访由同一团队接续（team 不变）。"""
        rider = self._rider(rider_id)
        if rider["zone"] == new_zone:
            raise ValidationFailed("骑手已在该片区")
        self.store.append("rider_transferred",
                          {"rider_id": rider_id, "new_zone": new_zone})
        return self.store.profiles[rider_id]

    def complete_followup(self, followup_id, at):
        if followup_id not in self.store.followups:
            raise NotFound(f"随访不存在: {followup_id}")
        self.store.append("followup_completed",
                          {"followup_id": followup_id, "at": at})
        return self.store.followups[followup_id]

    # ===== 视图：可执行队列 / 随访 / 去标识覆盖统计 / 分角色资料 =========

    def daily_queue(self, zone, day):
        """开诊前可执行队列：按时间排序，紧急约诊排在同时间普通约诊之前。"""
        slots_out = []
        for slot in sorted(self.store.slots.values(), key=lambda s: s["start"]):
            loc = self.store.locations[slot["location_id"]]
            if loc["zone"] != zone or day_key(slot["start"]) != day:
                continue
            appts = [a for a in self.store.appointments.values()
                     if a["slot_id"] == slot["slot_id"] and a["status"] in ACTIVE]
            appts = sorted(appts, key=lambda a: (
                a["start"], 0 if a["priority"] == catalog.PRIORITY_URGENT else 1))
            doc = self.store.practitioners.get(slot["practitioner_id"], {})
            slots_out.append({
                "slot_id": slot["slot_id"],
                "location": {"location_id": loc["location_id"], "name": loc["name"], "type": loc["type"]},
                "service": {"code": slot["service_code"],
                            "name": catalog.SERVICES[slot["service_code"]]["name"]},
                "practitioner_id": slot["practitioner_id"],
                "practitioner_name": doc.get("name", ""),
                "start": slot["start"], "end": slot["end"], "capacity": slot["capacity"],
                "status": slot["status"],
                "appointments": [{
                    "appt_id": a["appt_id"], "rider_id": a["rider_id"],
                    "start": a["start"], "end": a["end"],
                    "priority": a["priority"], "status": a["status"],
                    "arrival_minute": a["arrival_minute"],
                } for a in appts],
            })
        needs_resolution = [{
            "request_id": r["request_id"], "rider_id": r["rider_id"],
            "service_code": r["service_code"], "priority": r["priority"],
            "window_start": r["window_start"], "window_end": r["window_end"],
            "status": r["status"],
        } for r in self.store.requests.values()
            if r["zone"] == zone and r["status"] in ("queued", "needs_reschedule")]
        return {"zone": zone, "day": day, "slots": slots_out,
                "needs_resolution": needs_resolution}

    def team_followups(self, team_id):
        """家庭医生团队视角：含骑手跨站点后自动带过来的随访（跨日同样列出）。"""
        return [dict(f) for f in self.store.followups.values() if f["team_id"] == team_id]

    def union_coverage(self, day_start, day_end_exclusive):
        """工会视角：去标识化覆盖统计。只输出聚合计数，绝不输出骑手标识或诊断。"""
        by_zone = {}
        by_service = {}
        by_disposition = {}
        riders = set()
        visits = 0
        for enc in self.store.encounters.values():
            appt = self.store.appointments[enc["appt_id"]]
            if not (day_start <= appt["start"] < day_end_exclusive):
                continue
            visits += 1
            riders.add(enc["rider_id"])
            zone = appt["zone"]
            svc = appt["service_code"]
            by_zone[zone] = by_zone.get(zone, 0) + 1
            by_service[svc] = by_service.get(svc, 0) + 1
            by_disposition[enc["disposition"]] = by_disposition.get(enc["disposition"], 0) + 1
        return {
            "period": {"start": day_start, "end_exclusive": day_end_exclusive},
            "visits": visits,
            "distinct_riders": len(riders),
            "by_zone": by_zone,
            "by_service": by_service,
            "by_disposition": by_disposition,
            "note": "去标识化统计：不含骑手身份、证件与诊断信息",
        }

    def identity_view(self, rider_id):
        """核验岗视角：只有核验结果与证件引用，无健康内容。"""
        record = self.store.identities.get(rider_id)
        if not record:
            raise NotFound("无身份核验记录")
        return {k: record[k] for k in
                ("rider_id", "id_ref", "name_masked", "verified", "method", "verified_at")}

    def health_view(self, rider_id):
        """医护视角：健康档案与就诊记录，不含任何证件字段。"""
        rider = self._rider(rider_id)
        return {
            "rider_id": rider_id, "zone": rider["zone"],
            "insurance": rider["insurance"], "package_version": rider["package_version"],
            "team_id": rider["team_id"],
            "identity_verified": rider_id in self.store.identities
                                 and self.store.identities[rider_id]["verified"],
            "encounters": [dict(self.store.encounters[a["appt_id"]])
                           for a in self.store.appointments.values()
                           if a["rider_id"] == rider_id and a["appt_id"] in self.store.encounters],
        }

    # ===== 内部辅助 ======================================================

    def _rider(self, rider_id):
        if rider_id not in self.store.profiles:
            raise NotFound(f"骑手不存在: {rider_id}")
        return self.store.profiles[rider_id]

    def _request(self, request_id):
        if request_id not in self.store.requests:
            raise NotFound(f"请求不存在: {request_id}")
        return self.store.requests[request_id]

    def _appt(self, appt_id):
        if appt_id not in self.store.appointments:
            raise NotFound(f"约诊不存在: {appt_id}")
        return self.store.appointments[appt_id]

    def _slot(self, slot_id):
        if slot_id not in self.store.slots:
            raise NotFound(f"号源不存在: {slot_id}")
        return self.store.slots[slot_id]
