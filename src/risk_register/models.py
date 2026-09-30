"""领域模型。

所有对象均可序列化为普通 dict，便于 JSON 快照持久化与接口返回。
状态取值沿用领域契约（``domain/contract.json``）中的中文命名。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


# ---------------------------------------------------------------- 枚举 ----


class RiskStatus(StrEnum):
    """风险主状态（前五项为契约状态，其余为终结态）。"""

    IDENTIFIED = "识别"
    ASSESSED = "评估"
    RESTRICTED = "限制"
    MITIGATED = "缓解"
    REVIEW = "复查"
    MERGED = "已合并"
    CLOSED = "已关闭"


CONTRACT_STATES = {"识别", "评估", "限制", "缓解", "复查"}


class RiskLevel(StrEnum):
    LOW = "低"
    MEDIUM = "中"
    HIGH = "高"
    CRITICAL = "极高"


LEVEL_ORDER = {"低": 0, "中": 1, "高": 2, "极高": 3}


class FlowType(StrEnum):
    """受限制传播的下游流程：招生、付款、里程碑。"""

    ENROLLMENT = "招生"
    PAYMENT = "付款"
    MILESTONE = "里程碑"


class RestrictionAction(StrEnum):
    """限制动作。"""

    BLOCK = "阻断"        # 完全阻断流程
    HOLD = "冻结"         # 冻结付款 / 挂起里程碑，待人工放行
    WARN = "预警"         # 不阻断，仅提示风险


class RestrictionStatus(StrEnum):
    ACTIVE = "生效中"
    LIFTED = "已解除"
    EXEMPT = "例外放行"
    SUPERSEDED = "已承袭"  # 合并后由目标风险承袭


class DecisionType(StrEnum):
    """决定链上的决定类型。"""

    CREATE = "登记"
    RATING = "等级评定"
    RESTRICTION_IMPOSE = "施加限制"
    RESTRICTION_LIFT = "解除限制"
    EXEMPT_APPROVAL = "例外批准"
    MERGE = "风险合并"
    DOWNGRADE = "降级"
    REVIEW = "定时复查"
    MITIGATION_CONFIRM = "缓解落实"
    REOPEN = "复开"
    CLOSE = "关闭"


class MitigationStatus(StrEnum):
    PLANNED = "计划中"
    IN_PROGRESS = "执行中"
    CONFIRMED = "已落实"


# ---------------------------------------------------------------- 值对象 ----


@dataclass
class RiskSource:
    """风险来源。协议、课程、企业风险分散在不同部门，故显式记录部门。"""

    category: str            # 协议 / 课程 / 企业风险 / 其他
    department: str          # 来源归口部门，如"企业合作部"
    party: str               # 外部相关方，如停止提供岗位的企业
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"category": self.category, "department": self.department,
                "party": self.party, "detail": self.detail}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RiskSource:
        return cls(d["category"], d["department"], d["party"], d.get("detail", ""))


@dataclass
class AffectedObject:
    """影响对象：项目、付款节点、里程碑、招生批次等。"""

    kind: str                # 项目 / 付款节点 / 里程碑 / 招生批次
    ref: str                 # 对象编号
    name: str = ""

    def key(self) -> str:
        return f"{self.kind}:{self.ref}"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "ref": self.ref, "name": self.name}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AffectedObject:
        return cls(d["kind"], d["ref"], d.get("name", ""))


@dataclass
class Owner:
    """风险责任人（岗位 + 所属部门 + 姓名）。"""

    name: str
    role: str
    department: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "role": self.role, "department": self.department}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Owner:
        return cls(d["name"], d["role"], d["department"])


@dataclass
class Mitigation:
    """缓解措施。"""

    id: str
    description: str
    owner: str
    due_date: str
    status: str = str(MitigationStatus.PLANNED)
    confirmed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "description": self.description, "owner": self.owner,
            "due_date": self.due_date, "status": self.status,
            "confirmed_at": self.confirmed_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Mitigation:
        return cls(d["id"], d["description"], d["owner"], d["due_date"],
                   d.get("status", str(MitigationStatus.PLANNED)),
                   d.get("confirmed_at"))


@dataclass
class RestrictionSpec:
    """规则引擎输出的限制规格：动作、传播目标与适用范围。

    scope 为影响对象键（如 ``付款节点:P2``）；为空表示对该流程整体生效。
    rule_id 记录该限制来自哪条评定规则，供解释与重评对账。
    """

    action: str
    flow_type: str
    scope: str | None = None
    reason: str = ""
    rule_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "flow_type": self.flow_type,
                "scope": self.scope, "reason": self.reason, "rule_id": self.rule_id}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RestrictionSpec:
        return cls(d["action"], d["flow_type"], d.get("scope"),
                   d.get("reason", ""), d.get("rule_id", ""))


@dataclass
class Exemption:
    """例外批准记录。scope 为空表示整条限制例外，否则仅对某流程键例外。"""

    decision_id: str
    approver: str
    reason: str
    valid_from: str
    valid_until: str
    scope: str | None = None

    def active_on(self, day: str, flow_key: str | None) -> bool:
        if not (self.valid_from <= day <= self.valid_until):
            return False
        return self.scope is None or self.scope == flow_key

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id, "approver": self.approver,
            "reason": self.reason, "valid_from": self.valid_from,
            "valid_until": self.valid_until, "scope": self.scope,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Exemption:
        return cls(d["decision_id"], d["approver"], d["reason"],
                   d["valid_from"], d["valid_until"], d.get("scope"))


@dataclass
class Restriction:
    """已生效的限制（带身份与生命周期）。"""

    id: str
    risk_id: str
    action: str
    flow_type: str
    scope: str | None
    reason: str
    status: str = str(RestrictionStatus.ACTIVE)
    rule_id: str = "MANUAL"
    imposed_at: str = ""
    imposed_by: str = ""
    lifted_at: str | None = None
    lifted_reason: str | None = None
    exemptions: list[Exemption] = field(default_factory=list)
    origin_risk_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "risk_id": self.risk_id, "action": self.action,
            "flow_type": self.flow_type, "scope": self.scope, "reason": self.reason,
            "status": self.status, "rule_id": self.rule_id,
            "imposed_at": self.imposed_at, "imposed_by": self.imposed_by,
            "lifted_at": self.lifted_at, "lifted_reason": self.lifted_reason,
            "exemptions": [e.to_dict() for e in self.exemptions],
            "origin_risk_ids": list(self.origin_risk_ids),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Restriction:
        return cls(
            d["id"], d["risk_id"], d["action"], d["flow_type"], d.get("scope"),
            d["reason"], d.get("status", str(RestrictionStatus.ACTIVE)),
            d.get("rule_id", "MANUAL"),
            d.get("imposed_at", ""), d.get("imposed_by", ""),
            d.get("lifted_at"), d.get("lifted_reason"),
            [Exemption.from_dict(e) for e in d.get("exemptions", [])],
            list(d.get("origin_risk_ids", [])),
        )


@dataclass
class Decision:
    """决定链条目：任何改变风险/限制状态的动作都不可变留痕。

    before / after 保存关键字段快照，rule_ref 记录所依据的版本化规则。
    """

    id: str
    seq: int
    type: str
    actor: str
    at: str
    reason: str = ""
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    risk_refs: list[str] = field(default_factory=list)
    restriction_refs: list[str] = field(default_factory=list)
    ruleset_version: str | None = None
    rule_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "seq": self.seq, "type": self.type, "actor": self.actor,
            "at": self.at, "reason": self.reason, "before": self.before,
            "after": self.after, "risk_refs": list(self.risk_refs),
            "restriction_refs": list(self.restriction_refs),
            "ruleset_version": self.ruleset_version, "rule_refs": list(self.rule_refs),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Decision:
        return cls(
            d["id"], d["seq"], d["type"], d["actor"], d["at"], d.get("reason", ""),
            dict(d.get("before", {})), dict(d.get("after", {})),
            list(d.get("risk_refs", [])), list(d.get("restriction_refs", [])),
            d.get("ruleset_version"), list(d.get("rule_refs", [])),
        )


@dataclass
class Rating:
    """一次等级评定的完整依据，保证评定可按规则版本追溯。"""

    level: str
    score: int
    ruleset_version: str
    matched_rules: list[str]
    restrictions: list[RestrictionSpec]
    rated_at: str
    basis: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level, "score": self.score,
            "ruleset_version": self.ruleset_version,
            "matched_rules": list(self.matched_rules),
            "restrictions": [r.to_dict() for r in self.restrictions],
            "rated_at": self.rated_at, "basis": dict(self.basis),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Rating:
        return cls(
            d["level"], d["score"], d["ruleset_version"],
            list(d.get("matched_rules", [])),
            [RestrictionSpec.from_dict(r) for r in d.get("restrictions", [])],
            d["rated_at"], dict(d.get("basis", {})),
        )


@dataclass
class Risk:
    """风险登记主实体。"""

    id: str
    title: str
    source: RiskSource
    affected: list[AffectedObject]
    owner: Owner
    description: str = ""
    status: str = str(RiskStatus.IDENTIFIED)
    rating: Rating | None = None
    # 人工降级覆盖：不改动规则评定结果，current_level 取覆盖值
    level_override: dict[str, Any] | None = None
    mitigations: list[Mitigation] = field(default_factory=list)
    restriction_ids: list[str] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    next_review_date: str | None = None
    review_interval_days: int | None = None
    review_history: list[dict[str, Any]] = field(default_factory=list)
    merged_into: str | None = None
    reopen_count: int = 0
    created_at: str = ""
    updated_at: str = ""

    @property
    def current_level(self) -> str | None:
        """当前有效等级：人工降级覆盖优先，否则取规则评定等级。"""
        if self.level_override is not None:
            return self.level_override["level"]
        return self.rating.level if self.rating else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "source": self.source.to_dict(),
            "affected": [a.to_dict() for a in self.affected],
            "owner": self.owner.to_dict(), "description": self.description,
            "status": self.status,
            "rating": self.rating.to_dict() if self.rating else None,
            "current_level": self.current_level,
            "level_override": dict(self.level_override) if self.level_override else None,
            "mitigations": [m.to_dict() for m in self.mitigations],
            "restriction_ids": list(self.restriction_ids),
            "decisions": [x.to_dict() for x in self.decisions],
            "next_review_date": self.next_review_date,
            "review_interval_days": self.review_interval_days,
            "review_history": [dict(h) for h in self.review_history],
            "merged_into": self.merged_into, "reopen_count": self.reopen_count,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Risk:
        return cls(
            id=d["id"], title=d["title"], source=RiskSource.from_dict(d["source"]),
            affected=[AffectedObject.from_dict(a) for a in d["affected"]],
            owner=Owner.from_dict(d["owner"]), description=d.get("description", ""),
            status=d.get("status", str(RiskStatus.IDENTIFIED)),
            rating=Rating.from_dict(d["rating"]) if d.get("rating") else None,
            level_override=dict(d["level_override"]) if d.get("level_override") else None,
            mitigations=[Mitigation.from_dict(m) for m in d.get("mitigations", [])],
            restriction_ids=list(d.get("restriction_ids", [])),
            decisions=[Decision.from_dict(x) for x in d.get("decisions", [])],
            next_review_date=d.get("next_review_date"),
            review_interval_days=d.get("review_interval_days"),
            review_history=[dict(h) for h in d.get("review_history", [])],
            merged_into=d.get("merged_into"), reopen_count=d.get("reopen_count", 0),
            created_at=d.get("created_at", ""), updated_at=d.get("updated_at", ""),
        )


@dataclass
class Flow:
    """下游流程实例（招生批次 / 付款节点 / 里程碑）。

    held_by 为当前挂住该流程的生效限制（含尚在例外期内的），随限制
    施加/解除事务性维护；某条限制是否被例外放行由服务层按当日判断。
    restoration_log 记录每次限制解除后恢复的流程，回答"解除后恢复了什么"。
    """

    key: str
    flow_type: str
    ref: str
    name: str
    held_by: list[str] = field(default_factory=list)
    restoration_log: list[dict[str, Any]] = field(default_factory=list)

    @property
    def running(self) -> bool:
        """没有任何挂起限制即视为运行中（例外情况以服务层解释结果为准）。"""
        return not self.held_by

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "flow_type": self.flow_type, "ref": self.ref,
            "name": self.name, "running": self.running,
            "held_by": list(self.held_by),
            "restoration_log": [dict(x) for x in self.restoration_log],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Flow:
        return cls(d["key"], d["flow_type"], d["ref"], d.get("name", ""),
                   list(d.get("held_by", [])),
                   [dict(x) for x in d.get("restoration_log", [])])
