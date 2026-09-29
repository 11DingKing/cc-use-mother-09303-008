"""基于标准库的 JSON HTTP 接口。

路由：

- ``POST /rulesets``                 发布版本化规则集
- ``GET  /rulesets/latest``          查看当前生效规则集
- ``POST /risks``                    登记风险（六要素）
- ``GET  /risks``                    风险清单
- ``GET  /risks/{id}``               风险详情（含限制、例外、恢复记录）
- ``GET  /risks/{id}/history``       决定链（原始事件序列）
- ``POST /risks/{id}/rate``          提交事实、按版本规则评级并传播
- ``POST /risks/{id}/restrictions``  手工增加限制
- ``POST /risks/{id}/release``       手工解除限制
- ``POST /risks/{id}/exceptions``    例外批准
- ``POST /exceptions/{id}/revoke``   撤销例外
- ``POST /merges``                   风险合并
- ``POST /reviews/run-due``          推进定时复查（可控时间见 /clock）
- ``POST /risks/{id}/reviews``       记录复查结论
- ``POST /risks/{id}/close`` / ``/reopen``
- ``GET  /gate/{flow}?target_ref=``  查询限制为何生效
- ``GET  /restrictions/{id}/explain`` 解释限制 + 解除后恢复了哪些流程
- ``GET  /clock`` / ``POST /clock``  读取/设置可控时间（仅可调时钟模式）
- ``GET  /audit/verify-chain``       校验事件哈希链
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import events as ev
from .clock import Clock, MutableClock, SystemClock
from .service import RiskService, ServiceError
from .store import Store, parse


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    moment = parse(value)
    if moment is None:
        raise ServiceError(f"无法解析时间：{value}")
    return moment


class RiskHttpHandler(BaseHTTPRequestHandler):
    service: RiskService
    server_version = "RiskRegistry/0.1"

    # 静默替换默认访问日志，改由统一 JSON 之外的方式输出
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        if getattr(self, "verbose_log", False):
            super().log_message(fmt, *args)

    # ------------------------------------------------------------------ 工具

    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ServiceError(f"请求体不是合法 JSON：{exc}")
        if not isinstance(value, dict):
            raise ServiceError("请求体必须是 JSON 对象")
        return value

    def _id(self, pattern: str) -> str | None:
        match = re.fullmatch(pattern, self.path_parsed.path)
        return unquote(match.group(1)) if match else None

    def _query(self, key: str, default: str | None = None) -> str | None:
        return self.query.get(key, [default])[0]

    # ------------------------------------------------------------------ 入口

    @property
    def path_parsed(self):
        return urlparse(self.path)

    @property
    def query(self):
        return parse_qs(self.path_parsed.query)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            self._route(method)
        except ServiceError as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:  # pragma: no cover - 兜底
            self._send(500, {"error": f"服务器内部错误：{exc}"})

    def _route(self, method: str) -> None:
        path = self.path_parsed.path
        body = self._read_json() if method == "POST" else {}
        svc: RiskService = self.service

        if method == "POST" and path == "/rulesets":
            return self._send(200, svc.publish_ruleset(
                body["spec"],
                effective_from=parse_dt(body.get("effective_from")),
                actor=body.get("actor", "项目秘书处"),
                note=body.get("note", ""),
                version=body.get("version"),
            ))
        if method == "GET" and path == "/rulesets/latest":
            ruleset = svc.store.latest_ruleset(at=svc.clock.now())
            return self._send(200, ruleset or {"error": "尚未发布规则集", "_status": 404})

        if method == "POST" and path == "/risks":
            return self._send(200, svc.register_risk(
                title=body["title"], source=body["source"], target=body["target"],
                owner=body["owner"], mitigation=body.get("mitigation", ""),
                planned_actions=body.get("planned_actions"),
                facts=body.get("facts"),
                next_review_at=parse_dt(body.get("next_review_at")),
                actor=body.get("actor", "项目秘书处"),
            ))
        if method == "GET" and path == "/risks":
            return self._send(200, {"risks": svc.store.list_risks()})

        risk_match = re.fullmatch(r"/risks/([^/]+)(/[^/]*)?", path)
        risk_id = unquote(risk_match.group(1)) if risk_match else None
        suffix = risk_match.group(2) if risk_match else None
        if risk_id and method == "GET" and not suffix:
            return self._send(200, svc.risk_detail(risk_id))
        if risk_id and method == "GET" and suffix == "/history":
            return self._send(200, {"risk_id": risk_id, "events": svc.history(risk_id)})
        if risk_id and method == "POST" and path.endswith("/rate"):
            return self._send(200, {"rating": svc.rate_risk(
                risk_id, body["facts"], actor=body.get("actor", "风险责任人"))})
        if risk_id and method == "POST" and path.endswith("/restrictions"):
            return self._send(200, svc.add_restriction(
                risk_id, body["action"], target_ref=body.get("target_ref", ev.TARGET_ALL),
                reason=body.get("reason", ""), actor=body.get("actor", "风险责任人")))
        if risk_id and method == "POST" and path.endswith("/release"):
            return self._send(200, svc.release_restriction(
                risk_id, body["action"], reason=body["reason"],
                actor=body.get("actor", "风险责任人")))
        if risk_id and method == "POST" and path.endswith("/exceptions"):
            return self._send(200, svc.grant_exception(
                risk_id, body["action"], approver=body["approver"], reason=body["reason"],
                valid_until=parse_dt(body.get("valid_until")),
                actor=body.get("actor")))
        if risk_id and method == "POST" and path.endswith("/reviews"):
            return self._send(200, svc.record_review(
                risk_id, body["conclusion"], note=body.get("note", ""),
                facts=body.get("facts"), next_review_at=parse_dt(body.get("next_review_at")),
                actor=body.get("actor", "风险责任人")))
        if risk_id and method == "POST" and path.endswith("/close"):
            return self._send(200, svc.close_risk(
                risk_id, reason=body["reason"], actor=body.get("actor", "项目秘书处")))
        if risk_id and method == "POST" and path.endswith("/reopen"):
            return self._send(200, svc.reopen_risk(
                risk_id, reason=body["reason"], next_review_at=parse_dt(body.get("next_review_at")),
                facts=body.get("facts"), actor=body.get("actor", "项目秘书处")))

        if method == "POST" and path == "/merges":
            return self._send(200, svc.merge_risks(
                body["source_risk_id"], body["target_risk_id"], reason=body["reason"],
                actor=body.get("actor", "项目秘书处")))

        if method == "POST" and path == "/reviews/run-due":
            return self._send(200, svc.run_due_reviews())

        exc_id = self._id(r"/exceptions/([^/]+)/revoke")
        if exc_id and method == "POST":
            return self._send(200, svc.revoke_exception(
                int(exc_id), reason=body["reason"], actor=body.get("actor", "审批人员")))

        flow = self._id(r"/gate/([^/]+)")
        if flow and method == "GET":
            return self._send(200, svc.gate_check(flow, target_ref=self._query("target_ref", ev.TARGET_ALL)))

        restriction_id = self._id(r"/restrictions/([^/]+)/explain")
        if restriction_id and method == "GET":
            return self._send(200, svc.explain_restriction(int(restriction_id)))

        if method == "GET" and path == "/clock":
            return self._send(200, {"now": svc.clock.now().isoformat(),
                                    "controllable": isinstance(svc.clock, MutableClock)})
        if method == "POST" and path == "/clock":
            if not isinstance(svc.clock, MutableClock):
                raise ServiceError("当前服务未启用可控时钟")
            svc.clock.set(parse_dt(body["now"]))
            return self._send(200, {"now": svc.clock.now().isoformat()})

        if method == "GET" and path == "/audit/verify-chain":
            return self._send(200, svc.verify_chain())

        self._send(404, {"error": f"未找到路由：{method} {path}"})


def build_app(db_path: str = ":memory:", *, controllable_clock: bool = False,
              start_at: datetime | None = None) -> tuple[RiskService, type[RiskHttpHandler]]:
    """构造服务与处理类。可控时钟用于演示与测试中的定时复查。"""
    store = Store(db_path)
    clock: Clock = MutableClock(start_at or SystemClock().now()) if controllable_clock else SystemClock()
    service = RiskService(store, clock)

    class _Handler(RiskHttpHandler):
        pass

    _Handler.service = service
    return service, _Handler


def serve(host: str = "127.0.0.1", port: int = 8080, db_path: str = "data/risk_registry.sqlite3",
          controllable_clock: bool = False) -> ThreadingHTTPServer:
    service, handler = build_app(db_path, controllable_clock=controllable_clock)
    httpd = ThreadingHTTPServer((host, port), handler)
    return httpd
