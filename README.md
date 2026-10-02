# 住房贷款纾困申请与履约跟踪

纯Python标准库实现的住房贷款纾困申请与履约跟踪原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、偿付能力、方案阈值和履约状态和冲突检查。
- `src/reconcile.py`：还款计划期次生成、回执匹配判定与差异分类（纯逻辑，无副作用）。
- `src/repository.py`：SQLite建表、事务和查询；方案、期次、方案变更、回执、待核差异与对账检查点。
- `src/service.py`：用例编排、权限检查、乐观并发、方案变更双人复核和可恢复对账。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景与对账（去重/乱序/金额不符/检查点恢复/审计幂等）测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8327
```

默认端口为`8327`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/records/{id}/installments`：还款计划期次（active/settled/void）。
- `GET /api/records/{id}/receipts`：扣款回执。
- `GET /api/records/{id}/plan-changes`：方案变更记录。
- `GET /api/differences?status=open`：待核差异列表。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`；`activate`时同事务生成首版还款计划。
- `POST /api/records/{id}/receipts`：登记银行回执`{"data":{"bank_serial":"...","amount":3400,"period_no":1}}`，即时对账，重复/乱序/金额不符进入待核差异。
- `POST /api/records/{id}/plan-changes`：经办（servicer）提交方案变更。
- `POST /api/plan-changes/{id}/confirm|reject`：另一名复核人（reviewer）确认或驳回；确认前旧计划继续收款，确认后未核销应收失效并重算。
- `POST /api/reconcile`：从检查点执行对账（先重判待核差异，再处理新回执），支持`{"record_id":1}`限定案件。
- `POST /api/differences/{id}/resolve`：复核人人工关闭差异（`write_off`/`ignore`）。

## 对账规则

- **每期应收只对应一个有效方案**：`plans` 对每个案件的 `active` 行有部分唯一索引；`installments` 对未失效的 `(案件, 期号)` 唯一。
- **同一流水只能核销一笔应收**：`bank_receipts.bank_serial` 全局唯一，重复流水只登记 `duplicate` 待核差异，不二次核销。
- **乱序/金额不符先挂差异**：严格按期号顺序核销；未核销期不允许跨期，金额与当期应收不符（容差 0.01）即进 `recon_differences`。后续补齐前序款项或对账重跑时，差异可自动转正核销。
- **方案变更双人复核**：经办提交后为 `pending`，复核人不能是提交人本人；确认前旧计划照常收款，确认后旧未核销应收置 `void`，新计划从首个未核销期续算。
- **可恢复**：对账每笔回执独立事务，成功后推进 `recon_checkpoints` 水位；审计事件带 `idempotency_key`（部分唯一索引），崩溃重跑不重复核销、不重复审计。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
