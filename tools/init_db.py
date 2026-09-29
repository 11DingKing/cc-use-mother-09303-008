"""初始化数据库（幂等），可选写入演示数据。

用法：
    python3 tools/init_db.py --db data/risk_registry.sqlite3          # 仅建库
    python3 tools/init_db.py --db data/risk_registry.sqlite3 --demo  # 建库并播种演示场景
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from risk_service.clock import MutableClock
from risk_service.service import RiskService
from risk_service.store import Store
from risk_service import bootstrap


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化风险登记数据库")
    parser.add_argument("--db", default="data/risk_registry.sqlite3")
    parser.add_argument("--demo", action="store_true", help="播种企业停供实习岗位演示场景")
    args = parser.parse_args()

    path = Path(args.db)
    path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(path)
    if args.demo:
        service = RiskService(store, MutableClock(bootstrap.START))
        report = bootstrap.seed_demo(service)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    store.close()
    print(f"数据库已就绪：{path}")


if __name__ == "__main__":
    main()
