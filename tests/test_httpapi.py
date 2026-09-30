"""HTTP 接口端到端测试（真实 socket + 标准库客户端）。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.request
from urllib.parse import quote
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from risk_register import FixedClock, Repository, RiskService  # noqa: E402
from risk_register.httpapi import RiskHTTPServer  # noqa: E402
from risk_register.rules import default_registry  # noqa: E402


class Client:
    def __init__(self, base: str) -> None:
        self.base = base

    def call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        encoded = quote(path, safe="/:")
        req = urllib.request.Request(self.base + encoded, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))


class HttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = str(Path(self.tmp.name) / "state.json")
        clock = FixedClock("2026-09-30")
        service = RiskService(Repository(), default_registry(), clock)
        self.server = RiskHTTPServer(("127.0.0.1", 0), service, state_path=self.state)
        self.clock = clock
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        self.api = Client(f"http://127.0.0.1:{self.server.server_address[1]}")

    def _stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_full_journey_over_http_with_persistence(self) -> None:
        # 登记
        code, risk = self.api.call("POST", "/risks", {
            "actor": "项目秘书处", "title": "某企业停止提供实习岗位",
            "source": {"category": "企业风险", "department": "企业合作部",
                       "party": "XX教育科技", "detail": "停止供给实习岗位"},
            "affected": [
                {"kind": "付款节点", "ref": "P2", "name": "第二期合作款"},
                {"kind": "招生批次", "ref": "E2026-FALL", "name": "秋季招生"},
                {"kind": "里程碑", "ref": "M3", "name": "实习安置验收"},
            ],
            "owner": {"name": "李工", "role": "风险责任人", "department": "风控办"},
        })
        self.assertEqual(code, 201)
        rid = risk["id"]

        # 评定
        code, rated = self.api.call("POST", f"/risks/{rid}/rate", {"actor": "王审批"})
        self.assertEqual(code, 200)
        self.assertEqual(rated["rating"]["level"], "极高")

        # 付款被阻断
        code, status = self.api.call("GET", "/flows/付款节点:P2/status")
        self.assertEqual(code, 200)
        self.assertFalse(status["running"])

        # 例外批准
        hold = next(b["restriction_id"] for b in status["blockers"]
                    if b["action"] == "冻结")
        code, ex = self.api.call("POST", f"/restrictions/{hold}/exemptions", {
            "actor": "王审批", "approver": "张校长", "reason": "紧急补贴",
            "valid_until": "2026-10-15"})
        self.assertEqual(code, 200)
        self.assertTrue(ex["decision_id"])

        # 决定链
        code, chain = self.api.call("GET", f"/risks/{rid}/decisions")
        self.assertEqual([d["type"] for d in chain][:2], ["登记", "等级评定"])

        # 规则版本可查
        code, rules = self.api.call("GET", "/rules")
        self.assertEqual(rules["latest"], "1.1.0")
        self.assertIn("1.0.0", rules["versions"])

        # 定时复查
        code, due = self.api.call("GET", "/reviews/due")
        self.assertEqual(code, 200)
        self.assertEqual(due, [])

    def test_state_file_persists_across_server_restart(self) -> None:
        code, risk = self.api.call("POST", "/risks", {
            "actor": "秘书处", "title": "持久化验证",
            "source": {"category": "协议", "department": "法务部", "party": "X"},
            "affected": [{"kind": "付款节点", "ref": "P9", "name": "尾款"}],
            "owner": {"name": "钱工", "role": "风险责任人", "department": "法务部"},
        })
        rid = risk["id"]
        self.api.call("POST", f"/risks/{rid}/rate", {"actor": "王审批"})

        service2 = RiskService(Repository(), default_registry(), FixedClock("2026-09-30"))
        server2 = RiskHTTPServer(("127.0.0.1", 0), service2, state_path=self.state)
        thread2 = threading.Thread(target=server2.serve_forever, daemon=True)
        thread2.start()
        try:
            api2 = Client(f"http://127.0.0.1:{server2.server_address[1]}")
            code, loaded = api2.call("GET", f"/risks/{rid}")
            self.assertEqual(code, 200)
            self.assertIsNotNone(loaded["rating"])
            code, flows = api2.call("GET", "/flows")
            self.assertTrue(any(f["key"] == "付款节点:P9" for f in flows))
        finally:
            server2.shutdown()
            server2.server_close()
            thread2.join(timeout=2)

    def test_unknown_route_and_bad_request(self) -> None:
        code, body = self.api.call("GET", "/nope")
        self.assertEqual(code, 404)
        self.assertIn("error", body)
        code, body = self.api.call("POST", "/risks", {"actor": "x"})
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
