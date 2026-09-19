"""静态基础数据：三处服务点、两支家庭医生团队与四名医生。"""
from . import models as M

POINTS = (
    M.ServicePoint("sp-center", "社区卫生服务中心", "center", "城东", (M.CONSULT, M.TCM)),
    M.ServicePoint("sp-station", "社区服务站", "station", "城西", (M.CONSULT,)),
    M.ServicePoint("sp-union", "工会驿站", "union_station", "城东", (M.OUTREACH, M.CONSULT, M.TCM)),
)

TEAMS = (
    M.Team("team-a", "家庭医生团队A"),
    M.Team("team-b", "家庭医生团队B"),
)

DOCTORS = (
    M.Doctor("d1", "医生甲", "team-a"),
    M.Doctor("d2", "医生乙", "team-a"),
    M.Doctor("d3", "医生丙", "team-b"),
    M.Doctor("d4", "医生丁", "team-b"),
)


def seed_static(store):
    for point in POINTS:
        store.points[point.id] = point
    for team in TEAMS:
        store.teams[team.id] = team
    for doctor in DOCTORS:
        store.doctors[doctor.id] = doctor
