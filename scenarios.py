"""测试用场景构造辅助。"""

from clinic.clock import parse_minutes
from clinic.engine import ClinicService


def t(text):
    return parse_minutes(text)


def seed(service=None):
    """构造一个三处服务点、两个家庭医生团队的开诊场景。"""
    svc = service or ClinicService()
    # 三个片区各有服务点；A 片区三类齐全
    svc.register_location("loc-center-a", "A片区社区卫生服务中心", "center", "A")
    svc.register_location("loc-station-a", "A片区社区卫生服务站", "station", "A")
    svc.register_location("loc-post-a", "A片区工会驿站", "post", "A")
    svc.register_location("loc-station-b", "B片区社区卫生服务站", "station", "B")

    svc.register_practitioner("doc-li", "李医生", "team-1")
    svc.register_practitioner("doc-wang", "王医生", "team-1")
    svc.register_practitioner("doc-zhao", "赵中医", "team-2")
    return svc
