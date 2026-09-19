"""场景重放：把资料中的临时派单与并发改期按序执行，输出核对摘要。

场景文件为 JSON：riders/shifts 为种子数据，ops 为操作序列；
{"parallel": [...]} 内的操作由多线程同时发起，用于重放并发改期。
摘要包含容量、费用依据、医疗优先级与跨日随访，供核对。
"""
import json
import re
import threading

from .app import App
from .models import ApiError

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CONTROL_KEYS = {"op", "as", "parallel"}


def load_scenario(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _payload(op, *extra_drop):
    drop = _CONTROL_KEYS | set(extra_drop)
    return {k: v for k, v in op.items() if k not in drop}


def _ref(value, refs):
    if isinstance(value, str) and value.startswith("@"):
        key = value[1:]
        if key not in refs or refs[key] is None:
            raise ApiError(400, "bad_request", f"未知引用：{value}")
        return refs[key]
    return value


def _extract(result):
    if isinstance(result, dict):
        appointment = result.get("appointment")
        if isinstance(appointment, dict):
            return appointment.get("id")
        if isinstance(result.get("followup"), dict):
            return result["followup"].get("id")
        return result.get("id")
    return None


def _exec_op(app, op, refs, log):
    name = op.get("op")
    result = None
    try:
        if name == "register_rider":
            result = app.register_rider(_payload(op))
        elif name == "create_shift":
            result = app.create_shift(_payload(op))
        elif name == "submit_window":
            result = app.submit_window(op["rider_id"], _payload(op, "rider_id"))
        elif name == "reschedule":
            result = app.reschedule(_ref(op["appointment"], refs), _payload(op, "appointment"))
        elif name == "checkin":
            result = app.checkin(_ref(op["appointment"], refs), _payload(op, "appointment"))
        elif name == "outcome":
            result = app.record_outcome(_ref(op["appointment"], refs), _payload(op, "appointment"))
        elif name == "priority":
            result = app.set_priority(_ref(op["appointment"], refs), _payload(op, "appointment"))
        elif name == "doctor_change":
            result = app.change_shift(op["shift_id"], _payload(op, "shift_id"))
        elif name == "complete_followup":
            result = app.complete_followup(_ref(op["followup_id"], refs),
                                           _payload(op, "followup_id"))
        elif name == "identity_verify":
            result = app.record_identity_verification(_payload(op))
        elif name == "cost_estimate":
            result = app.cost_estimate(op["rider_id"], op.get("service", "consult_15m"))
        else:
            raise ApiError(400, "bad_request", f"未知操作：{name}")
        entry = {"op": name, "ok": True, "result": result}
    except ApiError as err:
        entry = {"op": name, "ok": False, "status": err.status, "error": err.message}
    log.append(entry)
    if "as" in op:
        refs[op["as"]] = _extract(result)
    return entry


def _run_parallel(app, subs, refs, log):
    barrier = threading.Barrier(len(subs))
    results = [None] * len(subs)

    def work(index, sub):
        barrier.wait()
        sub_log = []
        _exec_op(app, sub, refs, sub_log)
        results[index] = sub_log[0]

    threads = [threading.Thread(target=work, args=(i, s)) for i, s in enumerate(subs)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    log.append({"op": "parallel", "results": results})


def run_scenario(app, scenario):
    refs = {}
    log = []
    for rider in scenario.get("riders", []):
        _exec_op(app, {"op": "register_rider", **rider}, refs, log)
    for shift in scenario.get("shifts", []):
        _exec_op(app, {"op": "create_shift", **shift}, refs, log)
    for op in scenario.get("ops", []):
        if "parallel" in op:
            _run_parallel(app, op["parallel"], refs, log)
        else:
            _exec_op(app, op, refs, log)
    return _summary(app, scenario, log)


def _summary(app, scenario, log):
    dates = sorted(set(_DATE_RE.findall(json.dumps(scenario, ensure_ascii=False))))
    store = app.store
    return {
        "name": scenario.get("name", ""),
        "log": log,
        "appointments": [a.to_dict() for a in
                         sorted(store.appointments.values(), key=lambda a: a.created_seq)],
        "followups": [t.to_dict() for t in
                      sorted(store.followups.values(), key=lambda t: (t.due_date, t.id))],
        "queues": {d: app.queue(d) for d in dates},
        "coverage": {d: app.union_coverage(d) for d in dates},
    }
