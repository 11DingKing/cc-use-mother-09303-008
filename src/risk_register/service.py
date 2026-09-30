"""应用服务层：风险登记全部业务用例。

所有改变状态的操作都满足：

1. 在同一把仓储锁内完成，限制施加/解除与流程挂起/恢复是同一事务；
2. 必定追加一条不可变 :class:`Decision`，形成决定链；
3. 解除限制时把恢复的流程写入 ``restoration_log``，可回答"恢复了什么"。
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from .clock import Clock, SystemClock
from .models import (
    LEVEL_ORDER,
    AffectedObject,
    Decision,
    DecisionType,
    Flow,
    FlowType,
    Mitigation,
    MitigationStatus,
    Owner,
    Restriction,
    RestrictionAction,
    RestrictionSpec,
    RestrictionStatus,
    Risk,
    RiskSource,
    RiskStatus,
    Rating,
    Exemption,
)
from .rules import RuleRegistry, default_registry
from .store import Repository

TERMINAL_STATUSES = {str(RiskStatus.MERGED), str(RiskStatus.CLOSED)}
HOLDING_ACTIONS = {str(RestrictionAction.BLOCK), str(RestrictionAction.HOLD)}


class RiskService:
    def __init__(
        self,
        repo: Repository | None = None,
        rules: RuleRegistry | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.repo = repo or Repository()
        self.rules = rules or default_registry()
        self.clock = clock or SystemClock()

    # ============================================================ 登记 ====

    def create_risk(
        self,
        *,
        actor: str,
        title: str,
        source: RiskSource,
        affected: list[AffectedObject],
        owner: Owner,
        description: str = "",
        review_interval_days: int = 30,
    ) -> Risk:
        if not affected:
            raise ValueError("至少登记一个影响对象")
        valid_kinds = {"招生批次", "付款节点", "里程碑"}
        bad = {a.kind for a in affected if a.kind not in valid_kinds}
        if bad:
            raise ValueError(f"影响对象类型不能传播限制：{'、'.join(sorted(bad))}")
        today = self.clock.today().isoformat()
        with self.repo.lock:
            rid = self.repo.next_id("RSK")
            risk = Risk(
                id=rid, title=title, source=source, affected=affected, owner=owner,
                description=description, status=str(RiskStatus.IDENTIFIED),
                review_interval_days=review_interval_days,
                next_review_date=(self.clock.today() + timedelta(days=review_interval_days)).isoformat(),
                created_at=today, updated_at=today,
            )
            # 影响对象同步注册为可传播的下游流程
            for obj in affected:
                self._ensure_flow(obj)
            self.repo.save_risk(risk)
            self._decide(risk, DecisionType.CREATE, actor, today,
                         reason="登记风险来源与影响对象",
                         after={"status": risk.status, "title": title})
            return risk

    def _ensure_flow(self, obj: AffectedObject) -> Flow:
        mapping = {"招生批次": str(FlowType.ENROLLMENT), "付款节点": str(FlowType.PAYMENT),
                   "里程碑": str(FlowType.MILESTONE)}
        flow_type = mapping.get(obj.kind)
        if flow_type is None:
            raise ValueError(f"影响对象类型不能传播限制：{obj.kind}")
        key = obj.key()
        try:
            return self.repo.get_flow(key)
        except KeyError:
            flow = Flow(key=key, flow_type=flow_type, ref=obj.ref, name=obj.name)
            return self.repo.upsert_flow(flow)

    # ========================================================== 评定 ====

    def rate_risk(self, risk_id: str, *, actor: str, ruleset_version: str | None = None,
                  reason: str = "") -> Risk:
        """依据指定版本规则评定等级，并把限制规格对账式地传播到流程。"""
        with self.repo.lock:
            risk = self.repo.get_risk(risk_id)
            self._require_open(risk)
            version = ruleset_version or self.rules.latest.version
            engine = self.rules.engine(version)
            result = engine.evaluate(risk)
            today = self.clock.today().isoformat()
            before = {"level": risk.rating.level if risk.rating else None,
                      "ruleset_version": risk.rating.ruleset_version if risk.rating else None,
                      "status": risk.status}

            rating = Rating(
                level=result["level"], score=result["score"],
                ruleset_version=version, matched_rules=result["matched_rules"],
                restrictions=result["restrictions"], rated_at=today,
                basis=result["basis"],
            )
            risk.rating = rating
            # 新一轮规则评定取代既往人工降级覆盖
            risk.level_override = None
            self._reconcile_restrictions(risk, result["restrictions"], actor, today)
            risk.status = (str(RiskStatus.RESTRICTED) if risk.restriction_ids
                           else str(RiskStatus.ASSESSED))
            risk.updated_at = today
            self.repo.save_risk(risk)
            self._decide(
                risk, DecisionType.RATING, actor, today,
                reason=reason or f"按规则 {version} 评定",
                before=before,
                after={"level": rating.level, "score": rating.score,
                       "ruleset_version": version, "matched_rules": rating.matched_rules,
                       "status": risk.status},
                rule_refs=rating.matched_rules,
                restriction_refs=list(risk.restriction_ids),
            )
            return risk

    def _reconcile_restrictions(
        self, risk: Risk, specs: list[RestrictionSpec], actor: str, today: str,
    ) -> None:
        """把引擎输出与现存生效限制对账：新增缺失、保持命中、解除多余。"""
        # 规则级 scope 为空时展开到该风险在该流程上的全部具体流程键
        expanded: list[RestrictionSpec] = []
        for spec in specs:
            targets = self._target_flow_keys(risk, spec)
            for key in targets:
                expanded.append(RestrictionSpec(
                    action=spec.action, flow_type=spec.flow_type, scope=key,
                    reason=spec.reason, rule_id=spec.rule_id,
                ))

        active = [self.repo.get_restriction(rid) for rid in risk.restriction_ids
                  if self.repo.get_restriction(rid).status == str(RestrictionStatus.ACTIVE)]
        wanted: dict[tuple[str, str], RestrictionSpec] = {
            (s.action, s.scope): s for s in expanded
        }
        keep: list[str] = []
        for r in active:
            key = (r.action, r.scope)
            if key in wanted:
                spec = wanted.pop(key)
                r.rule_id = spec.rule_id or r.rule_id
                r.reason = spec.reason or r.reason
                self.repo.save_restriction(r)
                keep.append(r.id)
            else:
                self._lift(risk, r, actor, today, "复评后规则不再要求该限制", decide=False)
        for spec in wanted.values():
            self._impose(risk, spec, actor, today, decide=False)
        # _lift 已移除、_impose 已追加，restriction_ids 即当前生效集合

    def _target_flow_keys(self, risk: Risk, spec: RestrictionSpec) -> list[str]:
        if spec.scope:
            return [spec.scope]
        flow_kind = {str(FlowType.ENROLLMENT): "招生批次", str(FlowType.PAYMENT): "付款节点",
                     str(FlowType.MILESTONE): "里程碑"}[spec.flow_type]
        return [obj.key() for obj in risk.affected if obj.kind == flow_kind]

    def _impose(self, risk: Risk, spec: RestrictionSpec, actor: str, today: str, *,
                decide: bool = True, origin: list[str] | None = None) -> Restriction:
        rid = self.repo.next_id("RST")
        restriction = Restriction(
            id=rid, risk_id=risk.id, action=spec.action, flow_type=spec.flow_type,
            scope=spec.scope, reason=spec.reason, rule_id=spec.rule_id or "MANUAL",
            imposed_at=today, imposed_by=actor,
            origin_risk_ids=origin or [risk.id],
        )
        self.repo.save_restriction(restriction)
        risk.restriction_ids.append(rid)
        risk.updated_at = today
        if spec.action in HOLDING_ACTIONS and spec.scope:
            flow = self.repo.get_flow(spec.scope)
            if rid not in flow.held_by:
                flow.held_by.append(rid)
                self.repo.upsert_flow(flow)
        self.repo.save_risk(risk)
        if decide:
            self._decide(risk, DecisionType.RESTRICTION_IMPOSE, actor, today,
                         reason=spec.reason,
                         after={"restriction_id": rid, "action": spec.action,
                                "flow": spec.flow_type, "scope": spec.scope},
                         restriction_refs=[rid], rule_refs=[spec.rule_id] if spec.rule_id else [])
        return restriction

    def _lift(self, risk: Risk, restriction: Restriction, actor: str, today: str,
              reason: str, *, decide: bool = True) -> Restriction:
        if restriction.status != str(RestrictionStatus.ACTIVE):
            return restriction
        restriction.status = str(RestrictionStatus.LIFTED)
        restriction.lifted_at = today
        restriction.lifted_reason = reason
        self.repo.save_restriction(restriction)
        risk.updated_at = today
        # 从事务性挂起表移除，并记录"恢复了哪个流程"
        if restriction.scope:
            flow = self.repo.get_flow(restriction.scope)
            if restriction.id in flow.held_by:
                flow.held_by.remove(restriction.id)
                flow.restoration_log.append({
                    "at": today, "restriction_id": restriction.id,
                    "risk_id": risk.id, "action": restriction.action,
                    "reason": reason, "actor": actor,
                })
                self.repo.upsert_flow(flow)
        if restriction.id in risk.restriction_ids:
            risk.restriction_ids.remove(restriction.id)
        self.repo.save_risk(risk)
        if decide:
            self._decide(risk, DecisionType.RESTRICTION_LIFT, actor, today, reason=reason,
                         before={"restriction_id": restriction.id, "status": str(RestrictionStatus.ACTIVE)},
                         after={"restriction_id": restriction.id, "status": restriction.status},
                         restriction_refs=[restriction.id])
        return restriction

    # ====================================================== 缓解措施 ====

    def add_mitigation(self, risk_id: str, *, actor: str, description: str,
                       owner: str, due_date: str) -> Mitigation:
        with self.repo.lock:
            risk = self.repo.get_risk(risk_id)
            self._require_open(risk)
            m = Mitigation(id=self.repo.next_id("MIT"), description=description,
                           owner=owner, due_date=due_date)
            risk.mitigations.append(m)
            risk.updated_at = self.clock.today().isoformat()
            self.repo.save_risk(risk)
            return m

    def confirm_mitigation(self, risk_id: str, mitigation_id: str, *, actor: str,
                           rerate: bool = True) -> Risk:
        """落实缓解措施；默认立即复评（可能触发降级与限制解除）。"""
        with self.repo.lock:
            risk = self.repo.get_risk(risk_id)
            self._require_open(risk)
            today = self.clock.today().isoformat()
            target = next((m for m in risk.mitigations if m.id == mitigation_id), None)
            if target is None:
                raise KeyError(f"缓解措施不存在：{mitigation_id}")
            before = target.status
            target.status = str(MitigationStatus.CONFIRMED)
            target.confirmed_at = today
            risk.updated_at = today
            self.repo.save_risk(risk)
            self._decide(risk, DecisionType.MITIGATION_CONFIRM, actor, today,
                         reason=f"缓解措施 {mitigation_id} 已落实",
                         before={"mitigation_status": before},
                         after={"mitigation_status": target.status})
            if rerate:
                return self.rate_risk(risk_id, actor=actor, reason="缓解措施落实后复评")
            return risk

    # ====================================================== 例外批准 ====

    def approve_exemption(self, restriction_id: str, *, actor: str, approver: str,
                          reason: str, valid_until: str, scope: str | None = None) -> Exemption:
        """对生效限制批准限期例外；到期后限制自动重新生效，无需新决定。"""
        with self.repo.lock:
            r = self.repo.get_restriction(restriction_id)
            if r.status != str(RestrictionStatus.ACTIVE):
                raise ValueError("只能对生效中的限制批准例外")
            today = self.clock.today().isoformat()
            if valid_until < today:
                raise ValueError("例外到期日不能早于今天")
            risk = self.repo.get_risk(r.risk_id)
            decision = self._decide(
                risk, DecisionType.EXEMPT_APPROVAL, actor, today,
                reason=f"例外批准：{reason}（批准人 {approver}，有效期至 {valid_until}）",
                after={"restriction_id": r.id, "valid_until": valid_until,
                       "scope": scope if scope is not None else r.scope},
                restriction_refs=[r.id],
            )
            ex = Exemption(
                decision_id=decision.id, approver=approver, reason=reason,
                valid_from=today, valid_until=valid_until,
                scope=scope if scope is not None else r.scope,
            )
            r.exemptions.append(ex)
            self.repo.save_restriction(r)
            return ex

    # ======================================================== 合并 ====

    def merge_risks(self, source_ids: list[str], target_id: str, *, actor: str,
                    reason: str) -> Risk:
        """把多个风险并入目标风险。

        源风险置"已合并"；其生效限制由目标承袭（旧限制置"已承袭"，按目标
        名义去重后重新施加），origin_risk_ids 保留完整来源链。
        """
        if target_id in source_ids:
            raise ValueError("目标风险不能同时是被合并的源风险")
        with self.repo.lock:
            today = self.clock.today().isoformat()
            target = self.repo.get_risk(target_id)
            self._require_open(target)
            sources = [self.repo.get_risk(sid) for sid in dict.fromkeys(source_ids)]
            for s in sources:
                self._require_open(s)

            transferred: list[tuple[Restriction, str]] = []
            for s in sources:
                for rid in list(s.restriction_ids):
                    r = self.repo.get_restriction(rid)
                    if r.status != str(RestrictionStatus.ACTIVE):
                        continue
                    r.status = str(RestrictionStatus.SUPERSEDED)
                    self.repo.save_restriction(r)
                    if r.scope:
                        flow = self.repo.get_flow(r.scope)
                        if rid in flow.held_by:
                            flow.held_by.remove(rid)
                            self.repo.upsert_flow(flow)
                    s.restriction_ids.remove(rid)
                    transferred.append((r, s.id))
                s.status = str(RiskStatus.MERGED)
                s.merged_into = target.id
                s.updated_at = today
                self.repo.save_risk(s)

            existing = {(self.repo.get_restriction(rid).action,
                         self.repo.get_restriction(rid).scope)
                        for rid in target.restriction_ids
                        if self.repo.get_restriction(rid).status == str(RestrictionStatus.ACTIVE)}
            new_refs: list[str] = []
            for r, from_source in transferred:
                if (r.action, r.scope) in existing:
                    continue
                spec = RestrictionSpec(action=r.action, flow_type=r.flow_type,
                                       scope=r.scope, reason=r.reason, rule_id=r.rule_id)
                nr = self._impose(
                    target, spec, actor, today, decide=False,
                    origin=list(dict.fromkeys(r.origin_risk_ids + [from_source])),
                )
                existing.add((r.action, r.scope))
                new_refs.append(nr.id)

            target.updated_at = today
            self.repo.save_risk(target)
            self._decide(
                target, DecisionType.MERGE, actor, today, reason=reason,
                after={"merged_from": [s.id for s in sources],
                       "transferred_restrictions": new_refs},
                risk_refs=[s.id for s in sources],
                restriction_refs=new_refs,
            )
            for s in sources:
                self._decide(s, DecisionType.MERGE, actor, today,
                             reason=f"并入 {target.id}：{reason}",
                             after={"merged_into": target.id}, risk_refs=[target.id])
            return target

    # ======================================================== 降级 ====

    def downgrade_risk(self, risk_id: str, new_level: str, *, actor: str,
                       reason: str, lift_restrictions: bool = True) -> Risk:
        """人工降级（如审批认定影响降低）；可选同步解除限制并恢复流程。"""
        with self.repo.lock:
            risk = self.repo.get_risk(risk_id)
            self._require_open(risk)
            if risk.rating is None:
                raise ValueError("尚未评定的风险不能降级")
            if new_level not in LEVEL_ORDER:
                raise ValueError(f"未知等级：{new_level}")
            if LEVEL_ORDER[new_level] >= LEVEL_ORDER[risk.current_level]:
                raise ValueError("降级目标等级必须低于当前等级")
            today = self.clock.today().isoformat()
            before_level = risk.current_level
            risk.level_override = {
                "level": new_level, "actor": actor, "at": today, "reason": reason,
            }
            risk.updated_at = today
            lifted: list[str] = []
            if lift_restrictions:
                for rid in list(risk.restriction_ids):
                    r = self.repo.get_restriction(rid)
                    if r.status == str(RestrictionStatus.ACTIVE):
                        self._lift(risk, r, actor, today, f"风险降级为{new_level}：{reason}", decide=False)
                        lifted.append(rid)
            risk.status = str(RiskStatus.MITIGATED) if not risk.restriction_ids else risk.status
            self.repo.save_risk(risk)
            self._decide(risk, DecisionType.DOWNGRADE, actor, today, reason=reason,
                         before={"level": before_level, "rated_level": risk.rating.level},
                         after={"level": new_level, "rated_level": risk.rating.level},
                         restriction_refs=lifted)
            return risk

    # ======================================================== 复开 ====

    def reopen_risk(self, risk_id: str, *, actor: str, reason: str) -> Risk:
        """复开已关闭/已缓解风险，保留历史决定链，重新进入评定。"""
        with self.repo.lock:
            risk = self.repo.get_risk(risk_id)
            if risk.merged_into:
                raise ValueError("已合并风险不能直接复开，请复开其承袭风险")
            today = self.clock.today().isoformat()
            before = {"status": risk.status, "level": risk.current_level,
                      "override": risk.level_override}
            risk.status = str(RiskStatus.IDENTIFIED)
            # 复开是新一轮处置：人工降级覆盖失效，回到最近规则评定等级待重评
            risk.level_override = None
            risk.reopen_count += 1
            risk.updated_at = today
            if risk.review_interval_days:
                risk.next_review_date = (
                    self.clock.today() + timedelta(days=risk.review_interval_days)
                ).isoformat()
            self.repo.save_risk(risk)
            self._decide(risk, DecisionType.REOPEN, actor, today, reason=reason,
                         before=before, after={"status": risk.status})
            return risk

    def close_risk(self, risk_id: str, *, actor: str, reason: str) -> Risk:
        with self.repo.lock:
            risk = self.repo.get_risk(risk_id)
            self._require_open(risk)
            today = self.clock.today().isoformat()
            for rid in list(risk.restriction_ids):
                r = self.repo.get_restriction(rid)
                if r.status == str(RestrictionStatus.ACTIVE):
                    self._lift(risk, r, actor, today, f"风险关闭：{reason}", decide=False)
            before = risk.status
            risk.status = str(RiskStatus.CLOSED)
            risk.updated_at = today
            self.repo.save_risk(risk)
            self._decide(risk, DecisionType.CLOSE, actor, today, reason=reason,
                         before={"status": before}, after={"status": risk.status})
            return risk

    # ====================================================== 定时复查 ====

    def due_reviews(self, today: str | None = None) -> list[Risk]:
        day = today or self.clock.today().isoformat()
        with self.repo.lock:
            return [
                r for r in self.repo.list_risks()
                if r.status not in TERMINAL_STATUSES
                and r.next_review_date and r.next_review_date <= day
            ]

    def run_due_reviews(self, *, actor: str = "系统定时复查",
                        ruleset_version: str | None = None) -> list[dict[str, Any]]:
        """对所有到期风险执行复评并顺延复查日。时钟可控，故可确定性测试。"""
        results: list[dict[str, Any]] = []
        with self.repo.lock:
            due = self.due_reviews()
            for risk in due:
                before = {"level": risk.current_level,
                          "status": risk.status,
                          "next_review_date": risk.next_review_date}
                today = self.clock.today().isoformat()
                self.rate_risk(
                    risk.id, actor=actor,
                    ruleset_version=ruleset_version,
                    reason="定时到期复评",
                )
                risk = self.repo.get_risk(risk.id)
                if risk.review_interval_days:
                    risk.next_review_date = (
                        self.clock.today() + timedelta(days=risk.review_interval_days)
                    ).isoformat()
                risk.review_history.append({
                    "at": today, "before": before,
                    "after": {"level": risk.current_level,
                              "rated_level": risk.rating.level if risk.rating else None,
                              "status": risk.status},
                })
                self.repo.save_risk(risk)
                results.append({"risk_id": risk.id, "before": before,
                                "after": {"level": risk.current_level,
                                          "rated_level": risk.rating.level,
                                          "status": risk.status},
                                "next_review_date": risk.next_review_date})
        return results

    # ================================================== 查询与可解释性 ====

    def flow_status(self, flow_key: str) -> dict[str, Any]:
        """解释某流程当前是否被限制、为何生效、是否被例外放行。"""
        with self.repo.lock:
            flow = self.repo.get_flow(flow_key)
            today = self.clock.today().isoformat()
            blockers: list[dict[str, Any]] = []
            exempted: list[dict[str, Any]] = []
            for rid in flow.held_by:
                r = self.repo.get_restriction(rid)
                if r.status != str(RestrictionStatus.ACTIVE):
                    continue
                active_ex = next((e for e in r.exemptions if e.active_on(today, flow.key)), None)
                entry = self._restriction_explanation(r)
                if active_ex:
                    entry["exemption"] = active_ex.to_dict()
                    exempted.append(entry)
                else:
                    blockers.append(entry)
            warnings = []
            for r in self.repo.list_restrictions(status=str(RestrictionStatus.ACTIVE)):
                if r.action == str(RestrictionAction.WARN) and r.scope == flow.key:
                    warnings.append(self._restriction_explanation(r))
            return {
                "flow": flow.to_dict(),
                "today": today,
                "running": not blockers,
                "blockers": blockers,
                "exempted_blockers": exempted,
                "warnings": warnings,
                "restoration_log": flow.restoration_log,
            }

    def _restriction_explanation(self, r: Restriction) -> dict[str, Any]:
        risk = self.repo.get_risk(r.risk_id)
        rule_desc = ""
        try:
            version = risk.rating.ruleset_version if risk.rating else self.rules.latest.version
            rule_desc = self.rules.get(version).rule(r.rule_id).description
        except KeyError:
            rule_desc = "人工施加或规则版本不可考"
        return {
            "restriction_id": r.id,
            "action": r.action,
            "flow_type": r.flow_type,
            "scope": r.scope,
            "reason": r.reason,
            "rule_id": r.rule_id,
            "rule_description": rule_desc,
            "ruleset_version": risk.rating.ruleset_version if risk.rating else None,
            "risk_id": r.risk_id,
            "risk_title": risk.title,
            "imposed_at": r.imposed_at,
            "imposed_by": r.imposed_by,
            "origin_risk_ids": list(r.origin_risk_ids),
            "status": r.status,
        }

    def explain_restriction(self, restriction_id: str) -> dict[str, Any]:
        with self.repo.lock:
            r = self.repo.get_restriction(restriction_id)
            out = self._restriction_explanation(r)
            out["exemptions"] = [e.to_dict() for e in r.exemptions]
            out["lifted_at"] = r.lifted_at
            out["lifted_reason"] = r.lifted_reason
            risk = self.repo.get_risk(r.risk_id)
            out["risk_decision_chain"] = [d.to_dict() for d in risk.decisions]
            return out

    def decision_chain(self, risk_id: str) -> list[dict[str, Any]]:
        with self.repo.lock:
            risk = self.repo.get_risk(risk_id)
            chain = [d.to_dict() for d in sorted(risk.decisions, key=lambda d: d.seq)]
            if risk.merged_into:
                chain.append({"note": f"该风险已并入 {risk.merged_into}，后续决定见目标风险链"})
            return chain

    # ======================================================== 辅助 ====

    def _decide(self, risk: Risk, dtype: DecisionType, actor: str, at: str, *,
                reason: str = "", before: dict[str, Any] | None = None,
                after: dict[str, Any] | None = None,
                risk_refs: list[str] | None = None, restriction_refs: list[str] | None = None,
                rule_refs: list[str] | None = None) -> Decision:
        d = Decision(
            id=self.repo.next_id("DEC"), seq=self.repo.next_seq(), type=str(dtype),
            actor=actor, at=at, reason=reason, before=before or {}, after=after or {},
            risk_refs=risk_refs or [], restriction_refs=restriction_refs or [],
            ruleset_version=risk.rating.ruleset_version if risk.rating else None,
            rule_refs=rule_refs or [],
        )
        risk.decisions.append(d)
        self.repo.save_risk(risk)
        return d

    @staticmethod
    def _require_open(risk: Risk) -> None:
        if risk.status in TERMINAL_STATUSES:
            raise ValueError(f"风险 {risk.id} 已处于终结态（{risk.status}），不能操作")
