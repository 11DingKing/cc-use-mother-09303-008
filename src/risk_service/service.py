"""应用服务：把领域动作落到事件与读模型上。

每个公开方法在一个数据库事务内完成“追加事件 + 更新投影”，
因此合并、降级、例外批准、复开等操作天然留下可追溯的决定链。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from . import events as ev
from .clock import Clock, SystemClock
from .rules import LEVEL_RANK, RuleSet, evaluate, validate_spec
from .store import Store, iso, parse


class ServiceError(Exception):
    """违反领域规则的操作。"""


class RiskService:
    def __init__(self, store: Store, clock: Clock | None = None) -> None:
        self.store = store
        self.clock = clock or SystemClock()

    def _now(self) -> datetime:
        return self.clock.now()

    # ================================================================ 规则集

    def publish_ruleset(
        self,
        spec: dict[str, Any],
        *,
        effective_from: datetime | None = None,
        actor: str = "项目秘书处",
        note: str = "",
        version: int | None = None,
    ) -> dict[str, Any]:
        """发布一版评级规则。版本号必须递增，生效时间不可回退。"""
        validate_spec(spec)
        now = self._now()
        effective_from = effective_from or now
        latest = self.store.latest_ruleset()
        if version is None:
            version = (latest["version"] + 1) if latest else 1
        if latest is not None:
            if version <= latest["version"]:
                raise ServiceError("规则版本号必须高于现有版本")
            if effective_from < latest["effective_from"]:
                raise ServiceError("新生效时间不得早于当前版本生效时间")
        ruleset = RuleSet(
            version=version,
            published_at=now,
            effective_from=effective_from,
            spec=spec,
            published_by=actor,
            note=note,
        )
        payload = ruleset.to_dict()
        with self.store.transaction():
            self.store.insert_ruleset(payload)
            event = self.store.append_event(
                ev.RULESET_PUBLISHED, None, payload, actor, now
            )
        return {"ruleset": payload, "event": event}

    def _ruleset_for_rating(self) -> dict[str, Any]:
        ruleset = self.store.latest_ruleset(at=self._now())
        if ruleset is None:
            raise ServiceError("尚未发布任何生效的评级规则集")
        return ruleset

    # ================================================================ 风险登记

    def register_risk(
        self,
        *,
        title: str,
        source: dict[str, Any],
        target: dict[str, Any],
        owner: dict[str, Any],
        mitigation: str = "",
        planned_actions: list[str] | None = None,
        facts: dict[str, Any] | None = None,
        next_review_at: datetime | None = None,
        actor: str = "项目秘书处",
    ) -> dict[str, Any]:
        """登记风险，串联六要素：来源、影响对象、责任人、缓解措施、限制动作、复查日期。"""
        self._validate_party("来源", source)
        self._validate_party("影响对象", target)
        self._validate_party("责任人", owner)
        for action in planned_actions or []:
            self._validate_action(action)
        now = self._now()
        risk_id = "R-" + uuid.uuid4().hex[:10].upper()
        risk = {
            "risk_id": risk_id,
            "status": ev.STATUS_OPEN,
            "title": title,
            "source": source,
            "target": target,
            "owner": owner,
            "mitigation": mitigation,
            "next_review_at": iso(next_review_at) if next_review_at else None,
            "current_level": None,
            "current_rule_version": None,
            "latest_rating_seq": None,
            "merged_into": None,
            "closed_at": None,
            "created_at": iso(now),
            "updated_at": iso(now),
        }
        result: dict[str, Any] = {"risk_id": risk_id}
        with self.store.transaction():
            self.store.insert_risk(risk)
            data = {k: risk[k] for k in (
                "risk_id", "title", "source", "target", "owner", "mitigation"
            )}
            data["planned_actions"] = list(planned_actions or [])
            data["next_review_at"] = risk["next_review_at"]
            self.store.append_event(ev.RISK_REGISTERED, risk_id, data, actor, now)
            for action in planned_actions or []:
                self._append_restriction(
                    risk_id, action, ev.ORIGIN_PLAN, "登记时预案限制", "*", actor, now
                )
            if facts is not None:
                result["rating"] = self._rate(risk_id, facts, actor, now)
        return result

    @staticmethod
    def _validate_party(label: str, party: Any) -> None:
        if not isinstance(party, dict) or not party.get("name"):
            raise ServiceError(f"{label}必须是含 name 的对象")

    @staticmethod
    def _validate_action(action: str) -> None:
        if action not in ev.ACTIONS:
            raise ServiceError(f"未知限制动作：{action}")

    # ================================================================ 评级/降级

    def rate_risk(self, risk_id: str, facts: dict[str, Any], *, actor: str = "风险责任人") -> dict[str, Any]:
        now = self._now()
        with self.store.transaction():
            return self._rate(risk_id, facts, actor, now)

    def _rate(self, risk_id: str, facts: dict[str, Any], actor: str, now: datetime) -> dict[str, Any]:
        risk = self._require_open_risk(risk_id)
        ruleset = self._ruleset_for_rating()
        verdict = evaluate(ruleset["spec"], facts)
        previous_level = risk["current_level"]
        data = {
            "risk_id": risk_id,
            "rule_version": ruleset["version"],
            "level": verdict["level"],
            "matched_rules": verdict["matched_rules"],
            "required_actions": verdict["required_actions"],
            "facts": verdict["facts"],
            "previous_level": previous_level,
        }
        event = self.store.append_event(ev.RISK_RATED, risk_id, data, actor, now)
        self.store.set_facts(risk_id, facts)
        self.store.update_risk(
            risk_id,
            current_level=verdict["level"],
            current_rule_version=ruleset["version"],
            latest_rating_seq=event["seq"],
            updated_at=iso(now),
        )

        changes = self._reconcile_restrictions(risk_id, verdict, ruleset, actor, now)
        if previous_level is not None and LEVEL_RANK[verdict["level"]] < LEVEL_RANK[previous_level]:
            self.store.append_event(
                ev.RISK_DOWNGRADED,
                risk_id,
                {
                    "risk_id": risk_id,
                    "from_level": previous_level,
                    "to_level": verdict["level"],
                    "rule_version": ruleset["version"],
                    "released_actions": changes["released"],
                },
                actor,
                now,
            )
        return {
            "seq": event["seq"],
            "rule_version": ruleset["version"],
            **verdict,
            "previous_level": previous_level,
            "changes": changes,
        }

    def _reconcile_restrictions(
        self, risk_id: str, verdict: dict[str, Any], ruleset: dict[str, Any],
        actor: str, now: datetime,
    ) -> dict[str, list[str]]:
        """按评级结果同步规则来源的限制：新增缺项、解除不再要求的项。"""
        added: list[str] = []
        released: list[str] = []
        active = {r["action"]: r for r in self.store.list_restrictions(risk_id, active_only=True)}
        for action in verdict["required_actions"]:
            self._validate_action(action)
            if action not in active:
                reason = self._rule_reason(action, verdict, ruleset)
                self._append_restriction(risk_id, action, ev.ORIGIN_RULE, reason, "*", actor, now)
                added.append(action)
        for action, restriction in list(active.items()):
            # 规则、登记预案、合并带入的限制都跟随当前评级；
            # 只有手工升级（MANUAL）是显式决定，降级时予以保留。
            if restriction["origin"] != ev.ORIGIN_MANUAL and action not in verdict["required_actions"]:
                self._release(
                    restriction,
                    f"规则 v{ruleset['version']} 评级为「{verdict['level']}」，不再要求{action}",
                    actor,
                    now,
                )
                released.append(action)
        if released:
            self._emit_restored(risk_id, released, actor, now)
        return {"added": added, "released": released}

    @staticmethod
    def _rule_reason(action: str, verdict: dict[str, Any], ruleset: dict[str, Any]) -> str:
        hits = "、".join(m["rule_id"] for m in verdict["matched_rules"]) or "默认规则"
        return f"规则 v{ruleset['version']}（命中 {hits}）评定为「{verdict['level']}」，要求{action}"

    # ================================================================ 限制动作

    def add_restriction(
        self, risk_id: str, action: str, *, target_ref: str = ev.TARGET_ALL,
        reason: str = "", actor: str = "风险责任人",
    ) -> dict[str, Any]:
        self._validate_action(action)
        now = self._now()
        with self.store.transaction():
            self._require_open_risk(risk_id)
            created = self._append_restriction(
                risk_id, action, ev.ORIGIN_MANUAL, reason or "手工升级", target_ref, actor, now
            )
        return created

    def release_restriction(self, risk_id: str, action: str, *, reason: str, actor: str = "风险责任人") -> dict[str, Any]:
        now = self._now()
        with self.store.transaction():
            self._require_open_risk(risk_id)
            restriction = self.store.find_active_restriction(risk_id, action)
            if restriction is None:
                raise ServiceError(f"风险 {risk_id} 当前没有生效中的「{action}」")
            event = self._release(restriction, reason, actor, now)
            self._emit_restored(risk_id, [action], actor, now)
        return event

    def _append_restriction(
        self, risk_id: str, action: str, origin: str, reason: str,
        target_ref: str, actor: str, now: datetime,
    ) -> dict[str, Any]:
        flow = ev.ACTION_FLOW[action]
        event = self.store.append_event(
            ev.RISK_CONTROL_ADDED,
            risk_id,
            {
                "risk_id": risk_id,
                "action": action,
                "flow": flow,
                "target_ref": target_ref,
                "origin": origin,
                "reason": reason,
            },
            actor,
            now,
        )
        restriction_id = self.store.add_restriction(
            {
                "risk_id": risk_id,
                "action": action,
                "flow": flow,
                "target_ref": target_ref,
                "origin": origin,
                "reason": reason,
                "added_seq": event["seq"],
                "added_at": iso(now),
            }
        )
        self.store.update_risk(risk_id, updated_at=iso(now))
        return {"restriction_id": restriction_id, "event_seq": event["seq"], "action": action, "flow": flow}

    def _release(self, restriction: dict[str, Any], reason: str, actor: str, now: datetime) -> dict[str, Any]:
        event = self.store.append_event(
            ev.RESTRICTION_RELEASED,
            restriction["risk_id"],
            {
                "risk_id": restriction["risk_id"],
                "restriction_id": restriction["id"],
                "action": restriction["action"],
                "flow": restriction["flow"],
                "target_ref": restriction["target_ref"],
                "reason": reason,
            },
            actor,
            now,
        )
        self.store.release_restriction(restriction["id"], event["seq"], now, reason)
        self.store.update_risk(restriction["risk_id"], updated_at=iso(now))
        return {"restriction_id": restriction["id"], "event_seq": event["seq"]}

    def _emit_restored(self, risk_id: str, actions: list[str], actor: str, now: datetime) -> None:
        """解除后检查每个受影响流程是否已无任何阻断，有则记录“恢复了哪些流程”。"""
        for action in actions:
            flow = ev.ACTION_FLOW[action]
            remaining = [
                r for r in self.store.all_active_restrictions()
                if r["flow"] == flow and r["risk_id"] != risk_id
            ]
            own = [
                r for r in self.store.list_restrictions(risk_id, active_only=True)
                if r["flow"] == flow
            ]
            restored = not remaining and not own
            self.store.append_event(
                ev.FLOW_RESTORED,
                risk_id,
                {
                    "risk_id": risk_id,
                    "flow": flow,
                    "action": action,
                    "restored": restored,
                    "remaining_blocker_count": len(remaining) + len(own),
                    "resumes_what": f"{flow}流程已恢复，审批可继续推进" if restored else f"{flow}流程仍受其他风险限制",
                },
                actor,
                now,
            )

    # ================================================================ 例外批准

    def grant_exception(
        self, risk_id: str, action: str, *, approver: str, reason: str,
        valid_until: datetime | None = None, actor: str | None = None,
    ) -> dict[str, Any]:
        self._validate_action(action)
        if not approver or not reason:
            raise ServiceError("例外批准必须提供批准人与理由")
        now = self._now()
        actor = actor or approver
        with self.store.transaction():
            self._require_open_risk(risk_id)
            restriction = self.store.find_active_restriction(risk_id, action)
            if restriction is None:
                raise ServiceError("只能对生效中的限制批准例外")
            if valid_until is not None and valid_until <= now:
                raise ServiceError("例外有效期必须晚于当前时间")
            event = self.store.append_event(
                ev.EXCEPTION_GRANTED,
                risk_id,
                {
                    "risk_id": risk_id,
                    "restriction_id": restriction["id"],
                    "action": action,
                    "flow": restriction["flow"],
                    "approver": approver,
                    "reason": reason,
                    "valid_until": iso(valid_until) if valid_until else None,
                },
                actor,
                now,
            )
            exception_id = self.store.grant_exception(
                {
                    "risk_id": risk_id,
                    "action": action,
                    "approver": approver,
                    "reason": reason,
                    "granted_seq": event["seq"],
                    "granted_at": iso(now),
                    "valid_until": iso(valid_until) if valid_until else None,
                }
            )
        return {"exception_id": exception_id, "event_seq": event["seq"]}

    def revoke_exception(self, exception_id: int, *, reason: str, actor: str = "审批人员") -> dict[str, Any]:
        now = self._now()
        with self.store.transaction():
            exc = self.store.get_exception(exception_id)
            if exc is None or exc["status"] != ev.EXC_ACTIVE:
                raise ServiceError("例外不存在或已失效，无法撤销")
            event = self.store.append_event(
                ev.EXCEPTION_REVOKED,
                exc["risk_id"],
                {"exception_id": exception_id, "risk_id": exc["risk_id"],
                 "action": exc["action"], "reason": reason},
                actor,
                now,
            )
            self.store.close_exception(exception_id, ev.EXC_REVOKED, event["seq"], now)
        return {"exception_id": exception_id, "event_seq": event["seq"]}

    def _expire_exceptions(self, now: datetime) -> list[dict[str, Any]]:
        expired = []
        for exc in self.store.expirable_exceptions(now):
            event = self.store.append_event(
                ev.EXCEPTION_EXPIRED,
                exc["risk_id"],
                {"exception_id": exc["id"], "risk_id": exc["risk_id"],
                 "action": exc["action"], "valid_until": exc["valid_until"]},
                "系统时钟",
                now,
            )
            self.store.close_exception(exc["id"], ev.EXC_EXPIRED, event["seq"], now)
            expired.append({"exception_id": exc["id"], "event_seq": event["seq"]})
        return expired

    # ================================================================ 风险合并

    def merge_risks(self, source_id: str, target_id: str, *, reason: str, actor: str = "项目秘书处") -> dict[str, Any]:
        """把源风险并入目标风险；源风险生效中的限制动作传播（并入）到目标风险。"""
        if source_id == target_id:
            raise ServiceError("不能合并到自身")
        now = self._now()
        inherited: list[str] = []
        with self.store.transaction():
            source = self._require_open_risk(source_id)
            target = self._require_open_risk(target_id)
            target_actions = {
                r["action"] for r in self.store.list_restrictions(target_id, active_only=True)
            }
            for restriction in self.store.list_restrictions(source_id, active_only=True):
                if restriction["action"] in target_actions:
                    continue
                self._append_restriction(
                    target_id,
                    restriction["action"],
                    ev.ORIGIN_MERGE,
                    f"由风险 {source_id} 合并带入（{restriction['reason']}）",
                    restriction["target_ref"],
                    actor,
                    now,
                )
                target_actions.add(restriction["action"])
                inherited.append(restriction["action"])
            self.store.update_risk(
                source_id, status=ev.STATUS_MERGED, merged_into=target_id, updated_at=iso(now)
            )
            event = self.store.append_event(
                ev.RISK_MERGED,
                source_id,
                {"source_risk_id": source_id, "target_risk_id": target_id,
                 "reason": reason, "inherited_actions": inherited},
                actor,
                now,
            )
        return {"source_risk_id": source_id, "target_risk_id": target_id,
                "inherited_actions": inherited, "event_seq": event["seq"]}

    # ================================================================ 定时复查

    def run_due_reviews(self) -> dict[str, Any]:
        """推进时钟动作：过期例外失效，并为到期风险各发一条复查提醒事件。

        同一复查周期（``next_review_at`` 未变化）只提醒一次，
        记录复查结论后才会进入下一周期。
        """
        now = self._now()
        due_events: list[dict[str, Any]] = []
        with self.store.transaction():
            expired = self._expire_exceptions(now)
            for risk in self.store.due_risks(now):
                if self._due_already_signalled(risk["risk_id"], risk["next_review_at"]):
                    continue
                event = self.store.append_event(
                    ev.RISK_REVIEW_DUE,
                    risk["risk_id"],
                    {"risk_id": risk["risk_id"], "due_at": risk["next_review_at"],
                     "owner": risk["owner"]},
                    "系统时钟",
                    now,
                )
                due_events.append({"risk_id": risk["risk_id"], "event_seq": event["seq"]})
        return {"at": iso(now), "expired_exceptions": expired, "due": due_events}

    def _due_already_signalled(self, risk_id: str, due_at: str | None) -> bool:
        history = self.store.list_events(risk_id)
        for item in reversed(history):
            if item["event_type"] == ev.RISK_REVIEW_DUE and item["data"].get("due_at") == due_at:
                return True
            if item["event_type"] in (ev.RISK_REVIEWED, ev.RISK_RATED):
                return False
        return False

    def record_review(
        self, risk_id: str, conclusion: str, *, note: str = "",
        facts: dict[str, Any] | None = None, next_review_at: datetime | None = None,
        actor: str = "风险责任人",
    ) -> dict[str, Any]:
        """记录复查结论；RESOLVED 解除全部限制并关闭，ADJUSTED 可带新事实重新评级。"""
        if conclusion not in (ev.REVIEW_CONFIRMED, ev.REVIEW_ADJUSTED, ev.REVIEW_RESOLVED):
            raise ServiceError("复查结论非法")
        now = self._now()
        rating = None
        with self.store.transaction():
            self._require_open_risk(risk_id)
            data: dict[str, Any] = {
                "risk_id": risk_id,
                "conclusion": conclusion,
                "note": note,
                "next_review_at": iso(next_review_at) if next_review_at else None,
            }
            if facts is not None:
                rating = self._rate(risk_id, facts, actor, now)
                data["rating_seq"] = rating["seq"]
            event = self.store.append_event(ev.RISK_REVIEWED, risk_id, data, actor, now)
            updates: dict[str, Any] = {
                "next_review_at": iso(next_review_at) if next_review_at else None,
                "updated_at": iso(now),
            }
            if conclusion == ev.REVIEW_RESOLVED:
                self._release_all(risk_id, "复查确认风险已解除", actor, now)
                updates.update(status=ev.STATUS_CLOSED, closed_at=iso(now))
            self.store.update_risk(risk_id, **updates)
            if conclusion == ev.REVIEW_RESOLVED:
                self.store.append_event(
                    ev.RISK_CLOSED, risk_id,
                    {"risk_id": risk_id, "reason": "复查确认风险已解除", "review_seq": event["seq"]},
                    actor, now,
                )
        return {"risk_id": risk_id, "conclusion": conclusion, "rating": rating, "event_seq": event["seq"]}

    # ================================================================ 关闭/复开

    def close_risk(self, risk_id: str, *, reason: str, actor: str = "项目秘书处") -> dict[str, Any]:
        now = self._now()
        with self.store.transaction():
            self._require_open_risk(risk_id)
            self._release_all(risk_id, reason, actor, now)
            self.store.update_risk(
                risk_id, status=ev.STATUS_CLOSED, closed_at=iso(now), updated_at=iso(now)
            )
            event = self.store.append_event(
                ev.RISK_CLOSED, risk_id, {"risk_id": risk_id, "reason": reason}, actor, now
            )
        return {"risk_id": risk_id, "event_seq": event["seq"]}

    def reopen_risk(
        self, risk_id: str, *, reason: str, next_review_at: datetime | None = None,
        facts: dict[str, Any] | None = None, actor: str = "项目秘书处",
    ) -> dict[str, Any]:
        """复开已关闭风险：状态回到 OPEN，并按当前规则重新评定、传播限制。"""
        now = self._now()
        rating = None
        with self.store.transaction():
            risk = self.store.get_risk(risk_id)
            if risk is None:
                raise ServiceError("风险不存在")
            if risk["status"] != ev.STATUS_CLOSED:
                raise ServiceError("只有已关闭的风险可以复开")
            self.store.update_risk(
                risk_id, status=ev.STATUS_OPEN, closed_at=None,
                next_review_at=iso(next_review_at) if next_review_at else None,
                updated_at=iso(now),
            )
            event = self.store.append_event(
                ev.RISK_REOPENED,
                risk_id,
                {"risk_id": risk_id, "reason": reason,
                 "next_review_at": iso(next_review_at) if next_review_at else None},
                actor,
                now,
            )
            facts = facts if facts is not None else self.store.get_facts(risk_id)
            if facts:
                rating = self._rate(risk_id, facts, actor, now)
        return {"risk_id": risk_id, "rating": rating, "event_seq": event["seq"]}

    def _release_all(self, risk_id: str, reason: str, actor: str, now: datetime) -> list[str]:
        actions = [r["action"] for r in self.store.list_restrictions(risk_id, active_only=True)]
        for restriction in self.store.list_restrictions(risk_id, active_only=True):
            self._release(restriction, reason, actor, now)
        if actions:
            self._emit_restored(risk_id, actions, actor, now)
        return actions

    # ================================================================ 查询/解释

    def gate_check(self, flow: str, target_ref: str = ev.TARGET_ALL) -> dict[str, Any]:
        """查询某流程当前是否放行，并解释每条限制为何生效/为何被例外豁免。"""
        if flow not in ev.FLOWS:
            raise ServiceError(f"未知流程：{flow}")
        now = self._now()
        with self.store.transaction():
            expired = self._expire_exceptions(now)
            blockers: list[dict[str, Any]] = []
            hard_blocked = False
            for restriction in self.store.all_active_restrictions():
                if restriction["flow"] != flow:
                    continue
                if restriction["target_ref"] != ev.TARGET_ALL and restriction["target_ref"] != target_ref:
                    continue
                risk = self.store.get_risk(restriction["risk_id"])
                exc = self.store.active_exception(restriction["risk_id"], restriction["action"], now)
                entry = {
                    "restriction_id": restriction["id"],
                    "risk_id": restriction["risk_id"],
                    "risk_title": risk["title"],
                    "action": restriction["action"],
                    "target_ref": restriction["target_ref"],
                    "origin": restriction["origin"],
                    "why_in_effect": restriction["reason"],
                    "added_seq": restriction["added_seq"],
                    "added_at": restriction["added_at"],
                }
                if exc is None:
                    entry["status"] = ev.GATE_BLOCKED
                    hard_blocked = True
                else:
                    entry["status"] = ev.GATE_ALLOWED_WITH_EXCEPTION
                    entry["exception"] = {
                        "exception_id": exc["id"],
                        "approver": exc["approver"],
                        "reason": exc["reason"],
                        "valid_until": exc["valid_until"],
                        "granted_seq": exc["granted_seq"],
                    }
                blockers.append(entry)
            if not blockers:
                decision = ev.GATE_ALLOWED
            elif hard_blocked:
                decision = ev.GATE_BLOCKED
            else:
                decision = ev.GATE_ALLOWED_WITH_EXCEPTION
        return {
            "flow": flow,
            "target_ref": target_ref,
            "at": iso(now),
            "decision": decision,
            "expired_exceptions": expired,
            "restrictions": blockers,
        }

    def explain_restriction(self, restriction_id: int) -> dict[str, Any]:
        """给出单条限制的完整因果：生效依据、评级快照、例外与解除、恢复的流程。"""
        record = self.store.conn.execute(
            "SELECT * FROM restrictions WHERE id = ?", (restriction_id,)
        ).fetchone()
        if record is None:
            raise ServiceError("限制不存在")
        restriction = dict(record)
        risk = self.store.get_risk(restriction["risk_id"])
        chain: list[dict[str, Any]] = []
        restored: list[dict[str, Any]] = []
        for item in self.store.list_events(restriction["risk_id"]):
            data = item["data"]
            touches = False
            if item["event_type"] == ev.RISK_CONTROL_ADDED:
                touches = item["seq"] == restriction["added_seq"]
            elif item["event_type"] in (ev.RESTRICTION_RELEASED,):
                touches = data.get("restriction_id") == restriction_id
            elif item["event_type"] in (ev.EXCEPTION_GRANTED,):
                touches = data.get("restriction_id") == restriction_id
            elif item["event_type"] in (ev.EXCEPTION_REVOKED, ev.EXCEPTION_EXPIRED):
                touches = self._exception_for_restriction(data.get("exception_id"), restriction)
            elif item["event_type"] == ev.FLOW_RESTORED:
                if data.get("flow") == restriction["flow"]:
                    restored.append({"seq": item["seq"], "at": item["occurred_at"], **data})
                touches = False
            if touches:
                chain.append({"seq": item["seq"], "at": item["occurred_at"],
                              "actor": item["actor"], "type": item["event_type"], "data": data})
        rating_snapshot = None
        if risk.get("latest_rating_seq"):
            row = self.store.conn.execute(
                "SELECT * FROM event_log WHERE seq = ?", (risk["latest_rating_seq"],)
            ).fetchone()
            import json as _json
            rating_snapshot = {"seq": row["seq"], "at": row["occurred_at"], "data": _json.loads(row["data"])}
        return {
            "restriction": restriction,
            "risk": {"risk_id": risk["risk_id"], "title": risk["title"],
                     "level": risk["current_level"], "rule_version": risk["current_rule_version"],
                     "status": risk["status"], "owner": risk["owner"]},
            "decision_chain": chain,
            "rating_snapshot": rating_snapshot,
            "flow_restorations": restored,
        }

    def _exception_for_restriction(self, exception_id: Any, restriction: dict[str, Any]) -> bool:
        if exception_id is None:
            return False
        exc = self.store.get_exception(int(exception_id))
        return bool(exc and exc["action"] == restriction["action"] and exc["risk_id"] == restriction["risk_id"])

    def risk_detail(self, risk_id: str) -> dict[str, Any]:
        risk = self.store.get_risk(risk_id)
        if risk is None:
            raise ServiceError("风险不存在")
        now = self._now()
        restrictions = []
        for item in self.store.list_restrictions(risk_id):
            entry = dict(item)
            entry["active_bool"] = bool(entry["active"])
            if item["active"]:
                exc = self.store.active_exception(risk_id, item["action"], now)
                entry["current_exception"] = dict(exc) if exc else None
            restrictions.append(entry)
        return {
            "risk": risk,
            "restrictions": restrictions,
            "restored_flows": [
                {"seq": e["seq"], "at": e["occurred_at"], **e["data"]}
                for e in self.store.list_events(risk_id)
                if e["event_type"] == ev.FLOW_RESTORED
            ],
        }

    def history(self, risk_id: str) -> list[dict[str, Any]]:
        if self.store.get_risk(risk_id) is None:
            raise ServiceError("风险不存在")
        return self.store.list_events(risk_id)

    def verify_chain(self) -> dict[str, Any]:
        return {"valid": self.store.verify_chain()}

    def _require_open_risk(self, risk_id: str) -> dict[str, Any]:
        risk = self.store.get_risk(risk_id)
        if risk is None:
            raise ServiceError("风险不存在")
        if risk["status"] != ev.STATUS_OPEN:
            raise ServiceError(f"风险 {risk_id} 当前状态为 {risk['status']}，不可执行该操作")
        return risk
