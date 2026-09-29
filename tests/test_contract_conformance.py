"""领域契约与服务端实现的一致性回归。

契约（domain/contract.json）是权威领域定义；本测试保证服务端的
事件、动作、流程、等级、门禁结论始终与契约对齐，防止实现悄悄偏离契约。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from domain_contract.validator import load_contract
from risk_service import events as ev
from risk_service.rules import LEVEL_RANK

CONTRACT = load_contract(ROOT / "domain" / "contract.json")


class ContractConformanceTest(unittest.TestCase):
    def test_control_actions_match(self) -> None:
        self.assertEqual(CONTRACT["control_actions"], list(ev.ACTIONS))

    def test_propagated_flows_match(self) -> None:
        self.assertEqual(CONTRACT["propagated_flows"], list(ev.FLOWS))

    def test_action_flow_mapping_is_bijection(self) -> None:
        self.assertEqual(set(ev.ACTION_FLOW.keys()), set(ev.ACTIONS))
        self.assertEqual(set(ev.ACTION_FLOW.values()), set(ev.FLOWS))

    def test_rating_levels_match(self) -> None:
        self.assertEqual(CONTRACT["rating_levels"], list(LEVEL_RANK))

    def test_gate_decisions_match(self) -> None:
        self.assertEqual(
            CONTRACT["gate_decisions"],
            [ev.GATE_ALLOWED, ev.GATE_ALLOWED_WITH_EXCEPTION, ev.GATE_BLOCKED],
        )

    def test_lifecycle_statuses_match(self) -> None:
        self.assertEqual(CONTRACT["risk_lifecycle"], [ev.STATUS_OPEN, ev.STATUS_MERGED, ev.STATUS_CLOSED])

    def test_decision_events_exist_as_constants(self) -> None:
        for name in CONTRACT["decision_events"]:
            self.assertTrue(hasattr(ev, name), f"事件常量缺失：{name}")
            self.assertEqual(getattr(ev, name), name)

    def test_risk_elements_cover_six_fields(self) -> None:
        self.assertEqual(len(CONTRACT["risk_elements"]), 6)


if __name__ == "__main__":
    unittest.main()
