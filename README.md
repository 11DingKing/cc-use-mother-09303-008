# 合作项目风险登记服务端

面向合作项目的风险登记与控制传播服务。它把分散在**协议、课程、企业风险**等
不同部门的事实连接为一条完整链路：

> 风险来源 → 影响对象（招生/付款/里程碑）→ 责任人 → 缓解措施 →
> 限制动作 → 复查日期

并支持：按**有版本的规则**评定风险等级；把生效限制**传播**到招生、付款、
里程碑；风险**合并、降级、例外批准、复开**全部留下不可变**决定链**；
**定时复查**使用可控时钟；查询可**解释**限制为何生效、解除后恢复了哪些流程。

场景背景：某合作企业突然停止提供实习岗位，但付款节点仍按原计划推进。
`python3 tools/demo_scenario.py` 可端到端复现该场景。

## 分层结构

| 模块 | 职责 |
| --- | --- |
| `domain/contract.json` | 领域角色、状态、不变量契约 |
| `src/domain_contract/` | 契约读取与确定性校验（既有） |
| `src/risk_register/models.py` | 领域值对象：风险、来源、影响对象、责任人、缓解措施、限制、决定 |
| `src/risk_register/rules.py` | **有版本**的规则集与纯函数评定引擎（1.0.0 / 1.1.0 快照并存） |
| `src/risk_register/clock.py` | 时钟抽象：`SystemClock` 与测试用 `FixedClock` |
| `src/risk_register/store.py` | 仓储：内存实现 + JSON 快照持久化 |
| `src/risk_register/service.py` | 应用服务：评定、传播、例外、合并、降级、复开、复查、解释 |
| `src/risk_register/httpapi.py` | 标准库 HTTP 接口（零第三方依赖） |
| `src/risk_register/server.py` | 启动入口 |
| `tools/demo_scenario.py` | 完整业务场景演示 |
| `tests/` | 契约、服务层、HTTP 端到端回归测试 |

## 关键设计

- **版本化评定**：规则集是带版本号的不可变快照，只能追加。评定结果持久化
  `ruleset_version` 与命中规则，历史等级永远按当时规则解释；复评默认用最新版。
  - 1.0.0：因子计分 + 付款冻结/里程碑暂挂/招生预警。
  - 1.1.0：新增"高/极高等级强制**阻断**付款"等级动作，缓解措施落实减 2 分。
- **限制传播对账**：每次评定把引擎输出的限制规格与现存生效限制对账——
  新增缺失的、保持仍命中的、解除不再要求的，重复评定不会产生重复限制。
  限制的施加/解除与下游流程的挂起/恢复在同一事务内完成。
- **例外批准**：对生效限制批准带有效期的例外；到期后限制**自动重新生效**，
  无需新决定。阻断不因冻结被例外放行而失效。
- **合并**：源风险置"已合并"，生效限制由目标风险承袭（旧限制标记"已承袭"，
  按动作+范围去重后重新施加），`origin_risk_ids` 保留完整来源链。
- **降级**：规则评定结果不可变，人工降级以 `level_override` 形成当前等级；
  可选择同步解除限制并恢复流程；下一次规则评定自动取消覆盖。
- **决定链**：登记、评定、施加/解除限制、例外、合并、降级、缓解落实、复查、
  复开、关闭都追加带全局序号、前后快照、规则引用的不可变记录。
- **定时复查**：`next_review_date` 到期自动复评并顺延；时钟可注入，
  测试与演示用 `FixedClock` 完全可控（环境变量 `RISK_TODAY`）。
- **可解释查询**：流程状态接口返回每条阻断/冻结/预警来自哪条规则、哪个版本、
  哪个风险、是否处于例外期；解除记录回答"恢复了哪些流程"。

## HTTP 接口

| 方法与路径 | 说明 |
| --- | --- |
| `POST /risks` | 登记风险（来源、影响对象、责任人、复查间隔） |
| `GET /risks` / `GET /risks/{id}` | 查询风险 |
| `POST /risks/{id}/rate` | 评定等级（可带 `ruleset_version`） |
| `POST /risks/{id}/mitigations` | 登记缓解措施 |
| `POST /risks/{id}/mitigations/{mid}/confirm` | 落实缓解措施并复评 |
| `POST /risks/{id}/downgrade` | 人工降级（可同时解除限制） |
| `POST /risks/{id}/reopen` / `/close` | 复开 / 关闭 |
| `POST /merges` | 合并风险（`source_ids` → `target_id`） |
| `POST /restrictions/{id}/exemptions` | 限期例外批准 |
| `GET /restrictions/{id}/explain` | 解释一条限制为何生效 |
| `GET /risks/{id}/decisions` | 决定链 |
| `GET /flows` / `GET /flows/{key}/status` | 流程放行状态、阻断来源、恢复记录 |
| `GET /reviews/due` / `POST /reviews/run` | 到期复查查询 / 执行 |
| `GET /rules` / `GET /rules/{version}` | 规则版本与条文 |

流程键形如 `付款节点:P2`、`招生批次:E2026-FALL`、`里程碑:M3`（路径中文需
百分号编码）。

## 运行

```bash
# 场景演示（无需起服务）
python3 tools/demo_scenario.py

# 启动 HTTP 服务（可选 JSON 快照持久化与固定时钟）
RISK_STATE=./data/state.json RISK_TODAY=2026-09-30 \
  PYTHONPATH=src python3 -m risk_register.server --port 8080

# 指定规则版本评定
curl -X POST localhost:8080/risks/RSK-0001/rate \
  -H 'Content-Type: application/json' \
  -d '{"actor":"王审批","ruleset_version":"1.0.0"}'
```

## 验证

```bash
python3 -m unittest discover -s tests -v          # 全部回归测试
python3 -m compileall -q src tools tests          # 编译检查
python3 tools/check_contract.py domain/contract.json
```

## 角色

- **项目秘书处**：登记风险、发起合并与复开、触发复查。
- **风险责任人**：维护缓解措施并确认落实。
- **审批人员**：等级评定、例外批准、人工降级、关闭。
