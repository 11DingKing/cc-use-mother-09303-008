"""``python -m risk_service`` 启动 HTTP 服务。"""
from __future__ import annotations

import argparse

from .http_app import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="合作项目风险登记服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="data/risk_registry.sqlite3")
    parser.add_argument(
        "--controllable-clock", action="store_true",
        help="启用可控时钟（演示/测试定时复查，可通过 POST /clock 设置时间）",
    )
    args = parser.parse_args()
    httpd = serve(args.host, args.port, args.db, controllable_clock=args.controllable_clock)
    mode = "可控时钟" if args.controllable_clock else "系统时钟"
    print(f"风险登记服务已启动：http://{args.host}:{args.port}（{mode}，数据库 {args.db}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.server_close()


if __name__ == "__main__":
    main()
