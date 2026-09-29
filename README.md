# 合作项目风险登记

合作项目风险台账服务端。解决“协议在法务、课程在教务、企业风险在企业合作部”导致的信息割裂：
风控人员把**风险来源、影响对象、责任人、缓解措施、限制动作、复查日期**六要素登记进同一本台账，
依据**有版本的规则**评定等级，把生效限制**传播到招生、付款、里程碑**三类流程；
风险合并、降级、例外批准、复开全程留下**哈希链决定链**；定时复查使用**可控时钟**；
查询可解释限制为何生效、解除后恢复了哪些流程。

## 目录

- `domain/contract.json`：领域角色、状态、六要素、动作、流程、等级、事件与门禁结论的权威契约。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/risk_service/`：风险登记服务端（仅依赖 Python 3.11 标准库）。
  - `clock.py`：时钟抽象（系统 / 固定 / 可调），定时复查确定性可复现。
  - `rules.py`：版本化评级规则（声明式条件，无外部表达式），评级快照命中规则与版本。
  - `store.py`：SQLite 持久化；事件追加日志带 SHA-256 哈希链，读模型同事务投影。
  - `service.py`：评级传播、合并、降级、例外、复查、关闭/复开、门禁解释。
  - `http_app.py` / `__main__.py`：标准库 JSON HTTP 接口。
  - `bootstrap.py`：“企业停供实习岗位、付款仍推进”的端到端演示剧情。
- `tools/check_contract.py`：契约摘要检查；`tools/init_db.py`：建库与演示播种。
- `tests/`：契约、领域服务、HTTP 接口、契约一致性回归测试。

## 验证

```bash
python3 -m unittest discover -s tests -v     # 全部回归测试
python3 -m compileall -q src tools tests     # 编译检查
python3 tools/check_contract.py domain/contract.json
```

## 运行演示

```bash
# 建库并播种完整剧情（多部门登记 → 合并 → 严重评级 → 例外 → 到期 → 降级恢复 → 关闭 → 复开）
python3 tools/init_db.py --db data/demo.sqlite3 --demo

# 以可控时钟启动，便于演示定时复查
PYTHONPATH=src python3 -m risk_service --db data/demo.sqlite3 --controllable-clock --port 8080
```

常用查询：

```bash
# 某流程当前是否放行，以及每条限制为何生效 / 是否被例外豁免
curl -s "http://127.0.0.1:8080/gate/%E4%BB%98%E6%AC%BE"            # /gate/付款
# 单条限制的完整因果：评级快照、批准、解除、恢复了哪些流程
curl -s http://127.0.0.1:8080/restrictions/1/explain
# 可控时间推进后跑定时复查（例外到期自动失效、到期风险发出复查提醒，同周期幂等）
curl -s -X POST http://127.0.0.1:8080/clock   -H 'Content-Type: application/json' \
  -d '{"now":"2026-10-21T09:00:00+00:00"}'
curl -s -X POST http://127.0.0.1:8080/reviews/run-due -d '{}'
# 审计：重放事件校验哈希链
curl -s http://127.0.0.1:8080/audit/verify-chain
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/rulesets` | 发布版本化规则集（版本递增、生效时间不回退） |
| GET | `/rulesets/latest` | 当前时间已生效的规则集 |
| POST | `/risks` | 登记风险（六要素；可带预案动作、事实即评级） |
| GET | `/risks` `/risks/{id}` | 风险清单 / 详情（限制、当前例外、流程恢复记录） |
| GET | `/risks/{id}/history` | 决定链（原始事件序列） |
| POST | `/risks/{id}/rate` | 提交事实按版本规则评级，自动新增/解除限制 |
| POST | `/risks/{id}/restrictions` | 手工加严限制（可指定具体对象 `target_ref`） |
| POST | `/risks/{id}/release` | 手工解除限制并记录流程恢复情况 |
| POST | `/risks/{id}/exceptions` | 例外批准（可设 `valid_until`，到期自动失效） |
| POST | `/exceptions/{id}/revoke` | 撤销例外 |
| POST | `/merges` | 风险合并，源风险生效限制去重带入目标风险 |
| POST | `/reviews/run-due` | 时钟推进：例外失效 + 到期复查提醒（幂等） |
| POST | `/risks/{id}/reviews` | 复查结论 CONFIRMED/ADJUSTED/RESOLVED |
| POST | `/risks/{id}/close` `/reopen` | 关闭（解除全部限制）/ 复开（按当前规则重新传播） |
| GET | `/gate/{flow}?target_ref=` | 门禁结论 ALLOWED / ALLOWED_WITH_EXCEPTION / BLOCKED 及原因 |
| GET | `/restrictions/{id}/explain` | 限制因果解释 + 解除后恢复的流程 |
| GET/POST | `/clock` | 可控时钟读取/设置（仅 `--controllable-clock`） |
| GET | `/audit/verify-chain` | 事件哈希链完整性校验 |

## 关键语义

- **评级快照**：每次评级记录规则版本、命中规则、事实快照与要求动作；等级下降发出降级事件。
- **限制来源**：`RULE`（规则触发）、`PLAN`（登记预案）、`MERGE`（合并带入）跟随评级自动增删；
  `MANUAL`（手工加严）是显式决定，降级时保留，须手工解除。
- **门禁与例外**：任一开放风险存在生效且无有效例外的限制即 BLOCKED；
  全部被例外豁免时 ALLOWED_WITH_EXCEPTION；无任何限制时 ALLOWED。
- **恢复可追溯**：解除限制时按流程检查是否还存在其他阻断，发出 FLOW_RESTORED 事件记录恢复/仍受阻。
- **决定链**：所有变更只追加事件（seq 全局有序、前一条哈希入链），可整体重放校验，篡改即可发现。
