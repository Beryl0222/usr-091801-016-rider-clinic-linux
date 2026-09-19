"""排班容量、时间窗匹配、改期规则与可执行队列。

明确规则：
- 槽位以 15 分钟为粒度；十五分钟接诊占 1 格，三十分钟中医干预占连续 2 格。
- 匹配只使用可用时间窗与服务片区，不读取配送订单详情。
- 槽位选择：先签约家庭医生团队，再最早时间，再服务点剩余容量。
- 医疗优先级不能被普通排队覆盖：容量不足时，较高医疗优先级可置换严格
  更低优先级的预约，被置换者自动改期或进入候补。
- 迟到 10 分钟内保留槽位；超过宽限期重排到当天同服务点下一个可用槽位。
- 临时接单：释放原槽位并按新时间窗重排，释放的容量立即回流候补。
- 医生出诊变化：受影响预约按医疗优先级依次重排，先本服务点再本片区。
"""
from __future__ import annotations

from . import models as M


def snapshot(appt):
    return {"date": appt.date, "start": appt.start or None,
            "point_id": appt.point_id or None, "doctor_id": appt.doctor_id or None,
            "status": appt.status}


def record(store, appt, reason, actor, old, note=""):
    appt.version += 1
    appt.history.append({"seq": store.next_seq(), "reason": reason, "actor": actor,
                         "from": old, "to": snapshot(appt), "note": note})


def _overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end


def _doctor_appointments(store, doctor_id, date, exclude=None):
    return [a for a in store.appointments.values()
            if a.doctor_id == doctor_id and a.date == date
            and a.status in M.ACTIVE_STATUSES and a.id != exclude]


def slot_free(store, doctor_id, date, start_min, minutes, exclude=None):
    end_min = start_min + minutes
    for appt in _doctor_appointments(store, doctor_id, date, exclude):
        if _overlaps(start_min, end_min, M.to_min(appt.start), M.to_min(appt.start) + appt.minutes):
            return False
    return True


def _candidate_slots(store, rider, window, service, point_id=None, occupied_ok=False, exclude=None):
    duration = M.SERVICE_MINUTES[service]
    w_start, w_end = M.to_min(window.start), M.to_min(window.end)
    candidates = []
    for point in store.points.values():
        if point.area != window.area or service not in point.capabilities:
            continue
        if point_id and point.id != point_id:
            continue
        for shift in store.shifts.values():
            if (shift.point_id != point.id or shift.date != window.date
                    or shift.status != "active" or service not in shift.services):
                continue
            start = M.ceil_grid(max(M.to_min(shift.start), w_start))
            end = min(M.to_min(shift.end), w_end)
            while start + duration <= end:
                if occupied_ok or slot_free(store, shift.doctor_id, window.date, start, duration, exclude):
                    doctor = store.doctors[shift.doctor_id]
                    candidates.append({"point_id": point.id, "doctor_id": shift.doctor_id,
                                       "team_id": doctor.team_id, "start": start})
                start += M.GRID_MINUTES
    return candidates


def _point_free_units(store, point_id, date):
    free = 0
    for shift in store.shifts.values():
        if shift.point_id != point_id or shift.date != date or shift.status != "active":
            continue
        start = M.to_min(shift.start)
        while start + M.GRID_MINUTES <= M.to_min(shift.end):
            if slot_free(store, shift.doctor_id, date, start, M.GRID_MINUTES):
                free += 1
            start += M.GRID_MINUTES
    return free


def _slot_key(store, rider, date):
    def key(cand):
        team_match = 0 if cand["team_id"] == rider.team_id else 1
        return (team_match, cand["start"], -_point_free_units(store, cand["point_id"], date),
                cand["point_id"], cand["doctor_id"])
    return key


def find_slot(store, rider, window, service, point_id=None, exclude=None):
    candidates = _candidate_slots(store, rider, window, service,
                                  point_id=point_id, exclude=exclude)
    if not candidates:
        return None
    candidates.sort(key=_slot_key(store, rider, window.date))
    return candidates[0]


def find_preemptable(store, rider, window, service, priority):
    """为较高医疗优先级寻找可置换槽位：占用者须为严格更低优先级且未签到。"""
    duration = M.SERVICE_MINUTES[service]
    candidates = _candidate_slots(store, rider, window, service, occupied_ok=True)
    candidates.sort(key=_slot_key(store, rider, window.date))
    for cand in candidates:
        blockers = [a for a in _doctor_appointments(store, cand["doctor_id"], window.date)
                    if _overlaps(cand["start"], cand["start"] + duration,
                                 M.to_min(a.start), M.to_min(a.start) + a.minutes)]
        if blockers and all(a.status == M.SCHEDULED and a.priority < priority for a in blockers):
            return cand, blockers
    return None, []


def _assign(appt, cand):
    appt.point_id = cand["point_id"]
    appt.doctor_id = cand["doctor_id"]
    appt.team_id = cand["team_id"]
    appt.start = M.to_hhmm(cand["start"])
    appt.status = M.SCHEDULED


def _to_waiting(store, appt):
    appt.status = M.WAITING
    appt.start = ""
    appt.point_id = ""
    appt.doctor_id = ""
    appt.team_id = ""
    appt.waiting_seq = store.next_seq()


def place_or_wait(store, appt, window, reason, actor, same_point_only=False, note=""):
    """按时间窗为预约寻找槽位；先本服务点再本片区；找不到则置换或候补。"""
    rider = store.riders[appt.rider_id]
    old = snapshot(appt)
    point_id = appt.point_id or None
    cand = find_slot(store, rider, window, appt.service, point_id=point_id, exclude=appt.id)
    if cand is None and point_id and not same_point_only:
        cand = find_slot(store, rider, window, appt.service, exclude=appt.id)
    preempt_targets = []
    if cand is None and appt.priority > M.PRIORITY_ROUTINE:
        cand, preempt_targets = find_preemptable(store, rider, window, appt.service, appt.priority)
    if cand is not None:
        _assign(appt, cand)
    else:
        _to_waiting(store, appt)
    appt.date = window.date
    appt.area = window.area
    record(store, appt, reason, actor, old, note)
    preempted = []
    for blocker in preempt_targets:
        bump(store, blocker, actor)
        preempted.append(blocker.id)
    return preempted


def bump(store, appt, actor):
    """被更高医疗优先级置换：按原时间窗自动改期，否则进入候补。"""
    window = store.windows.get(appt.window_id)
    if window is None:
        old = snapshot(appt)
        _to_waiting(store, appt)
        record(store, appt, M.REASON_PREEMPTED, actor, old)
        return
    place_or_wait(store, appt, window, M.REASON_PREEMPTED, actor)


def book(store, rider, window, service, priority, priority_reason):
    appt = M.Appointment(
        id=store.new_id("appt"), rider_id=rider.id, window_id=window.id,
        service=service, date=window.date, area=window.area,
        minutes=M.SERVICE_MINUTES[service], priority=priority,
        priority_reason=priority_reason, created_seq=store.next_seq())
    store.appointments[appt.id] = appt
    preempted = place_or_wait(store, appt, window, M.REASON_BOOKED, rider.id)
    window.status = "matched" if appt.status == M.SCHEDULED else "queued"
    return appt, preempted


def checkin(store, appt, arrival, actor):
    """签到：宽限期内保留槽位；超过宽限期按明确规则重排到当天同服务点。"""
    if appt.status != M.SCHEDULED:
        raise M.conflict("仅已排定的预约可以签到")
    arr = M.to_min(arrival)
    start = M.to_min(appt.start)
    if arr <= start + M.LATE_GRACE_MINUTES:
        old = snapshot(appt)
        appt.status = M.CHECKED_IN
        appt.late = arr > start
        record(store, appt, "checkin", actor, old)
        return
    appt.late = True
    pseudo = M.Window(id=appt.window_id, rider_id=appt.rider_id, date=appt.date,
                      start=M.to_hhmm(M.ceil_grid(arr)), end="23:45", area=appt.area)
    place_or_wait(store, appt, pseudo, M.REASON_LATE, actor, same_point_only=True,
                  note=f"迟到{arr - start}分钟，超过宽限{M.LATE_GRACE_MINUTES}分钟")
    if appt.status == M.SCHEDULED:
        appt.status = M.CHECKED_IN


def apply_shift_change(store, shift, action, payload, actor):
    """医生出诊变化：取消、缩短、移动或换人；受影响预约按医疗优先级重排。"""
    old_range = (M.to_min(shift.start), M.to_min(shift.end))
    old_doctor = shift.doctor_id
    affected = [a for a in store.appointments.values()
                if a.doctor_id == old_doctor and a.date == shift.date
                and a.status in M.ACTIVE_STATUSES
                and _overlaps(M.to_min(a.start), M.to_min(a.start) + a.minutes, *old_range)]
    if action == "reassign":
        new_doctor = store.doctors[payload["doctor_id"]]
        shift.doctor_id = new_doctor.id
        for appt in affected:
            old = snapshot(appt)
            appt.doctor_id = new_doctor.id
            appt.team_id = new_doctor.team_id
            record(store, appt, M.REASON_DOCTOR, actor, old, note="出诊医生调整，时段不变")
        return {"displaced": [], "reassigned": [a.id for a in affected], "placements": []}

    if action == "cancel":
        new_range = old_range
    elif action == "shorten":
        new_range = (old_range[0], M.to_min(payload["end"]))
    else:  # move
        new_range = (M.to_min(payload["start"]), M.to_min(payload["end"]))
    displaced = [a for a in affected
                 if action == "cancel"
                 or not (new_range[0] <= M.to_min(a.start)
                         and M.to_min(a.start) + a.minutes <= new_range[1])]
    if action == "cancel":
        shift.status = "cancelled"
    elif action == "shorten":
        shift.end = payload["end"]
    else:
        shift.start = payload["start"]
        shift.end = payload["end"]
    displaced.sort(key=lambda a: (-a.priority, M.to_min(a.start), a.created_seq))
    placements = []
    for appt in displaced:
        window = store.windows.get(appt.window_id)
        if window is None:
            old = snapshot(appt)
            _to_waiting(store, appt)
            record(store, appt, M.REASON_DOCTOR, actor, old)
        else:
            place_or_wait(store, appt, window, M.REASON_DOCTOR, actor)
        placements.append({"appointment_id": appt.id, "status": appt.status,
                           "start": appt.start or None, "point_id": appt.point_id or None})
    drain_waitlist(store, date=shift.date)
    return {"displaced": [a.id for a in displaced], "reassigned": [],
            "placements": placements}


def drain_waitlist(store, date=None):
    """容量释放后按（医疗优先级，候补顺序）依次补位。"""
    pending = [a for a in store.appointments.values()
               if a.status == M.WAITING and (date is None or a.date == date)]
    pending.sort(key=lambda a: (-a.priority, a.waiting_seq))
    for appt in pending:
        window = store.windows.get(appt.window_id)
        if window is None:
            continue
        place_or_wait(store, appt, window, M.REASON_WAITLIST, "system")


def capacity_summary(store, date, point_id):
    """容量核对：15 分钟槽位总数、已约、可约（按服务类型）。"""
    total = booked = 0
    bookable = {M.CONSULT: 0, M.TCM: 0, M.OUTREACH: 0}
    for shift in store.shifts.values():
        if shift.point_id != point_id or shift.date != date or shift.status != "active":
            continue
        start = M.to_min(shift.start)
        end = M.to_min(shift.end)
        while start + M.GRID_MINUTES <= end:
            total += 1
            if slot_free(store, shift.doctor_id, date, start, M.GRID_MINUTES):
                for service in (M.CONSULT, M.OUTREACH):
                    if service in shift.services:
                        bookable[service] += 1
                if (M.TCM in shift.services and start + 2 * M.GRID_MINUTES <= end
                        and slot_free(store, shift.doctor_id, date, start, 2 * M.GRID_MINUTES)):
                    bookable[M.TCM] += 1
            else:
                booked += 1
            start += M.GRID_MINUTES
    return {"slot_minutes": M.GRID_MINUTES, "total_slots": total, "booked_slots": booked,
            "free_slots": total - booked, "bookable": bookable}


def _queue_entry(store, appt):
    rider = store.riders.get(appt.rider_id)
    doctor = store.doctors.get(appt.doctor_id)
    return {"appointment_id": appt.id, "rider_id": appt.rider_id,
            "rider_name": rider.name if rider else "",
            "service": appt.service, "service_name": M.SERVICE_NAMES[appt.service],
            "date": appt.date, "start": appt.start or None,
            "end": M.to_hhmm(M.to_min(appt.start) + appt.minutes) if appt.start else None,
            "minutes": appt.minutes,
            "doctor_id": appt.doctor_id or None,
            "doctor_name": doctor.name if doctor else None,
            "team_id": appt.team_id or None,
            "status": appt.status, "late": appt.late,
            "priority": appt.priority, "priority_name": M.PRIORITY_NAMES[appt.priority],
            "priority_reason": appt.priority_reason, "version": appt.version}


def build_queue(store, date, point_id=None):
    """开诊前的可执行队列：按开始时间排序，同时刻医疗优先级高者在前。"""
    points = [store.points[point_id]] if point_id else list(store.points.values())
    out = []
    for point in points:
        entries = [a for a in store.appointments.values()
                   if a.point_id == point.id and a.date == date and a.status in M.ACTIVE_STATUSES]
        entries.sort(key=lambda a: (M.to_min(a.start), -a.priority, a.created_seq))
        waiting = [a for a in store.appointments.values()
                   if a.status == M.WAITING and a.date == date and a.area == point.area]
        waiting.sort(key=lambda a: (-a.priority, a.waiting_seq))
        out.append({"point": point.to_dict(),
                    "capacity": capacity_summary(store, date, point.id),
                    "entries": [_queue_entry(store, a) for a in entries],
                    "waiting": [_queue_entry(store, a) for a in waiting]})
    return {"date": date, "points": out}
