"""有版本的评级规则。

规则集（``RuleSet``）是不可变快照：每个版本有明确的生效时间，
评级时快照命中的规则编号、等级与要求动作，事后可追溯“当时依据哪版规则”。

规则是纯声明式条件，不执行任何外部表达式，算子仅支持
``gte / lte / gt / lt / eq / ne / in_``，取值来自评级时提交的事实。
同一事实可由多个规则命中，取命中规则中的最高等级。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

LEVEL_LOW = "低"
LEVEL_MEDIUM = "中"
LEVEL_HIGH = "高"
LEVEL_CRITICAL = "严重"
LEVEL_RANK = {LEVEL_LOW: 1, LEVEL_MEDIUM: 2, LEVEL_HIGH: 3, LEVEL_CRITICAL: 4}

_OPS = {
    "gte": lambda a, b: a is not None and a >= b,
    "lte": lambda a, b: a is not None and a <= b,
    "gt": lambda a, b: a is not None and a > b,
    "lt": lambda a, b: a is not None and a < b,
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "in": lambda a, b: a in b,
}


@dataclass(frozen=True)
class RuleSet:
    """一版不可变规则集。"""

    version: int
    published_at: datetime
    effective_from: datetime
    spec: dict[str, Any]
    published_by: str = "项目秘书处"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "published_at": self.published_at.isoformat(),
            "effective_from": self.effective_from.isoformat(),
            "published_by": self.published_by,
            "note": self.note,
            "spec": self.spec,
        }


def validate_spec(spec: dict[str, Any]) -> None:
    """校验规则集声明结构。"""
    rules = spec.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError("规则集必须包含非空 rules 列表")
    seen: set[str] = set()
    for rule in rules:
        rid = rule.get("id")
        if not rid or rid in seen:
            raise ValueError(f"规则编号缺失或重复：{rid!r}")
        seen.add(rid)
        if rule.get("level") not in LEVEL_RANK:
            raise ValueError(f"规则 {rid} 的等级非法")
        for cond in rule.get("when", []):
            if cond.get("fact") is None or cond.get("op") not in _OPS:
                raise ValueError(f"规则 {rid} 的条件非法")
    actions = spec.get("level_actions", {})
    if not isinstance(actions, dict):
        raise ValueError("level_actions 必须是对象")
    for level, acts in actions.items():
        if level not in LEVEL_RANK:
            raise ValueError(f"level_actions 含未知等级：{level}")
        if not isinstance(acts, list):
            raise ValueError(f"等级 {level} 的动作必须是列表")


def _condition_matches(cond: dict[str, Any], facts: dict[str, Any]) -> bool:
    actual = facts.get(cond["fact"])
    if cond["op"] != "eq" and actual is None:
        return False
    return _OPS[cond["op"]](actual, cond["value"])


def evaluate(spec: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    """依据规则集评定等级。

    返回命中规则、最终等级、该等级要求的限制动作，以及用于解释的
    事实快照（评级时把整体结果存入事件，形成可追溯快照）。
    """
    validate_spec(spec)
    matched: list[dict[str, Any]] = []
    level = LEVEL_LOW
    for rule in spec["rules"]:
        if all(_condition_matches(c, facts) for c in rule.get("when", [])):
            matched.append({"rule_id": rule["id"], "level": rule["level"], "name": rule.get("name", "")})
            if LEVEL_RANK[rule["level"]] > LEVEL_RANK[level]:
                level = rule["level"]
    required_actions = list(spec.get("level_actions", {}).get(level, []))
    return {
        "level": level,
        "matched_rules": matched,
        "required_actions": required_actions,
        "facts": dict(facts),
    }
