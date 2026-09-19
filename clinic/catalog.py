"""服务目录、服务点类型与费用依据目录（均为示例费率，供对账核对）。"""

# 服务点三类：社区卫生服务中心 / 社区卫生服务站 / 工会驿站（外展）
LOCATION_CENTER = "center"
LOCATION_STATION = "station"
LOCATION_POST = "post"
LOCATION_TYPES = (LOCATION_CENTER, LOCATION_STATION, LOCATION_POST)

# 可排班服务
CONSULT = "consult"   # 十五分钟接诊
TCM = "tcm"           # 三十分钟中医干预
OUTREACH = "outreach"  # 工会驿站外展义诊
REFERRAL = "referral"  # 正式转诊（作为处置结果，不直接占普通号）

SERVICES = {
    CONSULT: {"name": "十五分钟接诊", "duration": 15, "locations": (LOCATION_CENTER, LOCATION_STATION, LOCATION_POST)},
    TCM: {"name": "三十分钟中医干预", "duration": 30, "locations": (LOCATION_CENTER, LOCATION_STATION)},
    OUTREACH: {"name": "外展义诊", "duration": 15, "locations": (LOCATION_POST,)},
}

# 医疗优先级
PRIORITY_NORMAL = "normal"
PRIORITY_URGENT = "urgent"
PRIORITIES = (PRIORITY_NORMAL, PRIORITY_URGENT)

# 医保身份
INS_EMPLOYEE = "employee"  # 职工医保
INS_RESIDENT = "resident"  # 居民医保
INS_SELF = "self_pay"      # 自费/未参保
INSURANCE_TYPES = (INS_EMPLOYEE, INS_RESIDENT, INS_SELF)

# 家庭医生服务包版本
PACKAGE_NONE = "none"
PACKAGES = {
    PACKAGE_NONE: "无服务包",
    "fd-v1": "家庭医生签约服务包 v1（2026 示例）",
}

# 处置结果
DISPO_INHOUSE = "inhouse_treatment"  # 院内治疗
DISPO_POST = "post_intervention"     # 驿站干预
DISPO_REFERRAL = "formal_referral"   # 正式转诊
DISPO_FOLLOWUP = "followup_only"     # 仅随访
DISPOSITIONS = (DISPO_INHOUSE, DISPO_POST, DISPO_REFERRAL, DISPO_FOLLOWUP)

# 约诊状态
ST_BOOKED = "booked"
ST_ARRIVED = "arrived"
ST_COMPLETED = "completed"
ST_NOSHOW = "no_show"
ST_CANCELLED = "cancelled"
ST_PENDING_RESCHEDULE = "pending_reschedule"
ST_RESCHEDULED = "rescheduled"

# 费用规则：金额单位为分。covered_fen 为统筹/服务包支付，self_fen 为个人支付。
# source 为依据出处，basis 为可读说明——估算单必须原样携带，供"费用依据"核对。
FEE_RULES = [
    {
        "service_code": CONSULT, "insurance": INS_EMPLOYEE, "package": PACKAGE_NONE,
        "total_fen": 1500, "covered_fen": 1200, "self_fen": 300,
        "basis": "职工医保普通门诊统筹：报销 80%，个人自付 20%（示例费率）",
        "source": "fee-manual://2026-sample/employee-consult",
    },
    {
        "service_code": CONSULT, "insurance": INS_RESIDENT, "package": PACKAGE_NONE,
        "total_fen": 1500, "covered_fen": 900, "self_fen": 600,
        "basis": "居民医保门诊统筹：报销 60%，个人自付 40%（示例费率）",
        "source": "fee-manual://2026-sample/resident-consult",
    },
    {
        "service_code": CONSULT, "insurance": INS_SELF, "package": PACKAGE_NONE,
        "total_fen": 1500, "covered_fen": 0, "self_fen": 1500,
        "basis": "自费：按门诊诊查费指导价全额自付（示例费率）",
        "source": "fee-manual://2026-sample/self-consult",
    },
    {
        "service_code": TCM, "insurance": INS_EMPLOYEE, "package": PACKAGE_NONE,
        "total_fen": 3000, "covered_fen": 2400, "self_fen": 600,
        "basis": "职工医保中医适宜技术：报销 80%（示例费率）",
        "source": "fee-manual://2026-sample/employee-tcm",
    },
    {
        "service_code": TCM, "insurance": INS_RESIDENT, "package": PACKAGE_NONE,
        "total_fen": 3000, "covered_fen": 1800, "self_fen": 1200,
        "basis": "居民医保中医适宜技术：报销 60%（示例费率）",
        "source": "fee-manual://2026-sample/resident-tcm",
    },
    {
        "service_code": TCM, "insurance": INS_SELF, "package": PACKAGE_NONE,
        "total_fen": 3000, "covered_fen": 0, "self_fen": 3000,
        "basis": "自费：中医干预项目指导价全额自付（示例费率）",
        "source": "fee-manual://2026-sample/self-tcm",
    },
    {
        "service_code": OUTREACH, "insurance": INS_EMPLOYEE, "package": PACKAGE_NONE,
        "total_fen": 0, "covered_fen": 0, "self_fen": 0,
        "basis": "工会驿站外展义诊：公共卫生服务，不向个人收费（示例）",
        "source": "fee-manual://2026-sample/outreach",
    },
    {
        "service_code": OUTREACH, "insurance": INS_RESIDENT, "package": PACKAGE_NONE,
        "total_fen": 0, "covered_fen": 0, "self_fen": 0,
        "basis": "工会驿站外展义诊：公共卫生服务，不向个人收费，与医保身份无关（示例）",
        "source": "fee-manual://2026-sample/outreach",
    },
    {
        "service_code": OUTREACH, "insurance": INS_SELF, "package": PACKAGE_NONE,
        "total_fen": 0, "covered_fen": 0, "self_fen": 0,
        "basis": "工会驿站外展义诊：公共卫生服务，不向个人收费，与医保身份无关（示例）",
        "source": "fee-manual://2026-sample/outreach",
    },
    {
        "service_code": REFERRAL, "insurance": INS_EMPLOYEE, "package": PACKAGE_NONE,
        "total_fen": 2000, "covered_fen": 1600, "self_fen": 400,
        "basis": "转诊对接：上级医院门诊按职工医保待遇结算（示例费率）",
        "source": "fee-manual://2026-sample/employee-referral",
    },
    {
        "service_code": REFERRAL, "insurance": INS_RESIDENT, "package": PACKAGE_NONE,
        "total_fen": 2000, "covered_fen": 1200, "self_fen": 800,
        "basis": "转诊对接：上级医院门诊按居民医保待遇结算（示例费率）",
        "source": "fee-manual://2026-sample/resident-referral",
    },
    {
        "service_code": REFERRAL, "insurance": INS_SELF, "package": PACKAGE_NONE,
        "total_fen": 2000, "covered_fen": 0, "self_fen": 2000,
        "basis": "转诊对接：自费全额（示例费率）",
        "source": "fee-manual://2026-sample/self-referral",
    },
]

# 服务包对费用规则的覆盖：fd-v1 将接诊个人自付封顶为 1 元（100 分），差额由服务包支付。
PACKAGE_ADJUSTMENTS = {
    "fd-v1": {
        CONSULT: {"covered_fen": 1400, "self_fen": 100,
                  "basis_suffix": "；家庭医生服务包 v1 将接诊自付封顶为 1 元"},
    },
}


def fee_rule(service_code, insurance, package_version=PACKAGE_NONE):
    """按 (服务, 医保身份, 服务包版本) 解析费用规则；找不到则回退自费并标注。"""
    if insurance not in INSURANCE_TYPES:
        insurance = INS_SELF
    for rule in FEE_RULES:
        if rule["service_code"] == service_code and rule["insurance"] == insurance and rule["package"] == PACKAGE_NONE:
            rule = dict(rule)
            adjustment = PACKAGE_ADJUSTMENTS.get(package_version, {}).get(service_code)
            if adjustment:
                rule["covered_fen"] = adjustment["covered_fen"]
                rule["self_fen"] = adjustment["self_fen"]
                rule["basis"] += adjustment["basis_suffix"]
                rule["source"] += f"|package:{package_version}"
                rule["package"] = package_version
            return rule
    raise KeyError(f"无费用规则: {service_code}/{insurance}")
