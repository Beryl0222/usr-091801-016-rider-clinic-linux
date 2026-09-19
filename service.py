"""骑手碎片时间诊疗协同的运行入口。"""

import argparse
import json
from http.server import ThreadingHTTPServer

from riderclinic.app import App
from riderclinic.http_api import build_handler
from riderclinic.replay import load_scenario, run_scenario

SERVICE_ID = "rider-clinic"
SERVICE_NAME = "骑手碎片时间诊疗协同"


def health_payload():
    """构造健康检查数据。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


APP = App()
Handler = build_handler(APP, health_payload)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--replay", metavar="SCENARIO", help="重放场景文件并输出核对摘要")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["status"] == "ok"
        print("基础检查通过")
        return
    if args.replay:
        summary = run_scenario(App(), load_scenario(args.replay))
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
