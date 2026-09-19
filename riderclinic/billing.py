"""费用估算：职工医保、居民医保与家庭医生服务包版本的依据说明。

计算顺序：基准价 → 服务包减免 → 医保统筹报销（对减免后余额）→ 个人自付。
每个数字都在 sources 中注明来源规则，便于核对费用依据。
"""
from . import models as M

PRICE_LIST_VERSION = "2025.09"
BASE_PRICES = {M.CONSULT: 30.0, M.TCM: 80.0, M.OUTREACH: 0.0}

INSURANCE_RULES = {
    M.INSURANCE_EMPLOYEE: (0.70, "职工医保门诊统筹"),
    M.INSURANCE_RESIDENT: (0.50, "居民医保门诊统筹"),
    M.INSURANCE_NONE: (0.0, "无医保"),
}

PACKAGE_VERSIONS = ("none", "basic-v1", "plus-v2")
PACKAGE_RULES = {
    "none": ({}, "未签约家庭医生服务包"),
    "basic-v1": ({M.CONSULT: 1.0}, "家庭医生服务包basic-v1"),
    "plus-v2": ({M.CONSULT: 1.0, M.TCM: 0.5}, "家庭医生服务包plus-v2"),
}


def estimate_cost(rider, service):
    if service not in BASE_PRICES:
        raise M.bad_request(f"未知服务类型：{service}")
    gross = BASE_PRICES[service]
    rules, package_label = PACKAGE_RULES.get(rider.package, PACKAGE_RULES["none"])
    package_ratio = rules.get(service, 0.0)
    package_cover = round(gross * package_ratio, 2)
    rest = round(gross - package_cover, 2)
    insurance_ratio, insurance_label = INSURANCE_RULES[rider.insurance]
    insurance_cover = round(rest * insurance_ratio, 2)
    out_of_pocket = round(rest - insurance_cover, 2)

    sources = [f"价格表{PRICE_LIST_VERSION}：{M.SERVICE_NAMES[service]}基准价{gross:.2f}元"]
    if service == M.OUTREACH:
        sources.append("工会驿站外展义诊：免收费用")
    if package_ratio:
        sources.append(f"{package_label}：本服务减免{package_ratio:.0%}，计{package_cover:.2f}元")
    else:
        sources.append(f"{package_label}：本服务无减免")
    if insurance_ratio:
        sources.append(f"{insurance_label}：报销{insurance_ratio:.0%}，计{insurance_cover:.2f}元")
    else:
        sources.append(f"{insurance_label}：个人全额自付")
    sources.append(f"个人自付合计{out_of_pocket:.2f}元")

    return {"rider_id": rider.id, "service": service,
            "service_name": M.SERVICE_NAMES[service], "currency": "CNY",
            "gross": gross, "package_cover": package_cover,
            "insurance_cover": insurance_cover, "out_of_pocket": out_of_pocket,
            "sources": sources,
            "basis": {"insurance_type": rider.insurance,
                      "insurance_name": M.INSURANCE_NAMES[rider.insurance],
                      "insurance_verified": rider.insurance_verified,
                      "package_version": rider.package,
                      "price_list_version": PRICE_LIST_VERSION}}
