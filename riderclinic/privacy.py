"""去标识化覆盖统计：工会视角只见到计数，不见身份与诊断。"""
from . import models as M


def coverage_stats(store, date=None):
    """按服务点聚合的覆盖统计：仅计数，不含骑手标识、不含诊断内容。"""
    points = {}
    for point in store.points.values():
        appts = [a for a in store.appointments.values()
                 if a.point_id == point.id and (date is None or a.date == date)]
        shifts = [s for s in store.shifts.values()
                  if s.point_id == point.id and (date is None or s.date == date)]
        if not appts and not shifts:
            continue
        by_service = {s: sum(1 for a in appts if a.service == s) for s in M.SERVICES}
        points[point.id] = {
            "point_name": point.name,
            "area": point.area,
            "booked": len(appts),
            "completed": sum(1 for a in appts if a.status == M.COMPLETED),
            "by_service": by_service,
            "unique_riders": len({a.rider_id for a in appts}),
        }
    waiting = sum(1 for a in store.appointments.values()
                  if a.status == M.WAITING and (date is None or a.date == date))
    return {"date": date, "points": points, "waiting": waiting,
            "note": "去标识化覆盖统计：仅计数，不含身份与诊断"}
