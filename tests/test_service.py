"""风险登记服务端的端到端领域测试。

用可控时钟驱动“企业停供实习岗位”场景，覆盖：
六要素登记、版本规则评级、限制传播、门禁解释、
合并、降级、例外批准/到期、定时复查、解除恢复、复开、哈希链。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from risk_service import bootstrap, events as ev
from risk_service.clock import MutableClock
from risk_service.rules import LEVEL_CRITICAL, LEVEL_HIGH, LEVEL_LOW, LEVEL_MEDIUM
from risk_service.service import RiskService, ServiceError
from risk_service.store import Store

T0 = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)


def spec_v1():
    import copy
    return copy.deepcopy(bootstrap.RULESET_V1)


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = MutableClock(T0)
        self.store = Store(":memory:")
        self.svc = RiskService(self.store, self.clock)
        self.svc.publish_ruleset(spec_v1(), effective_from=T0 - timedelta(days=19))

    def register(self, **overrides):
        params = dict(
            title="企业停供实习岗位",
            source={"name": "华东智造集团", "dept": "企业合作部"},
            target={"name": "秋季合作班", "dept": "招生处"},
            owner={"name": "王风控", "dept": "项目秘书处"},
            mitigation="备选企业补位",
            next_review_at=T0 + timedelta(days=5),
        )
        params.update(overrides)
        return self.svc.register_risk(**params)


class TestRegistrationAndRating(ServiceTestBase):
    def test_six_elements_linked(self) -> None:
        result = self.register()
        detail = self.svc.risk_detail(result["risk_id"])
        risk = detail["risk"]
        self.assertEqual(risk["source"]["name"], "华东智造集团")
        self.assertEqual(risk["target"]["name"], "秋季合作班")
        self.assertEqual(risk["owner"]["name"], "王风控")
        self.assertEqual(risk["mitigation"], "备选企业补位")
        self.assertIsNotNone(risk["next_review_at"])
        self.assertEqual(risk["status"], "OPEN")

    def test_party_validation(self) -> None:
        with self.assertRaises(ServiceError):
            self.register(source={"dept": "缺名称"})

    def test_rating_snapshots_rule_version(self) -> None:
        rid = self.register()["risk_id"]
        rating = self.svc.rate_risk(
            rid, {"internships_suspended": True, "payment_nodes_advancing": True,
                  "affected_students": 32},
        )
        self.assertEqual(rating["level"], LEVEL_CRITICAL)
        self.assertEqual(rating["rule_version"], 1)
        self.assertIn("R-CRIT-PAY", [r["rule_id"] for r in rating["matched_rules"]])
        risk = self.store.get_risk(rid)
        self.assertEqual(risk["current_level"], LEVEL_CRITICAL)
        self.assertEqual(risk["current_rule_version"], 1)
        # 评级事件中保存完整快照
        events = self.svc.history(rid)
        rated = [e for e in events if e["event_type"] == ev.RISK_RATED][0]
        self.assertEqual(rated["data"]["facts"]["affected_students"], 32)

    def test_rating_propagates_restrictions_to_flows(self) -> None:
        rid = self.register()["risk_id"]
        self.svc.rate_risk(
            rid, {"internships_suspended": True, "payment_nodes_advancing": False,
                  "affected_students": 32},
        )
        self.assertEqual(self.svc.gate_check(ev.FLOW_ENROLLMENT)["decision"], ev.GATE_BLOCKED)
        self.assertEqual(self.svc.gate_check(ev.FLOW_MILESTONE)["decision"], ev.GATE_BLOCKED)
        self.assertEqual(self.svc.gate_check(ev.FLOW_PAYMENT)["decision"], ev.GATE_ALLOWED)

    def test_no_ruleset_yet(self) -> None:
        store = Store(":memory:")
        svc = RiskService(store, self.clock)
        rid = svc.register_risk(
            title="t", source={"name": "s"}, target={"name": "t"}, owner={"name": "o"}
        )["risk_id"]
        with self.assertRaises(ServiceError):
            svc.rate_risk(rid, {"x": 1})

    def test_ruleset_version_monotonic_and_effective_window(self) -> None:
        with self.assertRaises(ServiceError):
            self.svc.publish_ruleset(spec_v1(), version=1)
        # 新规则未来生效时，评级仍按当前已生效版本
        rid = self.register()["risk_id"]
        v2 = {
            "rules": [{"id": "R-ALL", "name": "任意事实", "when": [], "level": LEVEL_LOW}],
            "level_actions": {"低": [ev.ACTION_FREEZE_PAYMENT]},
        }
        self.svc.publish_ruleset(v2, effective_from=T0 + timedelta(days=30), note="v2")
        rating = self.svc.rate_risk(
            rid, {"internships_suspended": False, "payment_nodes_advancing": False,
                  "affected_students": 0},
        )
        self.assertEqual(rating["rule_version"], 1)
        self.assertEqual(rating["level"], LEVEL_LOW)


class TestGateExplanation(ServiceTestBase):
    def test_gate_explains_why_in_effect(self) -> None:
        rid = self.register()["risk_id"]
        self.svc.rate_risk(
            rid, {"internships_suspended": True, "payment_nodes_advancing": False,
                  "affected_students": 32},
        )
        gate = self.svc.gate_check(ev.FLOW_ENROLLMENT)
        self.assertEqual(gate["decision"], ev.GATE_BLOCKED)
        blocker = gate["restrictions"][0]
        self.assertEqual(blocker["risk_id"], rid)
        self.assertIn("规则 v1", blocker["why_in_effect"])
        self.assertEqual(blocker["origin"], ev.ORIGIN_RULE)

    def test_explain_restriction_has_chain_and_snapshot(self) -> None:
        rid = self.register()["risk_id"]
        self.svc.rate_risk(
            rid, {"internships_suspended": True, "payment_nodes_advancing": False,
                  "affected_students": 32},
        )
        restriction_id = self.store.all_active_restrictions()[0]["id"]
        explanation = self.svc.explain_restriction(restriction_id)
        types = [c["type"] for c in explanation["decision_chain"]]
        self.assertIn(ev.RISK_CONTROL_ADDED, types)
        self.assertEqual(explanation["rating_snapshot"]["data"]["rule_version"], 1)

    def test_target_specific_restriction_does_not_block_other_targets(self) -> None:
        rid = self.register()["risk_id"]
        self.svc.add_restriction(
            rid, ev.ACTION_FREEZE_PAYMENT, target_ref="PAY-Q3-007", reason="仅该笔付款待核"
        )
        self.assertEqual(
            self.svc.gate_check(ev.FLOW_PAYMENT, target_ref="PAY-Q3-007")["decision"],
            ev.GATE_BLOCKED,
        )
        self.assertEqual(
            self.svc.gate_check(ev.FLOW_PAYMENT, target_ref="PAY-Q3-008")["decision"],
            ev.GATE_ALLOWED,
        )


class TestException(ServiceTestBase):
    def _excused_risk(self):
        rid = self.register()["risk_id"]
        self.svc.rate_risk(
            rid, {"internships_suspended": True, "payment_nodes_advancing": False,
                  "affected_students": 32},
        )
        grant = self.svc.grant_exception(
            rid, ev.ACTION_HALT_ENROLLMENT, approver="周总监", reason="老生续读",
            valid_until=T0 + timedelta(days=2),
        )
        return rid, grant

    def test_exception_allows_with_record(self) -> None:
        _, grant = self._excused_risk()
        gate = self.svc.gate_check(ev.FLOW_ENROLLMENT)
        self.assertEqual(gate["decision"], ev.GATE_ALLOWED_WITH_EXCEPTION)
        self.assertEqual(gate["restrictions"][0]["exception"]["approver"], "周总监")
        self.assertEqual(gate["restrictions"][0]["exception"]["exception_id"], grant["exception_id"])

    def test_exception_expires_on_clock_advance(self) -> None:
        rid, _grant = self._excused_risk()
        self.clock.advance(days=3)
        result = self.svc.run_due_reviews()
        self.assertEqual(len(result["expired_exceptions"]), 1)
        self.assertEqual(self.svc.gate_check(ev.FLOW_ENROLLMENT)["decision"], ev.GATE_BLOCKED)
        # 到期事件进决定链
        history = self.svc.history(rid)
        self.assertIn(ev.EXCEPTION_EXPIRED, [e["event_type"] for e in history])

    def test_revoke_exception(self) -> None:
        _, grant = self._excused_risk()
        self.svc.revoke_exception(grant["exception_id"], reason="情况变化")
        self.assertEqual(self.svc.gate_check(ev.FLOW_ENROLLMENT)["decision"], ev.GATE_BLOCKED)

    def test_grant_requires_active_restriction(self) -> None:
        rid = self.register()["risk_id"]
        with self.assertRaises(ServiceError):
            self.svc.grant_exception(rid, ev.ACTION_FREEZE_PAYMENT, approver="a", reason="r")

    def test_invalid_validity_window(self) -> None:
        rid = self.register()["risk_id"]
        self.svc.add_restriction(rid, ev.ACTION_FREEZE_PAYMENT)
        with self.assertRaises(ServiceError):
            self.svc.grant_exception(
                rid, ev.ACTION_FREEZE_PAYMENT, approver="a", reason="r",
                valid_until=T0 - timedelta(days=1),
            )


class TestMerge(ServiceTestBase):
    def test_merge_inherits_restrictions_and_marks_source(self) -> None:
        main = self.register(title="主风险")["risk_id"]
        other = self.register(title="财务风险")["risk_id"]
        self.svc.add_restriction(other, ev.ACTION_FREEZE_PAYMENT, reason="付款无依据")
        result = self.svc.merge_risks(other, main, reason="同一事件")
        self.assertEqual(result["inherited_actions"], [ev.ACTION_FREEZE_PAYMENT])
        self.assertEqual(self.store.get_risk(other)["status"], ev.STATUS_MERGED)
        self.assertEqual(self.svc.gate_check(ev.FLOW_PAYMENT)["decision"], ev.GATE_BLOCKED)
        # 决定链包含合并事件，且带入限制注明来源
        added = [e for e in self.svc.history(main) if e["event_type"] == ev.RISK_CONTROL_ADDED]
        self.assertEqual(added[-1]["data"]["origin"], ev.ORIGIN_MERGE)

    def test_merge_deduplicates_existing_action(self) -> None:
        main = self.register(title="主风险")["risk_id"]
        self.svc.add_restriction(main, ev.ACTION_FREEZE_PAYMENT)
        other = self.register(title="财务风险")["risk_id"]
        self.svc.add_restriction(other, ev.ACTION_FREEZE_PAYMENT)
        result = self.svc.merge_risks(other, main, reason="同一事件")
        self.assertEqual(result["inherited_actions"], [])

    def test_cannot_mutate_merged_risk(self) -> None:
        main = self.register()["risk_id"]
        other = self.register()["risk_id"]
        self.svc.merge_risks(other, main, reason="x")
        with self.assertRaises(ServiceError):
            self.svc.rate_risk(other, {"x": 1})


class TestDowngradeAndRestore(ServiceTestBase):
    def test_downgrade_releases_and_restores_flow(self) -> None:
        rid = self.register()["risk_id"]
        self.svc.rate_risk(
            rid, {"internships_suspended": True, "payment_nodes_advancing": True,
                  "affected_students": 32},
        )
        self.assertEqual(self.svc.gate_check(ev.FLOW_PAYMENT)["decision"], ev.GATE_BLOCKED)
        rating = self.svc.rate_risk(
            rid, {"internships_suspended": True, "payment_nodes_advancing": False,
                  "affected_students": 32},
        )
        self.assertEqual(rating["level"], LEVEL_HIGH)
        self.assertIn(ev.ACTION_FREEZE_PAYMENT, rating["changes"]["released"])
        self.assertEqual(self.svc.gate_check(ev.FLOW_PAYMENT)["decision"], ev.GATE_ALLOWED)
        history = self.svc.history(rid)
        self.assertIn(ev.RISK_DOWNGRADED, [e["event_type"] for e in history])
        restored = [e for e in history if e["event_type"] == ev.FLOW_RESTORED]
        pay = [e for e in restored if e["data"]["flow"] == ev.FLOW_PAYMENT][0]
        self.assertTrue(pay["data"]["restored"])
        self.assertIn("恢复", pay["data"]["resumes_what"])

    def test_flow_stays_blocked_when_another_risk_covers_it(self) -> None:
        rid1 = self.register(title="风险一")["risk_id"]
        rid2 = self.register(title="风险二")["risk_id"]
        self.svc.add_restriction(rid1, ev.ACTION_FREEZE_PAYMENT)
        self.svc.add_restriction(rid2, ev.ACTION_FREEZE_PAYMENT)
        self.svc.release_restriction(rid1, ev.ACTION_FREEZE_PAYMENT, reason="已核实")
        self.assertEqual(self.svc.gate_check(ev.FLOW_PAYMENT)["decision"], ev.GATE_BLOCKED)
        restored = [
            e for e in self.svc.history(rid1)
            if e["event_type"] == ev.FLOW_RESTORED and e["data"]["flow"] == ev.FLOW_PAYMENT
        ][0]
        self.assertFalse(restored["data"]["restored"])

    def test_manual_restriction_survives_downgrade(self) -> None:
        rid = self.register()["risk_id"]
        self.svc.rate_risk(
            rid, {"internships_suspended": True, "payment_nodes_advancing": True,
                  "affected_students": 32},
        )
        self.svc.add_restriction(rid, ev.ACTION_HOLD_MILESTONE, reason="校长特批加严")
        rating = self.svc.rate_risk(
            rid, {"internships_suspended": False, "payment_nodes_advancing": False,
                  "affected_students": 0},
        )
        self.assertEqual(rating["level"], LEVEL_LOW)
        actions = {r["action"] for r in self.store.list_restrictions(rid, active_only=True)}
        self.assertIn(ev.ACTION_HOLD_MILESTONE, actions)
        self.assertEqual(self.svc.gate_check(ev.FLOW_MILESTONE)["decision"], ev.GATE_BLOCKED)


class TestScheduledReview(ServiceTestBase):
    def test_due_review_uses_controllable_clock_and_is_idempotent(self) -> None:
        rid = self.register()["risk_id"]
        run = self.svc.run_due_reviews()
        self.assertEqual(run["due"], [])
        self.clock.advance(days=6)
        run = self.svc.run_due_reviews()
        self.assertEqual([d["risk_id"] for d in run["due"]], [rid])
        # 同一复查周期不重复提醒
        again = self.svc.run_due_reviews()
        self.assertEqual(again["due"], [])

    def test_review_resolved_closes_and_releases_all(self) -> None:
        rid = self.register()["risk_id"]
        self.svc.rate_risk(
            rid, {"internships_suspended": True, "payment_nodes_advancing": True,
                  "affected_students": 32},
        )
        result = self.svc.record_review(rid, ev.REVIEW_RESOLVED, note="已补位")
        self.assertEqual(result["conclusion"], ev.REVIEW_RESOLVED)
        self.assertEqual(self.store.get_risk(rid)["status"], ev.STATUS_CLOSED)
        for flow in ev.FLOWS:
            self.assertEqual(self.svc.gate_check(flow)["decision"], ev.GATE_ALLOWED)
        detail = self.svc.risk_detail(rid)
        restored_flows = {item["flow"] for item in detail["restored_flows"] if item["restored"]}
        self.assertEqual(restored_flows, set(ev.FLOWS))

    def test_review_adjusted_derates_and_schedules_next(self) -> None:
        rid = self.register()["risk_id"]
        self.svc.rate_risk(
            rid, {"internships_suspended": True, "payment_nodes_advancing": True,
                  "affected_students": 32},
        )
        self.clock.advance(days=5)
        self.svc.run_due_reviews()
        self.svc.record_review(
            rid, ev.REVIEW_ADJUSTED,
            facts={"internships_suspended": True, "payment_nodes_advancing": False,
                   "affected_students": 32},
            next_review_at=T0 + timedelta(days=20),
        )
        self.assertEqual(self.svc.gate_check(ev.FLOW_PAYMENT)["decision"], ev.GATE_ALLOWED)
        # 记录复查后进入下一周期，旧到期不再提醒；新周期到期会再次提醒
        self.clock.set(T0 + timedelta(days=19))
        self.assertEqual(self.svc.run_due_reviews()["due"], [])
        self.clock.set(T0 + timedelta(days=21))
        self.assertEqual(len(self.svc.run_due_reviews()["due"]), 1)

    def test_invalid_conclusion_rejected(self) -> None:
        rid = self.register()["risk_id"]
        with self.assertRaises(ServiceError):
            self.svc.record_review(rid, "随便")


class TestReopen(ServiceTestBase):
    def test_reopen_re_rates_and_re_propagates(self) -> None:
        rid = self.register()["risk_id"]
        self.svc.rate_risk(
            rid, {"internships_suspended": True, "payment_nodes_advancing": True,
                  "affected_students": 32},
        )
        self.svc.close_risk(rid, reason="已解决")
        self.assertEqual(self.svc.gate_check(ev.FLOW_MILESTONE)["decision"], ev.GATE_ALLOWED)
        result = self.svc.reopen_risk(
            rid, reason="企业二次停供",
            facts={"internships_suspended": True, "payment_nodes_advancing": False,
                   "affected_students": 8},
        )
        self.assertEqual(result["rating"]["level"], LEVEL_MEDIUM)
        self.assertEqual(self.store.get_risk(rid)["status"], ev.STATUS_OPEN)
        self.assertEqual(self.svc.gate_check(ev.FLOW_MILESTONE)["decision"], ev.GATE_BLOCKED)
        self.assertEqual(self.svc.gate_check(ev.FLOW_ENROLLMENT)["decision"], ev.GATE_ALLOWED)
        history = self.svc.history(rid)
        self.assertIn(ev.RISK_REOPENED, [e["event_type"] for e in history])

    def test_reopen_only_closed(self) -> None:
        rid = self.register()["risk_id"]
        with self.assertRaises(ServiceError):
            self.svc.reopen_risk(rid, reason="x")


class TestDecisionChain(unittest.TestCase):
    def test_hash_chain_valid_for_full_scenario(self) -> None:
        store = Store(":memory:")
        clock = MutableClock(bootstrap.START)
        svc = RiskService(store, clock)
        bootstrap.seed_demo(svc)
        self.assertTrue(store.verify_chain())

    def test_tampering_detected(self) -> None:
        store = Store(":memory:")
        clock = MutableClock(T0)
        svc = RiskService(store, clock)
        svc.publish_ruleset(spec_v1(), effective_from=T0 - timedelta(days=1))
        rid = svc.register_risk(
            title="t", source={"name": "s"}, target={"name": "t"}, owner={"name": "o"}
        )["risk_id"]
        svc.rate_risk(rid, {"internships_suspended": True, "payment_nodes_advancing": True,
                            "affected_students": 32})
        store.conn.execute("UPDATE event_log SET actor = '伪造者' WHERE seq = 2")
        store.conn.commit()
        self.assertFalse(store.verify_chain())


class TestBootstrapScenario(unittest.TestCase):
    def test_full_demo_report(self) -> None:
        store = Store(":memory:")
        svc = RiskService(store, MutableClock(bootstrap.START))
        report = bootstrap.seed_demo(svc)
        self.assertEqual(report["first_rating"]["level"], LEVEL_HIGH)
        self.assertEqual(report["gate_payment_before_merge"], ev.GATE_BLOCKED)
        self.assertEqual(report["critical_rating"]["level"], LEVEL_CRITICAL)
        self.assertEqual(report["gate_milestone_with_exception"], ev.GATE_ALLOWED_WITH_EXCEPTION)
        self.assertEqual(report["expired_exception_count"], 1)
        self.assertEqual(report["gate_milestone_after_expiry"], ev.GATE_BLOCKED)
        self.assertEqual(report["adjusted_review"]["level"], LEVEL_HIGH)
        self.assertIn(ev.ACTION_FREEZE_PAYMENT, report["adjusted_review"]["released"])
        self.assertEqual(report["gate_payment_restored"], ev.GATE_ALLOWED)
        self.assertEqual(report["reopen"]["level"], LEVEL_MEDIUM)
        self.assertEqual(report["reopen"]["actions_added"], [ev.ACTION_HOLD_MILESTONE])
        self.assertTrue(report["chain_valid"])


if __name__ == "__main__":
    unittest.main()
