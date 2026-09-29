"""HTTP 接口集成测试：在随机端口上起真实服务，走完整 JSON 协议。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from urllib import error, parse, request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from risk_service import bootstrap, events as ev
from risk_service.clock import MutableClock
from risk_service.http_app import build_app


class HttpClient:
    def __init__(self, server) -> None:
        self.server = server

    def call(self, method: str, path: str, payload=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        encoded_path = parse.quote(path, safe="/?=&")
        req = request.Request(
            f"http://127.0.0.1:{self.server.server_address[1]}{encoded_path}",
            data=body, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class HttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, handler = build_app(":memory:", controllable_clock=True, start_at=bootstrap.START)
        bootstrap.seed_demo(self.svc)
        self.svc.clock.set(bootstrap.START)
        from http.server import ThreadingHTTPServer
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api = HttpClient(self.server)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def test_clock_is_controllable(self) -> None:
        status, body = self.api.call("GET", "/clock")
        self.assertEqual(status, 200)
        self.assertTrue(body["controllable"])
        status, body = self.api.call("POST", "/clock", {"now": "2026-09-25T09:00:00+00:00"})
        self.assertEqual(status, 200)
        self.assertTrue(body["now"].startswith("2026-09-25"))

    def test_gate_and_explain_endpoints(self) -> None:
        # 直接推进到剧情结束后的 9 月 25 日并补跑复查逻辑
        status, gate = self.api.call("GET", f"/gate/{ev.FLOW_PAYMENT}")
        self.assertEqual(status, 200)
        self.assertIn(gate["decision"], (ev.GATE_BLOCKED, ev.GATE_ALLOWED))
        status, risks = self.api.call("GET", "/risks")
        self.assertEqual(status, 200)
        self.assertTrue(risks["risks"])

    def test_register_rate_gate_flow_over_http(self) -> None:
        spec = bootstrap.RULESET_V1
        status, body = self.api.call("POST", "/risks", {
            "title": "接口登记风险",
            "source": {"name": "某企业", "dept": "企业合作部"},
            "target": {"name": "某班级", "dept": "招生处"},
            "owner": {"name": "测试员"},
            "mitigation": "观察",
            "facts": {"internships_suspended": True,
                      "payment_nodes_advancing": True, "affected_students": 32},
            "next_review_at": "2026-09-30T09:00:00+00:00",
        })
        self.assertEqual(status, 200, body)
        rid = body["risk_id"]
        self.assertEqual(body["rating"]["level"], "严重")
        status, gate = self.api.call("GET", f"/gate/{ev.FLOW_ENROLLMENT}")
        self.assertEqual(gate["decision"], ev.GATE_BLOCKED)
        status, history = self.api.call("GET", f"/risks/{rid}/history")
        self.assertEqual(status, 200)
        self.assertTrue(history["events"])
        # 404 与 400
        status, body = self.api.call("GET", "/no-such-path")
        self.assertEqual(status, 404)
        status, body = self.api.call("POST", f"/risks/{rid}/exceptions",
                                     {"action": "不存在的动作", "approver": "a", "reason": "r"})
        self.assertEqual(status, 400)

    def test_due_review_endpoint(self) -> None:
        self.api.call("POST", "/clock", {"now": "2026-12-31T00:00:00+00:00"})
        status, body = self.api.call("POST", "/reviews/run-due", {})
        self.assertEqual(status, 200)
        self.assertIn("due", body)

    def test_verify_chain_endpoint(self) -> None:
        status, body = self.api.call("GET", "/audit/verify-chain")
        self.assertEqual(status, 200)
        self.assertTrue(body["valid"])


if __name__ == "__main__":
    unittest.main()
