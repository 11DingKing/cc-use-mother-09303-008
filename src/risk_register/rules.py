"""有版本的等级评定规则。

规则集（:class:`RuleSet`）是不可变快照，带语义化版本号；发布新版只新增
快照，永不覆盖旧版。风险评定结果（``Rating``）持久化所引用的
``ruleset_version``，因此历史等级永远可以按当时规则复算解释。

规则分两类：

- 条件因子（factor）：命中即加分，可选附加限制规格；
- 等级门槛（threshold）：按总分确定等级，每个门槛可携带默认限制。

引擎是纯函数，不接触时钟与仓储，保证同一输入同一输出。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .models import RestrictionAction, RestrictionSpec, Risk, RiskLevel


@dataclass(frozen=True)
class Rule:
    id: str
    description: str
    score: int = 0
    # matcher 接收引擎构造的因子上下文，返回 bool
    matcher: Callable[[dict[str, Any]], bool] = field(compare=False, default=lambda _: False)
    restrictions: tuple[RestrictionSpec, ...] = ()

    def to_meta(self) -> dict[str, Any]:
        """规则元数据（不含函数），用于接口展示规则版本内容。"""
        return {
            "id": self.id, "description": self.description, "score": self.score,
            "restrictions": [r.to_dict() for r in self.restrictions],
        }


@dataclass(frozen=True)
class RuleSet:
    version: str
    published_at: str
    description: str
    rules: tuple[Rule, ...]
    # 等级 -> 最低分；从高到低匹配
    thresholds: tuple[tuple[str, int], ...]
    # 等级触发的强制动作（定级后注入），键为等级
    level_actions: dict[str, tuple[RestrictionSpec, ...]] = field(default_factory=dict)
    default_level: str = str(RiskLevel.LOW)

    def rule(self, rule_id: str) -> Rule:
        for r in self.rules:
            if r.id == rule_id:
                return r
        raise KeyError(rule_id)

    def spec(self) -> dict[str, Any]:
        return {
            "version": self.version, "published_at": self.published_at,
            "description": self.description,
            "rules": [r.to_meta() for r in self.rules],
            "thresholds": [{"level": lv, "min_score": sc} for lv, sc in self.thresholds],
            "level_actions": {
                lv: [r.to_dict() for r in specs]
                for lv, specs in self.level_actions.items()
            },
            "default_level": self.default_level,
        }


# ------------------------------------------------------------ 因子提取 ----


def build_context(risk: Risk) -> dict[str, Any]:
    """从风险实体抽取规则匹配上下文。"""
    affected_kinds = {a.kind for a in risk.affected}
    return {
        "source_category": risk.source.category,
        "source_department": risk.source.department,
        "affected_kinds": affected_kinds,
        "affected_flows": _kinds_to_flows(affected_kinds),
        "mitigation_confirmed": all(
            m.status == "已落实" for m in risk.mitigations
        ) if risk.mitigations else False,
        "has_mitigation": bool(risk.mitigations),
        "description": risk.description,
        "title": risk.title,
    }


def _kinds_to_flows(kinds: set[str]) -> set[str]:
    mapping = {
        "招生批次": "招生",
        "付款节点": "付款",
        "里程碑": "里程碑",
    }
    return {mapping[k] for k in kinds if k in mapping}


# ---------------------------------------------------------------- 引擎 ----


class RatingEngine:
    def __init__(self, ruleset: RuleSet) -> None:
        self.ruleset = ruleset

    def evaluate(self, risk: Risk) -> dict[str, Any]:
        """纯函数评定：返回等级、总分、命中规则与限制规格。"""
        ctx = build_context(risk)
        matched: list[Rule] = []
        score = 0
        restrictions: dict[tuple[str, str, str | None], RestrictionSpec] = {}
        for rule in self.ruleset.rules:
            if rule.matcher(ctx):
                matched.append(rule)
                score += rule.score
                for spec in rule.restrictions:
                    # 同一规则对同一 scope 可能产出多条不同动作限制，全部保留
                    restrictions.setdefault(
                        (spec.flow_type, spec.action, spec.scope), spec
                    )
        level = self.ruleset.default_level
        for lv, min_score in self.ruleset.thresholds:  # 已按从高到低排序
            if score >= min_score:
                level = lv
                break
        # 定级后注入该等级的强制动作（如高风险阻断付款）
        for spec in self.ruleset.level_actions.get(level, ()):
            restrictions.setdefault(
                (spec.flow_type, spec.action, spec.scope), spec
            )
        return {
            "level": level,
            "score": score,
            "matched_rules": [r.id for r in matched],
            "matched_detail": [
                {"id": r.id, "description": r.description, "score": r.score}
                for r in matched
            ],
            "restrictions": list(restrictions.values()),
            "basis": {"context": _jsonable_context(ctx), "thresholds": list(self.ruleset.thresholds)},
        }


def _jsonable_context(ctx: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in ctx.items():
        if isinstance(v, set):
            out[k] = sorted(v)
        else:
            out[k] = v
    return out


# ------------------------------------------------------------ 规则版本 ----


def _r(action: str, flow: str, scope: str | None = None, reason: str = "", rule_id: str = "") -> RestrictionSpec:
    return RestrictionSpec(action=action, flow_type=flow, scope=scope, reason=reason, rule_id=rule_id)


def _v1() -> RuleSet:
    rules = (
        Rule(
            "R1-SOURCE-ENTERPRISE", "外部企业单方停止履行合作义务（如停止提供实习岗位）",
            score=2,
            matcher=lambda c: c["source_category"] == "企业风险",
            restrictions=(
                _r(str(RestrictionAction.WARN), "招生", reason="企业来源风险，招生环节预警", rule_id="R1-SOURCE-ENTERPRISE"),
            ),
        ),
        Rule(
            "R2-PAYMENT-AT-RISK", "风险波及付款节点且款项仍按原计划推进",
            score=2,
            matcher=lambda c: "付款" in c["affected_flows"],
            restrictions=(
                _r(str(RestrictionAction.HOLD), "付款", reason="付款节点受风险影响，冻结后续付款待人工放行",
                   rule_id="R2-PAYMENT-AT-RISK"),
            ),
        ),
        Rule(
            "R3-ENROLLMENT-AT-RISK", "风险波及招生批次",
            score=1,
            matcher=lambda c: "招生" in c["affected_flows"],
            restrictions=(
                _r(str(RestrictionAction.WARN), "招生", reason="招生批次受风险影响，发布预警",
                   rule_id="R3-ENROLLMENT-AT-RISK"),
            ),
        ),
        Rule(
            "R4-MILESTONE-AT-RISK", "风险波及项目里程碑",
            score=1,
            matcher=lambda c: "里程碑" in c["affected_flows"],
            restrictions=(
                _r(str(RestrictionAction.HOLD), "里程碑", reason="里程碑受风险影响，暂挂验收",
                   rule_id="R4-MILESTONE-AT-RISK"),
            ),
        ),
        Rule(
            "R5-CROSS-DEPARTMENT", "协议、课程、企业风险分属不同部门，信息未打通",
            score=1,
            matcher=lambda c: c["source_category"] in {"协议", "课程", "企业风险"},
        ),
    )
    thresholds = (
        (str(RiskLevel.CRITICAL), 6),
        (str(RiskLevel.HIGH), 4),
        (str(RiskLevel.MEDIUM), 2),
        (str(RiskLevel.LOW), 0),
    )
    return RuleSet(
        version="1.0.0",
        published_at="2026-09-01",
        description="首版评定规则：企业停岗 + 付款/招生/里程碑影响",
        rules=rules,
        thresholds=thresholds,
    )


def _v1_1() -> RuleSet:
    """1.1：高等级强制阻断付款；已落实缓解措施可减分。

    历史风险仍按其评定时的版本（1.0.0）解释；新评/复评默认使用最新版。
    """
    rules = (
        Rule(
            "R1-SOURCE-ENTERPRISE", "外部企业单方停止履行合作义务（如停止提供实习岗位）",
            score=2,
            matcher=lambda c: c["source_category"] == "企业风险",
            restrictions=(
                _r(str(RestrictionAction.WARN), "招生", reason="企业来源风险，招生环节预警", rule_id="R1-SOURCE-ENTERPRISE"),
            ),
        ),
        Rule(
            "R2-PAYMENT-AT-RISK", "风险波及付款节点且款项仍按原计划推进",
            score=2,
            matcher=lambda c: "付款" in c["affected_flows"],
            restrictions=(
                _r(str(RestrictionAction.HOLD), "付款", reason="付款节点受风险影响，冻结后续付款待人工放行",
                   rule_id="R2-PAYMENT-AT-RISK"),
            ),
        ),
        Rule(
            "R3-ENROLLMENT-AT-RISK", "风险波及招生批次",
            score=1,
            matcher=lambda c: "招生" in c["affected_flows"],
            restrictions=(
                _r(str(RestrictionAction.WARN), "招生", reason="招生批次受风险影响，发布预警",
                   rule_id="R3-ENROLLMENT-AT-RISK"),
            ),
        ),
        Rule(
            "R4-MILESTONE-AT-RISK", "风险波及项目里程碑",
            score=1,
            matcher=lambda c: "里程碑" in c["affected_flows"],
            restrictions=(
                _r(str(RestrictionAction.HOLD), "里程碑", reason="里程碑受风险影响，暂挂验收",
                   rule_id="R4-MILESTONE-AT-RISK"),
            ),
        ),
        Rule(
            "R5-CROSS-DEPARTMENT", "协议、课程、企业风险分属不同部门，信息未打通",
            score=1,
            matcher=lambda c: c["source_category"] in {"协议", "课程", "企业风险"},
        ),
        Rule(
            "R6-MITIGATION-CONFIRMED", "缓解措施已全部落实并复核确认",
            score=-2,
            matcher=lambda c: c["has_mitigation"] and c["mitigation_confirmed"],
        ),
        Rule(
            "R7-HIGH-BLOCK-PAYMENT", "高/极高等级强制阻断付款（等级动作，不计分）",
            score=0,
            # 等级动作不参与因子命中；实际限制由 level_actions 在定级后注入，
            # 规则保留在此仅为提供版本化的条文描述供解释。
            matcher=lambda c: False,
        ),
    )
    thresholds = (
        (str(RiskLevel.CRITICAL), 6),
        (str(RiskLevel.HIGH), 4),
        (str(RiskLevel.MEDIUM), 2),
        (str(RiskLevel.LOW), 0),
    )
    high_block = (
        _r(str(RestrictionAction.BLOCK), "付款",
           reason="高/极高风险强制阻断付款（规则 1.1 等级动作）",
           rule_id="R7-HIGH-BLOCK-PAYMENT"),
    )
    return RuleSet(
        version="1.1.0",
        published_at="2026-09-20",
        description="高风险强制阻断付款；已落实缓解措施减分",
        rules=rules,
        thresholds=thresholds,
        level_actions={
            str(RiskLevel.HIGH): high_block,
            str(RiskLevel.CRITICAL): high_block,
        },
    )


class RuleRegistry:
    """规则集版本注册表：保存全部历史快照，只能追加。"""

    def __init__(self, rulesets: list[RuleSet]) -> None:
        if not rulesets:
            raise ValueError("至少需要一个规则版本")
        self._sets: dict[str, RuleSet] = {}
        for rs in rulesets:
            if rs.version in self._sets:
                raise ValueError(f"规则版本重复：{rs.version}")
            self._sets[rs.version] = rs
        self._versions = sorted(self._sets, key=_version_key)
        self._latest = self._sets[self._versions[-1]]

    @property
    def latest(self) -> RuleSet:
        return self._latest

    def get(self, version: str) -> RuleSet:
        try:
            return self._sets[version]
        except KeyError:
            raise KeyError(f"未知规则版本：{version}（可用：{'、'.join(self._versions)}）") from None

    def versions(self) -> list[str]:
        return list(self._versions)

    def engine(self, version: str | None = None) -> RatingEngine:
        return RatingEngine(self.get(version) if version else self.latest)


def _version_key(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def default_registry() -> RuleRegistry:
    return RuleRegistry([_v1(), _v1_1()])


# 向后兼容的便捷导出
def default_ruleset() -> RuleSet:
    return default_registry().latest
