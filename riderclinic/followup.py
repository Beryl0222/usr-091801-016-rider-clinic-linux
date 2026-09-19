"""随访任务生成：归属签约家庭医生团队，跨站点、跨日接续。"""
from . import models as M

REFERRAL_FOLLOW_DAYS = 3   # 正式转诊后随访时限
TCM_FOLLOW_DAYS = 7        # 中医干预后复评时限


def tasks_for_outcome(store, rider, appointment, kind, payload):
    """按到诊转化生成随访任务；团队取自骑手签约关系，不随服务点变化。"""
    days = payload.get("follow_up_days")
    tasks = []

    def make(task_kind, due_days, note):
        task = M.FollowUpTask(
            id=store.new_id("fu"), rider_id=rider.id, team_id=rider.team_id,
            kind=task_kind, due_date=M.add_days(appointment.date, due_days),
            origin_appointment_id=appointment.id, note=note)
        store.followups[task.id] = task
        tasks.append(task)

    if kind == M.OUT_REFERRAL:
        make("referral_check", days or REFERRAL_FOLLOW_DAYS, "正式转诊后随访")
    if appointment.service == M.TCM:
        make("tcm_review", days or TCM_FOLLOW_DAYS, "中医干预后复评")
    if not tasks and days:
        make("routine_check", days, "医嘱随访")
    return tasks
