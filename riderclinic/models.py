"""领域模型与共享常量：实体、时间工具与错误类型。"""
from __future__ import annotations

import datetime as _dt
import re as _re
from dataclasses import dataclass, field

# ---- 服务类型 ----
CONSULT = "consult_15m"   # 十五分钟接诊
TCM = "tcm_30m"           # 三十分钟中医干预
OUTREACH = "outreach"     # 外展义诊
SERVICES = (CONSULT, TCM, OUTREACH)
SERVICE_MINUTES = {CONSULT: 15, TCM: 30, OUTREACH: 15}
SERVICE_NAMES = {CONSULT: "十五分钟接诊", TCM: "三十分钟中医干预", OUTREACH: "外展义诊"}

# ---- 预约状态 ----
SCHEDULED = "scheduled"    # 已排定
CHECKED_IN = "checked_in"  # 已签到
COMPLETED = "completed"    # 已完成（含到诊转化）
WAITING = "waiting"        # 候补中（未排入槽位）
CANCELLED = "cancelled"
ACTIVE_STATUSES = (SCHEDULED, CHECKED_IN)  # 占用槽位的状态

# ---- 医疗优先级：数值大者优先，普通排队不得覆盖医疗优先级 ----
PRIORITY_ROUTINE = 0
PRIORITY_ELEVATED = 1
PRIORITY_URGENT = 2
PRIORITIES = (PRIORITY_ROUTINE, PRIORITY_ELEVATED, PRIORITY_URGENT)
PRIORITY_NAMES = {PRIORITY_ROUTINE: "普通", PRIORITY_ELEVATED: "较高", PRIORITY_URGENT: "紧急"}

# ---- 改期原因（明确规则）----
REASON_BOOKED = "booked"              # 首次排定
REASON_TEMP_ORDER = "temp_order"      # 临时接单
REASON_RIDER = "rider_request"        # 骑手主动调整
REASON_LATE = "rider_late"            # 迟到超过宽限期
REASON_DOCTOR = "doctor_change"       # 医生出诊变化
REASON_PREEMPTED = "preempted"        # 被更高医疗优先级置换
REASON_WAITLIST = "waitlist_placed"   # 候补补位
RESCHEDULE_REASONS = (REASON_TEMP_ORDER, REASON_RIDER)

# ---- 到诊转化 ----
OUT_IN_HOSPITAL = "in_hospital"          # 院内治疗
OUT_STATION = "station_intervention"     # 驿站干预
OUT_REFERRAL = "formal_referral"         # 正式转诊
OUTCOMES = (OUT_IN_HOSPITAL, OUT_STATION, OUT_REFERRAL)
OUTCOME_NAMES = {OUT_IN_HOSPITAL: "院内治疗", OUT_STATION: "驿站干预", OUT_REFERRAL: "正式转诊"}

# ---- 医保身份 ----
INSURANCE_EMPLOYEE = "employee"  # 职工医保
INSURANCE_RESIDENT = "resident"  # 居民医保
INSURANCE_NONE = "none"
INSURANCE_TYPES = (INSURANCE_EMPLOYEE, INSURANCE_RESIDENT, INSURANCE_NONE)
INSURANCE_NAMES = {INSURANCE_EMPLOYEE: "职工医保", INSURANCE_RESIDENT: "居民医保", INSURANCE_NONE: "无医保"}

GRID_MINUTES = 15        # 槽位粒度
LATE_GRACE_MINUTES = 10  # 迟到宽限期


class ApiError(Exception):
    """携带 HTTP 状态码的业务错误。"""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def bad_request(message):
    return ApiError(400, "bad_request", message)


def unauthorized(message="缺少或未知的工作角色"):
    return ApiError(401, "unauthorized", message)


def forbidden(message="当前角色无权访问该资源"):
    return ApiError(403, "forbidden", message)


def not_found(message="资源不存在"):
    return ApiError(404, "not_found", message)


def conflict(message):
    return ApiError(409, "conflict", message)


# ---- 时间工具（日期 YYYY-MM-DD，时间 HH:MM）----
_TIME_RE = _re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def is_time(value):
    return isinstance(value, str) and bool(_TIME_RE.match(value))


def is_date(value):
    if not isinstance(value, str):
        return False
    try:
        _dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def to_min(hhmm):
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def to_hhmm(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def ceil_grid(minutes):
    """向上对齐到 15 分钟槽位边界。"""
    return ((minutes + GRID_MINUTES - 1) // GRID_MINUTES) * GRID_MINUTES


def add_days(date_str, days):
    return (_dt.date.fromisoformat(date_str) + _dt.timedelta(days=days)).isoformat()


# ---- 实体 ----
@dataclass
class Team:
    id: str
    name: str

    def to_dict(self):
        return {"id": self.id, "name": self.name}


@dataclass
class Doctor:
    id: str
    name: str
    team_id: str

    def to_dict(self):
        return {"id": self.id, "name": self.name, "team_id": self.team_id}


@dataclass
class ServicePoint:
    id: str
    name: str
    kind: str          # center / station / union_station
    area: str          # 服务片区
    capabilities: tuple

    def to_dict(self):
        return {"id": self.id, "name": self.name, "kind": self.kind,
                "area": self.area, "capabilities": list(self.capabilities)}


@dataclass
class Rider:
    id: str
    name: str
    area: str          # 常驻服务片区
    team_id: str       # 签约家庭医生团队（随访责任归属，不随服务点变化）
    insurance: str = INSURANCE_NONE
    package: str = "none"  # 家庭医生服务包版本
    insurance_verified: bool = False

    def to_dict(self):
        return {"id": self.id, "name": self.name, "area": self.area,
                "team_id": self.team_id, "insurance": self.insurance,
                "insurance_name": INSURANCE_NAMES[self.insurance],
                "package": self.package, "insurance_verified": self.insurance_verified}


@dataclass
class Window:
    """骑手可用时间窗：只记录开始、结束与服务片区，不关联配送订单。"""

    id: str
    rider_id: str
    date: str
    start: str
    end: str
    area: str
    status: str = "open"  # open / matched / queued / closed

    def to_dict(self):
        return {"id": self.id, "rider_id": self.rider_id, "date": self.date,
                "start": self.start, "end": self.end, "area": self.area,
                "status": self.status}


@dataclass
class Shift:
    id: str
    doctor_id: str
    point_id: str
    date: str
    start: str
    end: str
    services: tuple
    status: str = "active"  # active / cancelled

    def to_dict(self):
        return {"id": self.id, "doctor_id": self.doctor_id, "point_id": self.point_id,
                "date": self.date, "start": self.start, "end": self.end,
                "services": list(self.services), "status": self.status}


@dataclass
class Appointment:
    id: str
    rider_id: str
    window_id: str
    service: str
    date: str
    area: str
    minutes: int
    priority: int
    priority_reason: str
    created_seq: int
    start: str = ""
    point_id: str = ""
    doctor_id: str = ""
    team_id: str = ""
    status: str = WAITING
    version: int = 0
    late: bool = False
    waiting_seq: int = 0
    history: list = field(default_factory=list)

    def to_dict(self):
        return {"id": self.id, "rider_id": self.rider_id, "window_id": self.window_id,
                "service": self.service, "service_name": SERVICE_NAMES[self.service],
                "date": self.date, "area": self.area,
                "start": self.start or None,
                "end": to_hhmm(to_min(self.start) + self.minutes) if self.start else None,
                "minutes": self.minutes,
                "point_id": self.point_id or None, "doctor_id": self.doctor_id or None,
                "team_id": self.team_id or None,
                "status": self.status, "late": self.late,
                "priority": self.priority, "priority_name": PRIORITY_NAMES[self.priority],
                "priority_reason": self.priority_reason,
                "version": self.version, "history": list(self.history)}


@dataclass
class FollowUpTask:
    """随访任务：归属签约家庭医生团队，跨站点、跨日接续。"""

    id: str
    rider_id: str
    team_id: str
    kind: str      # referral_check / tcm_review / routine_check
    due_date: str
    origin_appointment_id: str
    note: str = ""
    status: str = "pending"  # pending / done / cancelled

    def to_dict(self):
        return {"id": self.id, "rider_id": self.rider_id, "team_id": self.team_id,
                "kind": self.kind, "due_date": self.due_date, "status": self.status,
                "origin_appointment_id": self.origin_appointment_id, "note": self.note}
