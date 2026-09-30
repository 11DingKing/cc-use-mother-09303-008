"""HTTP 接口（标准库实现，无第三方依赖）。

路由概览：

- ``POST /risks`` / ``GET /risks`` / ``GET /risks/{id}``
- ``POST /risks/{id}/rate``                 评定（可指定 ruleset_version）
- ``POST /risks/{id}/mitigations``          登记缓解措施
- ``POST /risks/{id}/mitigations/{mid}/confirm``  落实并复评
- ``POST /risks/{id}/downgrade`` / ``/reopen`` / ``/close``
- ``POST /merges``                          风险合并
- ``POST /restrictions/{id}/exemptions``    例外批准
- ``GET  /restrictions/{id}/explain``       解释限制为何生效
- ``GET  /risks/{id}/decisions``            决定链
- ``GET  /flows`` / ``GET /flows/{key}/status``  流程是否被挂起及恢复记录
- ``GET  /reviews/due`` / ``POST /reviews/run``  定时复查
- ``GET  /rules`` / ``GET /rules/{version}``

``RiskHTTPServer`` 支持把状态快照持久化到文件：每次写操作后落盘，
启动时自动恢复。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlparse

from .models import AffectedObject, Owner, RiskSource
from .service import RiskService


def _ok(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _err(handler: BaseHTTPRequestHandler, status: int, message: str) -> None:
    _ok(handler, {"error": message}, status)


def _flow_summary(svc: RiskService, key: str) -> dict[str, Any]:
    """流程列表项：含当日是否放行及挂起数，与详情接口口径一致。"""
    status = svc.flow_status(key)
    return {
        "key": key,
        "flow_type": status["flow"]["flow_type"],
        "name": status["flow"]["name"],
        "running": status["running"],
        "active_blockers": len(status["blockers"]),
        "exempted_blockers": len(status["exempted_blockers"]),
        "warnings": len(status["warnings"]),
    }


class Handler(BaseHTTPRequestHandler):
    server: "RiskHTTPServer"

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静化
        if self.server.verbose:
            super().log_message(fmt, *args)

    # ------------------------------------------------------------ GET ----
    def do_GET(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path).rstrip("/") or "/"
        svc = self.server.service
        try:
            if path == "/risks":
                _ok(self, [r.to_dict() for r in svc.repo.list_risks()])
            elif path.startswith("/risks/") and path.endswith("/decisions"):
                rid = path.split("/")[2]
                _ok(self, svc.decision_chain(rid))
            elif path.startswith("/risks/") and len(path.strip("/").split("/")) == 2:
                _ok(self, svc.repo.get_risk(path.split("/")[2]).to_dict())
            elif path == "/rules":
                _ok(self, {"latest": svc.rules.latest.version,
                           "versions": svc.rules.versions(),
                           "rulesets": [svc.rules.get(v).spec() for v in svc.rules.versions()]})
            elif path.startswith("/rules/"):
                _ok(self, svc.rules.get(path.split("/")[2]).spec())
            elif path == "/flows":
                _ok(self, [_flow_summary(svc, f.key) for f in svc.repo.list_flows()])
            elif path.startswith("/flows/") and path.endswith("/status"):
                key = path[len("/flows/"):-len("/status")]
                _ok(self, svc.flow_status(key))
            elif path.startswith("/restrictions/") and path.endswith("/explain"):
                rid = path.split("/")[2]
                _ok(self, svc.explain_restriction(rid))
            elif path == "/reviews/due":
                _ok(self, [{"risk_id": r.id, "title": r.title,
                            "next_review_date": r.next_review_date}
                           for r in svc.due_reviews()])
            elif path == "/health":
                _ok(self, {"status": "ok", "today": svc.clock.today().isoformat()})
            else:
                _err(self, 404, f"无此路由：{path}")
        except KeyError as e:
            _err(self, 404, str(e))
        except Exception as e:  # noqa: BLE001
            _err(self, 400, str(e))

    # ----------------------------------------------------------- POST ----
    def do_POST(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path).rstrip("/") or "/"
        payload = self._read_json()
        svc = self.server.service
        try:
            with self.server.persist_lock:
                result = self._dispatch(path, payload, svc)
                self.server.persist()
            _ok(self, result, 201 if path in {"/risks", "/merges"} else 200)
        except KeyError as e:
            message = str(e).strip("'\"")
            if "不存在" in message:
                _err(self, 404, message)
            else:
                _err(self, 400, f"请求缺少字段：{message}")
        except (ValueError, TypeError) as e:
            _err(self, 400, str(e))

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"请求体不是合法 JSON：{e}") from None
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _dispatch(self, path: str, p: dict[str, Any], svc: RiskService) -> Any:
        actor = str(p.get("actor") or "项目秘书处")

        def need(key: str) -> Any:
            if key not in p:
                raise ValueError(f"请求缺少必填字段：{key}")
            return p[key]

        if path == "/risks":
            src = need("source")
            owner = need("owner")
            risk = svc.create_risk(
                actor=actor, title=need("title"),
                source=RiskSource(src["category"], src["department"],
                                  src.get("party", ""), src.get("detail", "")),
                affected=[AffectedObject(a["kind"], a["ref"], a.get("name", ""))
                          for a in need("affected")],
                owner=Owner(owner["name"], owner["role"], owner["department"]),
                description=p.get("description", ""),
                review_interval_days=int(p.get("review_interval_days", 30)),
            )
            return risk.to_dict()

        if path == "/merges":
            target = svc.merge_risks(list(need("source_ids")), need("target_id"),
                                     actor=actor, reason=p.get("reason", ""))
            return target.to_dict()

        if path == "/reviews/run":
            return {"results": svc.run_due_reviews(
                actor=str(p.get("actor") or "系统定时复查"),
                ruleset_version=p.get("ruleset_version"))}

        parts = path.strip("/").split("/")
        # /risks/{id}/...
        if len(parts) >= 2 and parts[0] == "risks":
            rid = parts[1]
            sub = parts[2] if len(parts) > 2 else None
            if sub == "rate":
                return svc.rate_risk(rid, actor=actor,
                                     ruleset_version=p.get("ruleset_version"),
                                     reason=p.get("reason", "")).to_dict()
            if sub == "mitigations" and len(parts) == 3:
                m = svc.add_mitigation(
                    rid, actor=actor, description=p["description"],
                    owner=p["owner"], due_date=p["due_date"])
                return m.to_dict()
            if len(parts) == 5 and parts[2] == "mitigations" and parts[4] == "confirm":
                return svc.confirm_mitigation(
                    rid, parts[3], actor=actor,
                    rerate=bool(p.get("rerate", True))).to_dict()
            if sub == "downgrade":
                return svc.downgrade_risk(
                    rid, p["new_level"], actor=actor, reason=p.get("reason", ""),
                    lift_restrictions=bool(p.get("lift_restrictions", True))).to_dict()
            if sub == "reopen":
                return svc.reopen_risk(rid, actor=actor, reason=p.get("reason", "")).to_dict()
            if sub == "close":
                return svc.close_risk(rid, actor=actor, reason=p.get("reason", "")).to_dict()

        # /restrictions/{id}/exemptions
        if len(parts) == 3 and parts[0] == "restrictions" and parts[2] == "exemptions":
            ex = svc.approve_exemption(
                parts[1], actor=actor, approver=p["approver"],
                reason=p["reason"], valid_until=p["valid_until"],
                scope=p.get("scope"))
            return ex.to_dict()

        raise ValueError(f"无此路由或方法不支持：{path}")


class RiskHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: RiskService, *,
                 state_path: str | None = None, verbose: bool = False) -> None:
        super().__init__(address, Handler)
        self.service = service
        self.state_path = state_path
        self.verbose = verbose
        self.persist_lock = threading.Lock()
        if state_path:
            from pathlib import Path
            sp = Path(state_path)
            if sp.exists():
                service.repo.load_from(sp)

    def persist(self) -> None:
        if self.state_path:
            self.service.repo.save_to(self.state_path)


def serve(host: str = "127.0.0.1", port: int = 8080, *,
          state_path: str | None = None, verbose: bool = False) -> RiskHTTPServer:
    import os

    from .clock import FixedClock, SystemClock

    today = os.environ.get("RISK_TODAY")
    clock = FixedClock(today) if today else SystemClock()
    server = RiskHTTPServer((host, port), RiskService(clock=clock),
                            state_path=state_path, verbose=verbose)
    return server
