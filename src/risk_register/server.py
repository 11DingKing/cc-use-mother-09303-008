"""服务启动入口：``python -m risk_register.server``。

环境变量：

- ``RISK_HOST`` / ``RISK_PORT``：监听地址；
- ``RISK_STATE``：JSON 快照路径（设置后写操作自动落盘、重启自动恢复）；
- ``RISK_TODAY``：固定当前日期（YYYY-MM-DD），演示与测试定时复查用。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .httpapi import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="合作项目风险登记服务端")
    parser.add_argument("--host", default=os.environ.get("RISK_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("RISK_PORT", "8080")))
    parser.add_argument("--state", default=os.environ.get("RISK_STATE"),
                        help="JSON 快照持久化路径")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    server = serve(args.host, args.port, state_path=args.state, verbose=args.verbose)
    print(json.dumps({"msg": "风险登记服务已启动", "host": args.host, "port": args.port,
                      "state": args.state,
                      "today": server.service.clock.today().isoformat()},
                     ensure_ascii=False), file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
