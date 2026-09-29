"""演示数据：企业突然停止提供实习岗位的完整剧情。

背景：协议在法务、课程在教务、企业风险在企业合作部，三处信息互不相通，
付款节点仍按原计划推进。风控人员把各部门识别出的风险登记进同一本台账，
按版本规则评级、把限制传播到招生/付款/里程碑，并走完例外、复查、降级、
解除、复开的完整决定链。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import events as ev
from .service import RiskService

START = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)

# v1 评级规则：事实来自各部门上报
RULESET_V1 = {
    "rules": [
        {
            "id": "R-CRIT-PAY",
            "name": "实习停供且付款仍推进",
            "when": [
                {"fact": "internships_suspended", "op": "eq", "value": True},
                {"fact": "payment_nodes_advancing", "op": "eq", "value": True},
            ],
            "level": "严重",
        },
        {
            "id": "R-HIGH-ENROLL",
            "name": "实习停供波及大批学生",
            "when": [
                {"fact": "internships_suspended", "op": "eq", "value": True},
                {"fact": "affected_students", "op": "gte", "value": 20},
            ],
            "level": "高",
        },
        {
            "id": "R-MID-SCOPE",
            "name": "波及学生超过一个小组",
            "when": [{"fact": "affected_students", "op": "gte", "value": 5}],
            "level": "中",
        },
    ],
    "level_actions": {
        "严重": [ev.ACTION_FREEZE_PAYMENT, ev.ACTION_HALT_ENROLLMENT, ev.ACTION_HOLD_MILESTONE],
        "高": [ev.ACTION_HALT_ENROLLMENT, ev.ACTION_HOLD_MILESTONE],
        "中": [ev.ACTION_HOLD_MILESTONE],
        "低": [],
    },
}


def seed_demo(service: RiskService) -> dict:
    clock = service.clock
    clock.set(START)

    # 1) 发布 v1 规则（9 月 1 日已生效）
    service.publish_ruleset(
        RULESET_V1,
        effective_from=START.replace(day=1),
        actor="项目秘书处",
        note="合作项目风险评级首版规则",
    )

    # 2) 企业合作部登记主风险（此时付款部门尚未同步信息）
    main = service.register_risk(
        title="合作企业突然停止提供实习岗位",
        source={"name": "华东智造集团", "type": "合作企业", "dept": "企业合作部",
                "channel": "企业联系人书面通知"},
        target={"name": "2026 秋季校企合作班", "dept": "招生与培养处",
                "flows": [ev.FLOW_ENROLLMENT, ev.FLOW_PAYMENT, ev.FLOW_MILESTONE]},
        owner={"name": "王风控", "dept": "项目秘书处", "contact": "wang@registry"},
        mitigation="启动备选企业清单，两周内补足实习席位；付款材料暂缓提交",
        next_review_at=START + timedelta(days=5),
        actor="项目秘书处",
    )
    main_id = main["risk_id"]

    # 企业风险本身：评定为高（招生、里程碑受限；此时尚不知付款仍在推进）
    rating_high = service.rate_risk(
        main_id,
        {"internships_suspended": True, "payment_nodes_advancing": False, "affected_students": 32},
        actor="王风控",
    )

    # 3) 财务部门的独立登记：付款节点仍按原计划推进 → 手工冻结付款
    finance = service.register_risk(
        title="付款节点在实习停供后仍按原计划推进",
        source={"name": "共享付款台账", "type": "内部系统", "dept": "财务部"},
        target={"name": "Q3 企业合作款付款节点", "dept": "财务部", "flows": [ev.FLOW_PAYMENT]},
        owner={"name": "李会计", "dept": "财务部", "contact": "li@registry"},
        mitigation="暂停付款审批，待教学交付核实后恢复",
        actor="李会计",
    )
    finance_id = finance["risk_id"]
    service.add_restriction(
        finance_id, ev.ACTION_FREEZE_PAYMENT, reason="付款依据的实习交付已中断", actor="李会计"
    )

    # 教务部门的独立登记：课程排期依赖实习岗位 → 暂停里程碑
    academic = service.register_risk(
        title="课程里程碑依赖的实习排课无法落实",
        source={"name": "实习管理系统", "type": "内部系统", "dept": "教务处"},
        target={"name": "第 8 周岗位实训里程碑", "dept": "教务处", "flows": [ev.FLOW_MILESTONE]},
        owner={"name": "赵教务", "dept": "教务处"},
        mitigation="调整为校内实训过渡方案",
        actor="赵教务",
    )
    academic_id = academic["risk_id"]
    service.add_restriction(
        academic_id, ev.ACTION_HOLD_MILESTONE, reason="实习岗位为零，里程碑无法验收", actor="赵教务"
    )

    # 付款门禁：此时应被财务风险阻断
    gate_payment_before_merge = service.gate_check(ev.FLOW_PAYMENT)

    # 4) 风险合并：财务/教务风险并入主风险，生效限制随之带入（去重）
    clock.set(START + timedelta(days=1))
    merge_finance = service.merge_risks(
        finance_id, main_id, reason="同一企业停供事件的资金侧表现，归口统一管理", actor="项目秘书处"
    )
    merge_academic = service.merge_risks(
        academic_id, main_id, reason="同一企业停供事件的教学侧表现，归口统一管理", actor="项目秘书处"
    )

    # 5) 合并后拿到完整事实重新评级：严重（付款也被规则要求冻结）
    rating_critical = service.rate_risk(
        main_id,
        {"internships_suspended": True, "payment_nodes_advancing": True, "affected_students": 32},
        actor="王风控",
    )

    # 6) 审批人员为例外开口子：允许在学班级完成当期里程碑评估，9 月 24 日到期
    exception = service.grant_exception(
        main_id,
        ev.ACTION_HOLD_MILESTONE,
        approver="审批人员-周总监",
        reason="仅限在学 32 名学生完成当期已开始的评估，不新增批次",
        valid_until=START + timedelta(days=4),
    )
    gate_milestone_excused = service.gate_check(ev.FLOW_MILESTONE)

    # 7) 9 月 25 日定时复查：复查到期提醒 + 例外到期自动失效
    clock.set(START + timedelta(days=5))
    due_run = service.run_due_reviews()
    gate_milestone_after_expiry = service.gate_check(ev.FLOW_MILESTONE)

    # 8) 复查结论 ADJUSTED：付款已冻结、事实更新 → 降级为高，付款限制解除并恢复
    review_adjusted = service.record_review(
        main_id,
        ev.REVIEW_ADJUSTED,
        note="付款节点已冻结，备选企业席位落实 10 个",
        facts={"internships_suspended": True, "payment_nodes_advancing": False, "affected_students": 32},
        next_review_at=clock.now() + timedelta(days=15),
        actor="王风控",
    )
    gate_payment_restored = service.gate_check(ev.FLOW_PAYMENT)

    # 9) 10 月 10 日复查确认解除：全部限制解除、风险关闭，招生/里程碑恢复
    clock.set(START + timedelta(days=20))
    service.run_due_reviews()
    review_resolved = service.record_review(
        main_id, ev.REVIEW_RESOLVED, note="备选企业 32 个席位全部落实，付款依据补齐", actor="王风控"
    )

    # 10) 10 月 15 日企业再次通知停供：复开，按当前规则重新评定为中
    clock.set(START + timedelta(days=25))
    reopen = service.reopen_risk(
        main_id,
        reason="华东智造集团二次通知：下月岗位再次缩减",
        facts={"internships_suspended": True, "payment_nodes_advancing": False, "affected_students": 8},
        next_review_at=clock.now() + timedelta(days=5),
        actor="项目秘书处",
    )

    return {
        "ruleset_version": 1,
        "main_risk_id": main_id,
        "finance_risk_id": finance_id,
        "academic_risk_id": academic_id,
        "first_rating": {"level": rating_high["level"], "changes": rating_high["changes"]},
        "gate_payment_before_merge": gate_payment_before_merge["decision"],
        "merge_finance_inherited": merge_finance["inherited_actions"],
        "merge_academic_inherited": merge_academic["inherited_actions"],
        "critical_rating": {"level": rating_critical["level"], "changes": rating_critical["changes"]},
        "exception_id": exception["exception_id"],
        "gate_milestone_with_exception": gate_milestone_excused["decision"],
        "due_run_at": due_run["at"],
        "expired_exception_count": len(due_run["expired_exceptions"]),
        "due_review_count": len(due_run["due"]),
        "gate_milestone_after_expiry": gate_milestone_after_expiry["decision"],
        "adjusted_review": {
            "conclusion": review_adjusted["conclusion"],
            "level": review_adjusted["rating"]["level"],
            "released": review_adjusted["rating"]["changes"]["released"],
        },
        "gate_payment_restored": gate_payment_restored["decision"],
        "resolved_review_seq": review_resolved["event_seq"],
        "reopen": {
            "level": reopen["rating"]["level"],
            "actions_added": reopen["rating"]["changes"]["added"],
        },
        "chain_valid": service.verify_chain()["valid"],
    }
