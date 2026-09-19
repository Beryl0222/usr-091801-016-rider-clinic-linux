"""骑手碎片时间诊疗协同运行入口。

保留基线契约（SERVICE_ID / SERVICE_NAME / health_payload / Handler），
Handler 即完整领域 API 的请求处理器；健康检查形状保持不变。
"""

import argparse
import json
from http.server import ThreadingHTTPServer

from clinic.server import ApiState, build_handler_class

SERVICE_ID = "rider-clinic"
SERVICE_NAME = "骑手碎片时间诊疗协同"


def health_payload():
    """构造健康检查数据。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


# 进程级默认状态；测试若需隔离可自行用 make_server 构建
default_state = ApiState(health_extra={"service": SERVICE_ID, "name": SERVICE_NAME})
Handler = build_handler_class(default_state)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["status"] == "ok"
        # 导入期自检：领域模块与重放核对器可加载
        from clinic.engine import ClinicService
        from clinic.replay import audit
        assert audit([])["ok"] is True
        assert ClinicService() is not None
        print("基础检查通过")
        return
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
