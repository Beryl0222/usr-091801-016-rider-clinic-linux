"""应用服务层：用例编排、参数校验、幂等与并发控制。

所有写操作在 store.lock 内串行执行；携带 request_id 的写请求幂等，
重放同一请求返回首次结果，不产生重复记录。
"""
from __future__ import annotations

from . import billing, followup, privacy, scheduling, seed
from . import models as M
from .store import Store

# 系统不读取配送订单详情：命中这些字段直接拒绝
ORDER_DETAIL_FIELDS = ("order_id", "order", "orders", "delivery_order",
                       "dispatch_id", "task_id", "waybill_id", "order_detail")


class App:
    def __init__(self, seed_static_data=True):
        self.store = Store()
        if seed_static_data:
            seed.seed_static(self.store)

    # ---- 内部工具 ----
    def _cached(self, payload):
        """重放检测：同一 request_id 直接返回首次结果，先于任何状态校验。"""
        request_id = payload.get("request_id")
        if request_id:
            return self.store.idempotency.get(request_id)
        return None

    def _idempotent(self, payload, produce):
        request_id = payload.get("request_id")
        if request_id:
            cached = self.store.idempotency.get(request_id)
            if cached is not None:
                return cached
        result = produce()
        if request_id:
            self.store.idempotency[request_id] = result
        return result

    @staticmethod
    def _reject_order_details(payload):
        bad = sorted(set(ORDER_DETAIL_FIELDS) & set(payload))
        if bad:
            raise M.bad_request(f"系统不读取配送订单详情，请移除字段：{bad}")

    @staticmethod
    def _need(payload, *fields):
        missing = [f for f in fields if payload.get(f) in (None, "")]
        if missing:
            raise M.bad_request(f"缺少必填字段：{missing}")

    @staticmethod
    def _parse_window(payload, service):
        date, start, end, area = (payload["date"], payload["start"],
                                  payload["end"], payload["area"])
        if not M.is_date(date):
            raise M.bad_request("日期格式应为YYYY-MM-DD")
        if not M.is_time(start) or not M.is_time(end):
            raise M.bad_request("时间格式应为HH:MM")
        if M.to_min(start) >= M.to_min(end):
            raise M.bad_request("时间窗开始须早于结束")
        need = M.SERVICE_MINUTES[service]
        if M.to_min(end) - M.to_min(start) < need:
            raise M.bad_request(f"时间窗不足{need}分钟，无法安排{M.SERVICE_NAMES[service]}")
        return date, start, end, area

    def _rider(self, rider_id):
        rider = self.store.riders.get(rider_id)
        if rider is None:
            raise M.not_found(f"骑手不存在：{rider_id}")
        return rider

    def _appointment(self, appointment_id):
        appt = self.store.appointments.get(appointment_id)
        if appt is None:
            raise M.not_found(f"预约不存在：{appointment_id}")
        return appt

    # ---- 基础数据 ----
    def list_points(self):
        return [p.to_dict() for p in self.store.points.values()]

    def list_doctors(self):
        return [d.to_dict() for d in self.store.doctors.values()]

    def list_teams(self):
        return [t.to_dict() for t in self.store.teams.values()]

    def reset(self):
        with self.store.lock:
            self.store = Store()
            seed.seed_static(self.store)
        return {"status": "reset"}

    # ---- 骑手 ----
    def register_rider(self, payload):
        with self.store.lock:
            cached = self._cached(payload)
            if cached is not None:
                return cached
            self._need(payload, "name", "area", "team_id")
            if payload["team_id"] not in self.store.teams:
                raise M.bad_request(f"未知家庭医生团队：{payload['team_id']}")
            insurance = payload.get("insurance", M.INSURANCE_NONE)
            if insurance not in M.INSURANCE_TYPES:
                raise M.bad_request(f"未知医保类型：{insurance}")
            package = payload.get("package", "none")
            if package not in billing.PACKAGE_VERSIONS:
                raise M.bad_request(f"未知服务包版本：{package}")

            def produce():
                rider_id = payload.get("id") or self.store.new_id("rider")
                if rider_id in self.store.riders:
                    raise M.conflict(f"骑手已存在：{rider_id}")
                rider = M.Rider(id=rider_id, name=payload["name"], area=payload["area"],
                                team_id=payload["team_id"], insurance=insurance,
                                package=package)
                self.store.riders[rider.id] = rider
                return rider.to_dict()

            return self._idempotent(payload, produce)

    def get_rider(self, rider_id):
        with self.store.lock:
            return self._rider(rider_id).to_dict()

    # ---- 时间窗与匹配 ----
    def submit_window(self, rider_id, payload):
        with self.store.lock:
            cached = self._cached(payload)
            if cached is not None:
                return cached
            rider = self._rider(rider_id)
            self._reject_order_details(payload)
            self._need(payload, "date", "start", "end", "area")
            service = payload.get("service", M.CONSULT)
            if service not in M.SERVICES:
                raise M.bad_request(f"未知服务类型：{service}")
            date, start, end, area = self._parse_window(payload, service)
            priority = payload.get("priority", 0)
            if priority not in M.PRIORITIES:
                raise M.bad_request("医疗优先级须为0/1/2")
            priority_reason = payload.get("priority_reason", "")
            if priority > 0 and not priority_reason:
                raise M.bad_request("医疗优先级须注明医学理由")

            def produce():
                window = M.Window(id=self.store.new_id("win"), rider_id=rider.id,
                                  date=date, start=start, end=end, area=area)
                self.store.windows[window.id] = window
                appt, preempted = scheduling.book(self.store, rider, window, service,
                                                  priority, priority_reason)
                return {"window": window.to_dict(),
                        "matched": appt.status == M.SCHEDULED,
                        "appointment": appt.to_dict(), "preempted": preempted}

            return self._idempotent(payload, produce)

    def list_rider_appointments(self, rider_id):
        with self.store.lock:
            self._rider(rider_id)
            appts = [a for a in self.store.appointments.values() if a.rider_id == rider_id]
            appts.sort(key=lambda a: (a.date, a.start or "99:99", a.created_seq))
            return [a.to_dict() for a in appts]

    def get_appointment(self, appointment_id):
        with self.store.lock:
            return self._appointment(appointment_id).to_dict()

    # ---- 改期与签到 ----
    def reschedule(self, appointment_id, payload, actor="clinician"):
        """临时接单或骑手主动调整：释放原槽位，按新时间窗重排。"""
        with self.store.lock:
            cached = self._cached(payload)
            if cached is not None:
                return cached
            appt = self._appointment(appointment_id)
            self._reject_order_details(payload)
            reason = payload.get("reason")
            if reason not in M.RESCHEDULE_REASONS:
                raise M.bad_request(f"改期原因须为：{list(M.RESCHEDULE_REASONS)}")
            expected = payload.get("expected_version")
            if expected is not None and expected != appt.version:
                raise M.conflict(f"预约版本已变化：当前{appt.version}，请刷新后重试")
            if appt.status in (M.COMPLETED, M.CANCELLED):
                raise M.conflict("已结束或已取消的预约不可改期")

            def produce():
                window_payload = payload.get("window")
                if not isinstance(window_payload, dict):
                    raise M.bad_request("缺少新的可用时间窗：window")
                self._need(window_payload, "date", "start", "end", "area")
                self._reject_order_details(window_payload)
                date, start, end, area = self._parse_window(window_payload, appt.service)
                old_window = self.store.windows.get(appt.window_id)
                window = M.Window(id=self.store.new_id("win"), rider_id=appt.rider_id,
                                  date=date, start=start, end=end, area=area)
                self.store.windows[window.id] = window
                preempted = scheduling.place_or_wait(self.store, appt, window, reason, actor)
                if old_window is not None:
                    old_window.status = "closed"
                window.status = "matched" if appt.status == M.SCHEDULED else "queued"
                scheduling.drain_waitlist(self.store)
                return {"appointment": appt.to_dict(), "preempted": preempted}

            return self._idempotent(payload, produce)

    def checkin(self, appointment_id, payload, actor="clinician"):
        with self.store.lock:
            cached = self._cached(payload)
            if cached is not None:
                return cached
            appt = self._appointment(appointment_id)
            self._need(payload, "arrival")
            arrival = payload["arrival"]
            if not M.is_time(arrival):
                raise M.bad_request("时间格式应为HH:MM")

            def produce():
                scheduling.checkin(self.store, appt, arrival, actor)
                return {"appointment": appt.to_dict()}

            return self._idempotent(payload, produce)

    def set_priority(self, appointment_id, payload, actor="clinician"):
        with self.store.lock:
            appt = self._appointment(appointment_id)
            priority = payload.get("priority")
            if priority not in M.PRIORITIES:
                raise M.bad_request("医疗优先级须为0/1/2")
            reason = payload.get("reason", "")
            if priority > 0 and not reason:
                raise M.bad_request("医疗优先级须注明医学理由")
            old = scheduling.snapshot(appt)
            appt.priority = priority
            appt.priority_reason = reason
            scheduling.record(self.store, appt, "priority", actor, old, note=reason)
            if appt.status == M.WAITING:
                scheduling.drain_waitlist(self.store, date=appt.date)
            return {"appointment": appt.to_dict()}

    # ---- 到诊转化 ----
    def record_outcome(self, appointment_id, payload, actor="clinician"):
        with self.store.lock:
            cached = self._cached(payload)
            if cached is not None:
                return cached
            appt = self._appointment(appointment_id)
            kind = payload.get("kind")
            if kind not in M.OUTCOMES:
                raise M.bad_request(f"到诊转化须为：{list(M.OUTCOMES)}")
            if appt.status not in (M.SCHEDULED, M.CHECKED_IN):
                raise M.conflict("仅已排定或已签到的预约可以登记转化")
            if kind == M.OUT_REFERRAL:
                self._need(payload, "referral_target")

            def produce():
                old = scheduling.snapshot(appt)
                appt.status = M.COMPLETED
                scheduling.record(self.store, appt, f"outcome:{kind}", actor, old,
                                  note=payload.get("note", ""))
                rider = self.store.riders[appt.rider_id]
                entry = {"id": self.store.new_id("he"), "date": appt.date,
                         "appointment_id": appt.id, "point_id": appt.point_id,
                         "doctor_id": appt.doctor_id, "outcome": kind,
                         "outcome_name": M.OUTCOME_NAMES[kind],
                         "diagnosis": payload.get("diagnosis", ""),
                         "note": payload.get("note", "")}
                self.store.health.setdefault(rider.id, []).append(entry)
                referral = None
                if kind == M.OUT_REFERRAL:
                    referral = {"id": self.store.new_id("ref"), "rider_id": rider.id,
                                "appointment_id": appt.id, "target": payload["referral_target"],
                                "reason": payload.get("referral_reason", ""),
                                "date": appt.date, "status": "open"}
                    self.store.referrals[referral["id"]] = referral
                tasks = followup.tasks_for_outcome(self.store, rider, appt, kind, payload)
                return {"appointment": appt.to_dict(),
                        "followups": [t.to_dict() for t in tasks],
                        "referral": referral}

            return self._idempotent(payload, produce)

    # ---- 排班与医生出诊变化 ----
    def create_shift(self, payload):
        with self.store.lock:
            cached = self._cached(payload)
            if cached is not None:
                return cached
            self._need(payload, "doctor_id", "point_id", "date", "start", "end")
            doctor = self.store.doctors.get(payload["doctor_id"])
            if doctor is None:
                raise M.bad_request(f"未知医生：{payload['doctor_id']}")
            point = self.store.points.get(payload["point_id"])
            if point is None:
                raise M.bad_request(f"未知服务点：{payload['point_id']}")
            if not M.is_date(payload["date"]):
                raise M.bad_request("日期格式应为YYYY-MM-DD")
            if not M.is_time(payload["start"]) or not M.is_time(payload["end"]):
                raise M.bad_request("时间格式应为HH:MM")
            if M.to_min(payload["start"]) >= M.to_min(payload["end"]):
                raise M.bad_request("排班开始须早于结束")
            services = tuple(payload.get("services") or point.capabilities)
            invalid = sorted(set(services) - set(point.capabilities))
            if invalid:
                raise M.bad_request(f"该服务点不提供：{invalid}")

            def produce():
                shift_id = payload.get("id") or self.store.new_id("shift")
                if shift_id in self.store.shifts:
                    raise M.conflict(f"排班已存在：{shift_id}")
                shift = M.Shift(shift_id, doctor.id, point.id, payload["date"],
                                payload["start"], payload["end"], services)
                self.store.shifts[shift.id] = shift
                scheduling.drain_waitlist(self.store, date=shift.date)
                return shift.to_dict()

            return self._idempotent(payload, produce)

    def list_shifts(self, date=None, point_id=None):
        with self.store.lock:
            shifts = [s for s in self.store.shifts.values()
                      if (date is None or s.date == date)
                      and (point_id is None or s.point_id == point_id)]
            shifts.sort(key=lambda s: (s.date, s.start, s.id))
            return [s.to_dict() for s in shifts]

    def change_shift(self, shift_id, payload, actor="clinician"):
        with self.store.lock:
            cached = self._cached(payload)
            if cached is not None:
                return cached
            shift = self.store.shifts.get(shift_id)
            if shift is None:
                raise M.not_found(f"排班不存在：{shift_id}")
            action = payload.get("action")
            if action not in ("cancel", "shorten", "move", "reassign"):
                raise M.bad_request("出诊变化须为：cancel/shorten/move/reassign")
            if action == "shorten":
                self._need(payload, "end")
                if not M.is_time(payload["end"]):
                    raise M.bad_request("时间格式应为HH:MM")
                if not M.to_min(shift.start) < M.to_min(payload["end"]) < M.to_min(shift.end):
                    raise M.bad_request("缩短后的结束时间须在原时段内")
            if action == "move":
                self._need(payload, "start", "end")
                if not M.is_time(payload["start"]) or not M.is_time(payload["end"]):
                    raise M.bad_request("时间格式应为HH:MM")
                if M.to_min(payload["start"]) >= M.to_min(payload["end"]):
                    raise M.bad_request("移动后的开始须早于结束")
            if action == "reassign":
                self._need(payload, "doctor_id")
                if payload["doctor_id"] not in self.store.doctors:
                    raise M.bad_request(f"未知医生：{payload['doctor_id']}")

            def produce():
                report = scheduling.apply_shift_change(self.store, shift, action, payload, actor)
                return {"shift": shift.to_dict(), **report}

            return self._idempotent(payload, produce)

    # ---- 可执行队列与容量 ----
    def queue(self, date, point_id=None):
        with self.store.lock:
            if not date:
                raise M.bad_request("缺少参数：date")
            if not M.is_date(date):
                raise M.bad_request("日期格式应为YYYY-MM-DD")
            if point_id is not None and point_id not in self.store.points:
                raise M.not_found(f"服务点不存在：{point_id}")
            return scheduling.build_queue(self.store, date, point_id)

    # ---- 费用估算 ----
    def cost_estimate(self, rider_id, service):
        with self.store.lock:
            rider = self._rider(rider_id)
            return billing.estimate_cost(rider, service)

    # ---- 随访 ----
    def rider_followups(self, rider_id):
        with self.store.lock:
            self._rider(rider_id)
            tasks = [t for t in self.store.followups.values() if t.rider_id == rider_id]
            tasks.sort(key=lambda t: (t.due_date, t.id))
            return [t.to_dict() for t in tasks]

    def team_followups(self, team_id, due_before=None):
        with self.store.lock:
            if team_id not in self.store.teams:
                raise M.not_found(f"团队不存在：{team_id}")
            if due_before is not None and not M.is_date(due_before):
                raise M.bad_request("日期格式应为YYYY-MM-DD")
            tasks = [t for t in self.store.followups.values()
                     if t.team_id == team_id
                     and (due_before is None or t.due_date <= due_before)]
            tasks.sort(key=lambda t: (t.due_date, t.id))
            return [t.to_dict() for t in tasks]

    def complete_followup(self, followup_id, payload, actor="clinician"):
        with self.store.lock:
            cached = self._cached(payload)
            if cached is not None:
                return cached
            task = self.store.followups.get(followup_id)
            if task is None:
                raise M.not_found(f"随访任务不存在：{followup_id}")
            if task.status != "pending":
                raise M.conflict("随访任务已处理")

            def produce():
                task.status = "done"
                if payload.get("note"):
                    task.note = f"{task.note}；{payload['note']}" if task.note else payload["note"]
                return {"followup": task.to_dict()}

            return self._idempotent(payload, produce)

    # ---- 身份证明核验（独立存储）----
    def record_identity_verification(self, payload):
        with self.store.lock:
            cached = self._cached(payload)
            if cached is not None:
                return cached
            self._need(payload, "rider_id", "method", "result")
            rider = self._rider(payload["rider_id"])
            if payload["result"] not in ("verified", "failed"):
                raise M.bad_request("核验结果须为verified/failed")

            def produce():
                record = {"id": self.store.new_id("idv"), "rider_id": rider.id,
                          "method": payload["method"], "result": payload["result"],
                          "verified_at": payload.get("verified_at", ""),
                          "note": payload.get("note", "")}
                self.store.identity[rider.id] = record
                if payload["result"] == "verified":
                    rider.insurance_verified = True
                return record

            return self._idempotent(payload, produce)

    def get_identity(self, rider_id):
        with self.store.lock:
            self._rider(rider_id)
            record = self.store.identity.get(rider_id)
            if record is None:
                raise M.not_found(f"暂无身份核验记录：{rider_id}")
            return record

    # ---- 健康档案（独立存储）----
    def get_health_record(self, rider_id):
        with self.store.lock:
            self._rider(rider_id)
            return {"rider_id": rider_id,
                    "entries": list(self.store.health.get(rider_id, [])),
                    "referrals": [r for r in self.store.referrals.values()
                                  if r["rider_id"] == rider_id]}

    # ---- 工会覆盖统计（去标识化）----
    def union_coverage(self, date=None):
        with self.store.lock:
            if date is not None and not M.is_date(date):
                raise M.bad_request("日期格式应为YYYY-MM-DD")
            return privacy.coverage_stats(self.store, date)
