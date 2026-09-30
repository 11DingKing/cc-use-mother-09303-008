"""端到端场景演示：企业突然停止提供实习岗位。

用固定时钟驱动，直接在进程内调用服务层并按阶段打印结果。

    python3 tools/demo_scenario.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

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
from risk_register.rules import default_registry  # noqa: E402


def show(title: str, payload: object) -> None:
    print(f"\n===== {title} =====")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    clock = FixedClock("2026-09-30")
    svc = RiskService(Repository(), default_registry(), clock)

    # 1) 三个部门各自掌握一部分事实：企业停岗（企业合作部）、协议条款缺失
    #    （法务部）、课程排期未调整（教务部）。秘书处先登记主风险。
    main_risk = svc.create_risk(
        actor="项目秘书处", title="合作企业单方停止提供实习岗位",
        source=RiskSource(
            category="企业风险", department="企业合作部", party="XX教育科技",
            detail="开学前一周通知终止全部实习岗位，但付款节点仍按原计划推进",
        ),
        affected=[
            AffectedObject("付款节点", "P2", "第二期合作款"),
            AffectedObject("招生批次", "E2026-FALL", "秋季招生批次"),
            AffectedObject("里程碑", "M3", "实习安置验收"),
        ],
        owner=Owner("李工", "风险责任人", "风控办"),
        description="岗位供给中断，学生无法安置；付款与招生系统尚未感知",
        review_interval_days=30,
    )
    contract_risk = svc.create_risk(
        actor="项目秘书处", title="合作协议缺少停岗违约条款",
        source=RiskSource("协议", "法务部", "XX教育科技", "旧协议未约定停岗责任"),
        affected=[AffectedObject("付款节点", "P2", "第二期合作款")],
        owner=Owner("赵工", "风险责任人", "法务部"),
    )
    show("阶段1 登记：跨部门风险来源与影响对象", {
        "risks": [{"id": r.id, "title": r.title, "source": r.source.to_dict(),
                   "affected": [a.key() for a in r.affected],
                   "owner": r.owner.to_dict(), "next_review_date": r.next_review_date}
                  for r in svc.repo.list_risks()],
    })

    # 2) 按最新版规则（1.1.0）评定主风险；协议风险刻意按旧版 1.0.0 评定，
    #    证明历史评定始终绑定其规则版本。
    svc.rate_risk(main_risk.id, actor="王审批", reason="开学周紧急评定")
    svc.rate_risk(contract_risk.id, actor="王审批", ruleset_version="1.0.0",
                  reason="沿用签约时规则评定")
    main_risk = svc.repo.get_risk(main_risk.id)
    contract_risk = svc.repo.get_risk(contract_risk.id)
    show("阶段2 版本化评定与限制传播", {
        "main": {"id": main_risk.id, "level": main_risk.rating.level,
                 "score": main_risk.rating.score,
                 "ruleset_version": main_risk.rating.ruleset_version,
                 "matched_rules": main_risk.rating.matched_rules},
        "contract": {"id": contract_risk.id, "level": contract_risk.rating.level,
                     "ruleset_version": contract_risk.rating.ruleset_version},
        "flows": [{"flow": f.key, "running": f.running,
                   "held_by": len(f.held_by)} for f in svc.repo.list_flows()],
    })

    # 3) 查询：付款流程为何走不下去？
    payment = svc.flow_status("付款节点:P2")
    show("阶段3 解释限制为何生效", {
        "flow": payment["flow"]["key"], "running": payment["running"],
        "blockers": [{"action": b["action"], "rule_id": b["rule_id"],
                      "ruleset_version": b["ruleset_version"],
                      "rule": b["rule_description"], "reason": b["reason"],
                      "risk_id": b["risk_id"]} for b in payment["blockers"]],
        "warnings": [w["reason"] for w in payment["warnings"]],
    })

    # 4) 审批人员对"冻结"批准限期例外（先发学生补贴），但"阻断"仍在。
    hold = next(b["restriction_id"] for b in payment["blockers"] if b["action"] == "冻结")
    svc.approve_exemption(hold, actor="王审批", approver="张校长",
                          reason="先行发放学生生活补贴", valid_until="2026-10-15")
    during = svc.flow_status("付款节点:P2")
    clock.advance(to="2026-10-16")
    expired = svc.flow_status("付款节点:P2")
    show("阶段4 例外批准与到期自动失效", {
        "例外有效期内": {"running": during["running"],
                     "例外放行": [b["reason"] for b in during["exempted_blockers"]],
                     "仍阻断": [b["action"] for b in during["blockers"]]},
        "到期后(2026-10-16)": {"running": expired["running"],
                        "生效阻断数": len(expired["blockers"])},
    })

    # 5) 合并两条同根风险，限制由主风险承袭，来源链保留。
    svc.merge_risks([contract_risk.id], main_risk.id, actor="王审批",
                    reason="同一企业同一付款节点，合并处置")
    merged_status = svc.flow_status("付款节点:P2")
    show("阶段5 风险合并（限制承袭、来源链保留）", {
        "source": {"id": contract_risk.id,
                   "status": svc.repo.get_risk(contract_risk.id).status,
                   "merged_into": svc.repo.get_risk(contract_risk.id).merged_into},
        "付款阻断来源": [{"action": b["action"], "origin": b["origin_risk_ids"]}
                      for b in merged_status["blockers"]],
    })

    # 6) 缓解措施落实后复评（分数下降），再经人工降级解除全部限制，
    #    恢复记录回答"解除后恢复了哪些流程"。
    m = svc.add_mitigation(main_risk.id, actor="李工",
                           description="启用备用实习企业并完成学生转置",
                           owner="李工", due_date="2026-11-10")
    svc.confirm_mitigation(main_risk.id, m.id, actor="李工")
    after_mitig = svc.repo.get_risk(main_risk.id)
    svc.downgrade_risk(main_risk.id, "低", actor="王审批",
                       reason="备用企业承接，付款与里程碑恢复")
    final = svc.repo.get_risk(main_risk.id)
    restored = {f.key: f.restoration_log for f in svc.repo.list_flows()
                if f.restoration_log}
    show("阶段6 缓解、降级、限制解除与流程恢复", {
        "规则复评等级": after_mitig.rating.level, "规则复评分数": after_mitig.rating.score,
        "人工降级后当前等级": final.current_level,
        "恢复的流程": {k: [{"restriction": x["restriction_id"],
                         "at": x["at"], "reason": x["reason"]} for x in log]
                     for k, log in restored.items()},
    })

    # 7) 复发 → 复开；推进时钟触发定时复查。
    svc.reopen_risk(main_risk.id, actor="项目秘书处",
                    reason="备用企业也出现缩减岗位苗头")
    clock.advance(to="2026-11-20")
    results = svc.run_due_reviews()
    show("阶段7 复开与可控时间的定时复查", {
        "reopen_count": svc.repo.get_risk(main_risk.id).reopen_count,
        "到期复查结果": results,
        "决定链": [{"seq": d["seq"], "type": d["type"], "actor": d["actor"],
                  "reason": d["reason"], "ruleset": d["ruleset_version"]}
                 for d in svc.decision_chain(main_risk.id)],
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
