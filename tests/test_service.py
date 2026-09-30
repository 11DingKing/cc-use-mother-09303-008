"""风险登记服务端的业务回归测试。

用 FixedClock 保证定时复查与到期判断完全可复现。
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from risk_register import (  # noqa: E402
    AffectedObject,
    FixedClock,
    Owner,
    Repository,
    RiskService,
    RiskSource,
)
from risk_register.models import (  # noqa: E402
    DecisionType,
    RestrictionAction,
    RestrictionStatus,
    RiskStatus,
)
from risk_register.rules import default_registry  # noqa: E402


def make_service(start: str = "2026-09-30") -> tuple[RiskService, FixedClock]:
    clock = FixedClock(start)
    svc = RiskService(Repository(), default_registry(), clock)
    return svc, clock


def enterprise_risk(svc: RiskService, **kw) -> str:
    payload = dict(
        actor="项目秘书处", title="某企业停止提供实习岗位",
        source=RiskSource("企业风险", "企业合作部", "XX教育科技", "停止供给实习岗位"),
        affected=[
            AffectedObject("付款节点", "P2", "第二期合作款"),
            AffectedObject("招生批次", "E2026-FALL", "秋季招生"),
            AffectedObject("里程碑", "M3", "实习安置验收"),
        ],
        owner=Owner("李工", "风险责任人", "风控办"),
        review_interval_days=30,
    )
    payload.update(kw)
    return svc.create_risk(**payload).id


class RatingTest(unittest.TestCase):
    def test_latest_ruleset_rates_critical_and_blocks_payment(self) -> None:
        svc, _ = make_service()
        rid = enterprise_risk(svc)
        svc.rate_risk(rid, actor="王审批")
        risk = svc.repo.get_risk(rid)
        # R1 2 + R2 2 + R3 1 + R4 1 + R5 1 = 7 -> 极高
        self.assertEqual(risk.rating.level, "极高")
        self.assertEqual(risk.rating.score, 7)
        self.assertEqual(risk.rating.ruleset_version, "1.1.0")
        actions = {(r.action, r.flow_type) for r in svc.repo.list_restrictions()}
        self.assertIn((str(RestrictionAction.BLOCK), "付款"), actions)
        self.assertIn((str(RestrictionAction.HOLD), "付款"), actions)
        self.assertIn((str(RestrictionAction.HOLD), "里程碑"), actions)

    def test_rating_pins_ruleset_version_for_later_explanation(self) -> None:
        svc, _ = make_service()
        rid = enterprise_risk(svc)
        svc.rate_risk(rid, actor="王审批", ruleset_version="1.0.0")
        risk = svc.repo.get_risk(rid)
        self.assertEqual(risk.rating.ruleset_version, "1.0.0")
        # v1 没有等级阻断动作：付款只有冻结，没有阻断
        actions = [r.action for r in svc.repo.list_restrictions() if r.flow_type == "付款"]
        self.assertNotIn(str(RestrictionAction.BLOCK), actions)
        # 解释仍能按 1.0.0 版本找到规则描述
        hold = svc.repo.list_restrictions()[0]
        explanation = svc.explain_restriction(hold.id)
        self.assertTrue(explanation["rule_description"])
        self.assertEqual(explanation["ruleset_version"], "1.0.0")

    def test_confirmed_mitigation_reduces_score(self) -> None:
        svc, _ = make_service()
        rid = enterprise_risk(svc, affected=[AffectedObject("付款节点", "P2", "第二期款")])
        svc.rate_risk(rid, actor="王审批")  # 2+2+1=5 高
        m = svc.add_mitigation(rid, actor="李工", description="启用备用企业",
                               owner="李工", due_date="2026-11-01")
        svc.confirm_mitigation(rid, m.id, actor="李工")
        risk = svc.repo.get_risk(rid)
        self.assertEqual(risk.rating.score, 3)
        self.assertEqual(risk.rating.level, "中")


class PropagationTest(unittest.TestCase):
    def test_restrictions_hold_and_restore_flows_transactionally(self) -> None:
        svc, _ = make_service()
        rid = enterprise_risk(svc)
        svc.rate_risk(rid, actor="王审批")
        pay = svc.flow_status("付款节点:P2")
        mile = svc.flow_status("里程碑:M3")
        enroll = svc.flow_status("招生批次:E2026-FALL")
        self.assertFalse(pay["running"])
        self.assertFalse(mile["running"])
        # 预警不阻断流程
        self.assertTrue(enroll["running"])
        self.assertTrue(enroll["warnings"])

        svc.downgrade_risk(rid, "低", actor="王审批", reason="备用企业承接")
        pay2 = svc.flow_status("付款节点:P2")
        mile2 = svc.flow_status("里程碑:M3")
        self.assertTrue(pay2["running"])
        self.assertTrue(mile2["running"])
        # 恢复记录可回答"解除后恢复了什么"
        restored = {entry["restriction_id"] for entry in pay2["restoration_log"]}
        self.assertEqual(len(restored), 2)  # 冻结 + 阻断 各一条

    def test_rerate_reconciles_restrictions_without_duplicates(self) -> None:
        svc, _ = make_service()
        rid = enterprise_risk(svc)
        svc.rate_risk(rid, actor="王审批")
        first = len(svc.repo.list_restrictions())
        svc.rate_risk(rid, actor="王审批", reason="无变化复评")
        svc.rate_risk(rid, actor="王审批", reason="再次无变化复评")
        self.assertEqual(len(svc.repo.list_restrictions()), first)


class ExemptionTest(unittest.TestCase):
    def test_limited_exemption_reinstates_after_expiry(self) -> None:
        svc, clock = make_service()
        rid = enterprise_risk(svc)
        svc.rate_risk(rid, actor="王审批")
        status = svc.flow_status("付款节点:P2")
        hold = next(b["restriction_id"] for b in status["blockers"]
                    if b["action"] == str(RestrictionAction.HOLD))
        svc.approve_exemption(hold, actor="王审批", approver="张校长",
                              reason="紧急发放补贴", valid_until="2026-10-15")
        # 冻结被例外放行，但阻断仍在 -> 流程依然不通
        during = svc.flow_status("付款节点:P2")
        self.assertEqual(len(during["exempted_blockers"]), 1)
        self.assertFalse(during["running"])
        # 到期后无需新决定，例外自动失效
        clock.advance(to="2026-10-16")
        after = svc.flow_status("付款节点:P2")
        self.assertEqual(len(after["exempted_blockers"]), 0)
        self.assertFalse(after["running"])
        # 例外批准留在决定链
        chain = svc.decision_chain(rid)
        self.assertIn(str(DecisionType.EXEMPT_APPROVAL), [d["type"] for d in chain])


class MergeDowngradeReopenTest(unittest.TestCase):
    def test_merge_transfers_restrictions_and_keeps_origin_chain(self) -> None:
        svc, _ = make_service()
        target = enterprise_risk(svc, title="停岗主风险",
                                 affected=[AffectedObject("付款节点", "P2", "二期款")])
        source = svc.create_risk(
            actor="秘书处", title="协议条款缺失",
            source=RiskSource("协议", "法务部", "XX教育科技", "旧协议"),
            affected=[AffectedObject("付款节点", "P3", "三期款")],
            owner=Owner("赵工", "风险责任人", "法务部"),
        ).id
        svc.rate_risk(target, actor="王审批")
        svc.rate_risk(source, actor="王审批", ruleset_version="1.0.0")
        svc.merge_risks([source], target, actor="王审批", reason="同一事件合并")

        self.assertEqual(svc.repo.get_risk(source).status, str(RiskStatus.MERGED))
        self.assertEqual(svc.repo.get_risk(source).merged_into, target)
        p3 = svc.flow_status("付款节点:P3")
        self.assertFalse(p3["running"])
        self.assertEqual(p3["blockers"][0]["origin_risk_ids"], [source])
        # 源风险不能复开，需复开承袭风险
        with self.assertRaises(ValueError):
            svc.reopen_risk(source, actor="秘书处", reason="复开")

    def test_downgrade_requires_lower_level_and_lifts(self) -> None:
        svc, _ = make_service()
        rid = enterprise_risk(svc)
        svc.rate_risk(rid, actor="王审批")
        with self.assertRaises(ValueError):
            svc.downgrade_risk(rid, "极高", actor="王审批", reason="不能升级")
        svc.downgrade_risk(rid, "低", actor="王审批", reason="影响消除")
        risk = svc.repo.get_risk(rid)
        # 规则评定结果不可变，人工降级只形成覆盖
        self.assertEqual(risk.rating.level, "极高")
        self.assertEqual(risk.current_level, "低")
        self.assertIsNotNone(risk.level_override)
        self.assertTrue(svc.flow_status("付款节点:P2")["running"])
        # 复评后覆盖失效，回到规则等级（该风险无缓解措施，仍为极高）
        svc.reopen_risk(rid, actor="秘书处", reason="复发")
        svc.rate_risk(rid, actor="王审批")
        self.assertIsNone(svc.repo.get_risk(rid).level_override)
        self.assertEqual(svc.repo.get_risk(rid).current_level, "极高")
        chain_types = [d["type"] for d in svc.decision_chain(rid)]
        self.assertIn(str(DecisionType.DOWNGRADE), chain_types)

    def test_reopen_after_close_keeps_chain_and_reschedules(self) -> None:
        svc, clock = make_service()
        rid = enterprise_risk(svc)
        svc.rate_risk(rid, actor="王审批")
        svc.close_risk(rid, actor="王审批", reason="处置完毕")
        self.assertEqual(svc.repo.get_risk(rid).status, str(RiskStatus.CLOSED))
        svc.reopen_risk(rid, actor="秘书处", reason="出现复发迹象")
        risk = svc.repo.get_risk(rid)
        self.assertEqual(risk.status, str(RiskStatus.IDENTIFIED))
        self.assertEqual(risk.reopen_count, 1)
        self.assertIsNotNone(risk.next_review_date)
        types = [d["type"] for d in svc.decision_chain(rid)]
        self.assertEqual(types.count(str(DecisionType.REOPEN)), 1)


class ScheduledReviewTest(unittest.TestCase):
    def test_due_reviews_run_with_controlled_clock(self) -> None:
        svc, clock = make_service("2026-09-30")
        rid = enterprise_risk(svc, review_interval_days=30)
        svc.rate_risk(rid, actor="王审批")
        self.assertFalse(svc.due_reviews())
        clock.advance(days=30)
        due = svc.due_reviews()
        self.assertEqual([r.id for r in due], [rid])
        results = svc.run_due_reviews()
        self.assertEqual(results[0]["risk_id"], rid)
        risk = svc.repo.get_risk(rid)
        self.assertEqual(risk.next_review_date, "2026-11-29")
        self.assertEqual(len(risk.review_history), 1)
        self.assertFalse(svc.due_reviews())

    def test_merged_and_closed_risks_are_not_reviewed(self) -> None:
        svc, clock = make_service()
        rid = enterprise_risk(svc)
        svc.rate_risk(rid, actor="王审批")
        svc.close_risk(rid, actor="王审批", reason="关闭")
        clock.advance(days=40)
        self.assertEqual(svc.due_reviews(), [])


class ExplanationTest(unittest.TestCase):
    def test_explain_says_why_restriction_is_active(self) -> None:
        svc, _ = make_service()
        rid = enterprise_risk(svc)
        svc.rate_risk(rid, actor="王审批")
        status = svc.flow_status("付款节点:P2")
        block = next(b for b in status["blockers"] if b["action"] == str(RestrictionAction.BLOCK))
        self.assertEqual(block["rule_id"], "R7-HIGH-BLOCK-PAYMENT")
        self.assertEqual(block["ruleset_version"], "1.1.0")
        self.assertEqual(block["risk_title"], "某企业停止提供实习岗位")
        full = svc.explain_restriction(block["restriction_id"])
        # 决定链可追到登记与评定
        self.assertIn("risk_decision_chain", full)
        self.assertGreaterEqual(len(full["risk_decision_chain"]), 2)


class PersistenceTest(unittest.TestCase):
    def test_snapshot_roundtrip_preserves_state(self) -> None:
        svc, _ = make_service()
        rid = enterprise_risk(svc)
        svc.rate_risk(rid, actor="王审批")
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            svc.repo.save_to(path)
            repo = Repository()
            repo.load_from(path)
            risk = repo.get_risk(rid)
            self.assertEqual(risk.rating.level, "极高")
            self.assertTrue(repo.list_restrictions())
            self.assertTrue(repo.list_flows())


if __name__ == "__main__":
    unittest.main()
