# 住房贷款纾困申请与履约跟踪

纯Python标准库实现的住房贷款纾困申请与履约跟踪原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、偿付能力、方案阈值、方案变更要素和冲突检查。
- `src/recon.py`：对账纯领域逻辑（账期生成、回执分类、履约状态推导）。
- `src/repository.py`：SQLite建表、事务、方案/应收/回执/差异/检查点查询。
- `src/service.py`：用例编排、权限检查、双人复核、检查点续跑和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败重试与对账场景测试。

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
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
  - `activate`支持可选`first_due_date`（YYYY-MM-DD），生效即生成唯一有效方案与每期应收。

### 纾困方案与对账接口

- `POST /api/records/{id}/plan-changes`：经办（servicer/reviewer）提交方案变更，旧计划在确认前继续收款。
- `GET  /api/records/{id}/plan-changes`：变更单列表。
- `POST /api/plan-changes/{id}/review`：复核人（reviewer角色，且不能是提交人本人）确认/驳回，请求体`{"approved":true,"data":{"review_note":"..."}}`。确认后旧方案停用、未核销应收失效并按新方案重算；驳回则旧计划不变。
- `POST /api/records/{id}/receipts`：登记银行扣款回执`{"bank_serial","amount","period_no"可选,"received_at"可选}`，入箱即自动对账。
- `POST /api/reconcile`：按检查点重新处理所有未对账回执（失败重放入口）。
- `GET  /api/records/{id}/reconciliation`：方案、每期应收、回执、待核差异与履约状态汇总。
- `GET  /api/discrepancies?status=open&record_id=...`：待核差异列表。
- `POST /api/discrepancies/{id}`：`{"action":"recheck"}`重新分类核销，或`{"action":"ignore","data":{"note":"..."}}`人工核销挂起。

### 对账规则

1. **每期应收只对应一个有效方案**：`plans`对每条记录存在部分唯一索引（仅一个`active`）。方案生效或变更确认时，应收账期与其方案版本同事务生成。
2. **同一流水只能核销一笔应收**：`settlements.bank_serial`与`receivable_id`各自唯一；重复送达的流水进入待核差异，不产生第二笔核销。
3. **重复、乱序、金额不符先挂待核差异**：重复流水、指定期号前仍有未核销应收（乱序）、金额与应收不符（容差0.005）均只写`reconciliation_discrepancies`，不核销、不改履约状态；不指定期号时按最早未核销应收（FIFO）核销。
4. **双人复核**：经办提交变更后保持`pending`，必须由另一名`reviewer`确认；确认前旧计划继续收款，确认后未核销应收置`void`并按新计划重算，已核销历史保留。
5. **可恢复**：回执按记录维度在`checkpoints`中记录已处理位置，每笔回执在独立事务内分类落账并推进检查点；中途写入失败整体回滚，重放只续做未提交部分。所有审计事件带幂等键，重放不产生重复审计。
6. **履约状态由应收推导**：`current / overdue / completed`由当前有效方案下应收的核销与到期情况计算，回执本身不直接改履约状态。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突、回执对账（重复/乱序/金额不符/FIFO）、方案双人变更与检查点失败重试。
