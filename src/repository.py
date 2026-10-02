"""SQLite 表结构与事务访问。

对账相关表：
- plans / receivables：每期应收只挂在一个有效方案版本上（per-record active唯一索引）；
- plan_changes：经办提交、另一人复核的双人控制变更单；
- receipt_inbox：银行回执收件箱（去重与重放入口，同一bank_serial保留全部送达）；
- settlements：核销流水，bank_serial与receivable_id各自唯一，保证同一流水只核销一笔应收；
- reconciliation_discrepancies：待核差异（重复/乱序/金额不符/无有效应收）；
- checkpoints：按记录维度推进的处理检查点，失败后可断点重放。
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from .domain import Conflict, NotFound
from .recon import PAYABLE, SETTLE, SETTLED, VOID, classify_receipt, monthly_schedule


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    idempotency_key TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    program_type TEXT NOT NULL,
                    periods INTEGER NOT NULL,
                    installment_amount REAL NOT NULL,
                    first_due_date TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    change_id INTEGER,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receivables (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    period_no INTEGER NOT NULL,
                    due_date TEXT NOT NULL,
                    amount_due REAL NOT NULL,
                    paid_amount REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'payable',
                    settled_at TEXT
                );
                CREATE TABLE IF NOT EXISTS plan_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    periods INTEGER NOT NULL,
                    installment_amount REAL NOT NULL,
                    program_type TEXT NOT NULL,
                    first_due_date TEXT,
                    reason TEXT NOT NULL,
                    submitted_by TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    reviewed_by TEXT,
                    reviewed_at TEXT,
                    review_note TEXT,
                    superseded_plan_id INTEGER,
                    new_plan_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS receipt_inbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    bank_serial TEXT NOT NULL,
                    amount REAL NOT NULL,
                    period_no INTEGER,
                    received_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'received',
                    received_by TEXT NOT NULL,
                    processed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS settlements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    receivable_id INTEGER NOT NULL REFERENCES receivables(id),
                    receipt_id INTEGER NOT NULL REFERENCES receipt_inbox(id),
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    bank_serial TEXT NOT NULL,
                    amount REAL NOT NULL,
                    settled_at TEXT NOT NULL,
                    UNIQUE(bank_serial),
                    UNIQUE(receivable_id),
                    UNIQUE(receipt_id)
                );
                CREATE TABLE IF NOT EXISTS reconciliation_discrepancies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    receipt_id INTEGER NOT NULL UNIQUE REFERENCES receipt_inbox(id),
                    difference_type TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    expected_amount REAL,
                    status TEXT NOT NULL DEFAULT 'open',
                    resolution TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_by TEXT,
                    resolved_at TEXT
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    name TEXT NOT NULL,
                    stream_id INTEGER NOT NULL,
                    last_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(name, stream_id)
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_plans_record ON plans(record_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_plans_one_active
                    ON plans(record_id) WHERE status='active';
                CREATE INDEX IF NOT EXISTS idx_receivables_record ON receivables(record_id, period_no);
                CREATE INDEX IF NOT EXISTS idx_changes_record ON plan_changes(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_inbox_status ON receipt_inbox(status, id);
                CREATE INDEX IF NOT EXISTS idx_inbox_record ON receipt_inbox(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_discrep_status ON reconciliation_discrepancies(status, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_idempotent
                    ON audit_events(idempotency_key) WHERE idempotency_key IS NOT NULL;
                """
            )
            # 兼容旧库：早期audit_events没有幂等键列
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(audit_events)")}
            if "idempotency_key" not in columns:
                connection.execute("ALTER TABLE audit_events ADD COLUMN idempotency_key TEXT")

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _audit_once(
        connection: sqlite3.Connection,
        record_id: int,
        action: str,
        actor_id: str,
        version: int,
        details: Dict[str, Any],
        idempotency_key: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> bool:
        """写审计；带幂等键时同一业务事件重放不会产生第二条审计。"""
        if idempotency_key is None:
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), None, created_at or _now()),
            )
            return True
        cursor = connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?)",
            (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), idempotency_key, created_at or _now()),
        )
        return cursor.rowcount > 0

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                self._audit_once(connection, record_id, "created", actor_id, 1, {"state": state}, created_at=now)
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any], idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            self._audit_once(connection, record_id, action, actor_id, version, details, idempotency_key=idempotency_key, created_at=now)
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    # ---- 方案与应收 ----------------------------------------------------

    def activate_with_plan(
        self,
        record_id: int,
        expected_version: int,
        payload: Dict[str, Any],
        actor_id: str,
        terms: Dict[str, Any],
        first_due_date: str,
        details: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """方案生效与首期应收计划在同一事务内生成（激活即产生唯一有效方案）。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version,state FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            if str(row["state"]) != "approved":
                connection.rollback()
                raise Conflict("当前状态不允许执行activate")
            existing = connection.execute("SELECT id FROM plans WHERE record_id=? AND status='active'", (record_id,)).fetchone()
            if existing is not None:
                connection.rollback()
                raise Conflict("该记录已存在有效方案")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state='active',version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            plan_cursor = connection.execute(
                "INSERT INTO plans(record_id,version,status,program_type,periods,installment_amount,first_due_date,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (record_id, 1, "active", terms["program_type"], int(terms["periods"]), float(terms["installment_amount"]), first_due_date, actor_id, now),
            )
            plan_id = int(plan_cursor.lastrowid)
            schedule = monthly_schedule(first_due_date, int(terms["periods"]), float(terms["installment_amount"]))
            connection.executemany(
                "INSERT INTO receivables(plan_id,record_id,period_no,due_date,amount_due,status) VALUES(?,?,?,?,?,?)",
                [(plan_id, record_id, item["period_no"], item["due_date"], item["amount_due"], PAYABLE) for item in schedule],
            )
            plan_details = dict(details)
            plan_details["plan"] = {
                "plan_id": plan_id,
                "periods": int(terms["periods"]),
                "installment_amount": round(float(terms["installment_amount"]), 2),
                "first_due_date": first_due_date,
            }
            self._audit_once(connection, record_id, "activate", actor_id, version, plan_details, created_at=now)
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            plan = connection.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
            connection.commit()
        return self._row(result), dict(plan)

    def get_active_plan(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM plans WHERE record_id=? AND status='active'", (record_id,)).fetchone()
        return dict(row) if row else None

    def list_plans(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM plans WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [dict(row) for row in rows]

    def list_receivables(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM receivables WHERE record_id=? ORDER BY period_no", (record_id,)).fetchall()
        return [dict(row) for row in rows]

    # ---- 方案变更双人复核 ----------------------------------------------

    def create_plan_change(self, record_id: int, change: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = connection.execute("SELECT state FROM records WHERE id=?", (record_id,)).fetchone()
            if record is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if str(record["state"]) != "active":
                connection.rollback()
                raise Conflict("只有生效中的纾困方案可以提交变更")
            pending = connection.execute(
                "SELECT id FROM plan_changes WHERE record_id=? AND status='pending'", (record_id,)
            ).fetchone()
            if pending is not None:
                connection.rollback()
                raise Conflict("已有待复核的方案变更，请等待复核结论")
            cursor = connection.execute(
                "INSERT INTO plan_changes(record_id,status,periods,installment_amount,program_type,first_due_date,reason,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (record_id, "pending", int(change["periods"]), float(change["installment_amount"]), change["program_type"], change.get("first_due_date"), change["reason"], actor_id, now),
            )
            change_id = int(cursor.lastrowid)
            version = int(connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()["version"])
            self._audit_once(
                connection, record_id, "plan_change_submitted", actor_id, version,
                {"change_id": change_id, "periods": int(change["periods"]), "installment_amount": round(float(change["installment_amount"]), 2), "reason": change["reason"], "note": "确认前旧计划继续收款"},
                idempotency_key="plan-change-submit:%s" % change_id, created_at=now,
            )
            row = connection.execute("SELECT * FROM plan_changes WHERE id=?", (change_id,)).fetchone()
            connection.commit()
        return dict(row)

    def get_plan_change(self, change_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM plan_changes WHERE id=?", (change_id,)).fetchone()
        if row is None:
            raise NotFound("方案变更单不存在")
        return dict(row)

    def list_plan_changes(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM plan_changes WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [dict(row) for row in rows]

    def before_plan_change_commit(self, connection: sqlite3.Connection) -> None:
        """测试故障注入点：默认空操作。"""
        return None

    def confirm_plan_change(self, change_id: int, actor_id: str, review_note: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """复核通过：旧方案停用、未核销应收失效、按新要素重算，单事务原子提交。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            change_row = connection.execute("SELECT * FROM plan_changes WHERE id=?", (change_id,)).fetchone()
            if change_row is None:
                connection.rollback()
                raise NotFound("方案变更单不存在")
            change = dict(change_row)
            if change["status"] != "pending":
                connection.rollback()
                raise Conflict("方案变更单已复核，不能重复确认")
            record_id = int(change["record_id"])
            record = connection.execute("SELECT state,version FROM records WHERE id=?", (record_id,)).fetchone()
            if record is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if str(record["state"]) != "active":
                connection.rollback()
                raise Conflict("记录已不在生效状态，不能确认变更")
            old_plan = connection.execute("SELECT * FROM plans WHERE record_id=? AND status='active'", (record_id,)).fetchone()
            if old_plan is None:
                connection.rollback()
                raise Conflict("缺少有效方案，无法确认变更")
            old_plan_id = int(old_plan["id"])
            first_due_date = change["first_due_date"] or now[:10]
            next_version = int(connection.execute("SELECT COALESCE(MAX(version),0)+1 AS v FROM plans WHERE record_id=?", (record_id,)).fetchone()["v"])
            # 先停用旧方案，再插入新的active方案，满足“每记录至多一个有效方案”
            connection.execute("UPDATE plans SET status='superseded' WHERE id=?", (old_plan_id,))
            plan_cursor = connection.execute(
                "INSERT INTO plans(record_id,version,status,program_type,periods,installment_amount,first_due_date,created_by,change_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (record_id, next_version, "active", change["program_type"], int(change["periods"]), float(change["installment_amount"]), first_due_date, actor_id, change_id, now),
            )
            new_plan_id = int(plan_cursor.lastrowid)
            schedule = monthly_schedule(first_due_date, int(change["periods"]), float(change["installment_amount"]))
            connection.executemany(
                "INSERT INTO receivables(plan_id,record_id,period_no,due_date,amount_due,status) VALUES(?,?,?,?,?,?)",
                [(new_plan_id, record_id, item["period_no"], item["due_date"], item["amount_due"], PAYABLE) for item in schedule],
            )
            voided = connection.execute(
                "UPDATE receivables SET status=? WHERE plan_id=? AND status=?",
                (VOID, old_plan_id, PAYABLE),
            ).rowcount
            connection.execute(
                "UPDATE plan_changes SET status='confirmed', reviewed_by=?, reviewed_at=?, review_note=?, superseded_plan_id=?, new_plan_id=? WHERE id=?",
                (actor_id, now, review_note, old_plan_id, new_plan_id, change_id),
            )
            version = int(record["version"])
            self._audit_once(
                connection, record_id, "plan_change_confirmed", actor_id, version,
                {"change_id": change_id, "superseded_plan_id": old_plan_id, "new_plan_id": new_plan_id, "voided_receivables": int(voided), "periods": int(change["periods"]), "installment_amount": round(float(change["installment_amount"]), 2), "first_due_date": first_due_date, "note": "未核销应收已失效并按新方案重算"},
                idempotency_key="plan-change-confirm:%s" % change_id, created_at=now,
            )
            self.before_plan_change_commit(connection)
            result_change = connection.execute("SELECT * FROM plan_changes WHERE id=?", (change_id,)).fetchone()
            new_plan = connection.execute("SELECT * FROM plans WHERE id=?", (new_plan_id,)).fetchone()
            connection.commit()
        return dict(result_change), dict(new_plan)

    def reject_plan_change(self, change_id: int, actor_id: str, review_note: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            change_row = connection.execute("SELECT * FROM plan_changes WHERE id=?", (change_id,)).fetchone()
            if change_row is None:
                connection.rollback()
                raise NotFound("方案变更单不存在")
            if change_row["status"] != "pending":
                connection.rollback()
                raise Conflict("方案变更单已复核，不能重复驳回")
            record_id = int(change_row["record_id"])
            version_row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if version_row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            connection.execute(
                "UPDATE plan_changes SET status='rejected', reviewed_by=?, reviewed_at=?, review_note=? WHERE id=?",
                (actor_id, now, review_note, change_id),
            )
            self._audit_once(
                connection, record_id, "plan_change_rejected", actor_id, int(version_row["version"]),
                {"change_id": change_id, "review_note": review_note, "note": "复核驳回，旧计划继续收款"},
                idempotency_key="plan-change-reject:%s" % change_id, created_at=now,
            )
            row = connection.execute("SELECT * FROM plan_changes WHERE id=?", (change_id,)).fetchone()
            connection.commit()
        return dict(row)

    # ---- 回执与对账 ----------------------------------------------------

    def insert_receipt(self, record_id: int, receipt: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if record is None:
                connection.rollback()
                raise NotFound("记录不存在")
            cursor = connection.execute(
                "INSERT INTO receipt_inbox(record_id,bank_serial,amount,period_no,received_at,status,received_by) VALUES(?,?,?,?,?,?,?)",
                (record_id, receipt["bank_serial"], float(receipt["amount"]), receipt.get("period_no"), receipt["received_at"], "received", actor_id),
            )
            receipt_id = int(cursor.lastrowid)
            self._audit_once(
                connection, record_id, "receipt_received", actor_id, int(record["version"]),
                {"receipt_id": receipt_id, "bank_serial": receipt["bank_serial"], "amount": round(float(receipt["amount"]), 2), "period_no": receipt.get("period_no"), "received_at": receipt["received_at"], "note": "回执进入收件箱，核销结果以对账处理为准"},
                idempotency_key="receipt-received:%s" % receipt_id, created_at=now,
            )
            row = connection.execute("SELECT * FROM receipt_inbox WHERE id=?", (receipt_id,)).fetchone()
            connection.commit()
        return dict(row)

    def list_receipts(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM receipt_inbox WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [dict(row) for row in rows]

    def list_discrepancies(self, status: Optional[str] = None, record_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = ("SELECT d.*, i.bank_serial, i.amount AS receipt_amount, i.period_no AS requested_period, i.received_at "
               "FROM reconciliation_discrepancies d JOIN receipt_inbox i ON i.id=d.receipt_id")
        clauses = []
        params: List[Any] = []
        if status:
            clauses.append("d.status=?")
            params.append(status)
        if record_id is not None:
            clauses.append("d.record_id=?")
            params.append(record_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY d.id"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def pending_receipt_record_ids(self) -> List[int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT record_id FROM receipt_inbox WHERE status='received' ORDER BY record_id"
            ).fetchall()
        return [int(row["record_id"]) for row in rows]

    def before_receipt_commit(self, connection: sqlite3.Connection, record_id: int, receipt_id: int) -> None:
        """测试故障注入点：默认空操作。"""
        return None

    def process_receipt_queue(self) -> Dict[str, int]:
        """按检查点处理所有已收件未处理回执；失败重放只续做未提交的部分。"""
        totals = {"records": 0, "processed": 0, "settled": 0, "differences": 0}
        for record_id in self.pending_receipt_record_ids():
            result = self._drain_record(record_id)
            totals["records"] += 1
            for key in ("processed", "settled", "differences"):
                totals[key] += result[key]
        return totals

    def _drain_record(self, record_id: int) -> Dict[str, int]:
        processed = settled = differences = 0
        while True:
            outcome = self._process_next_receipt(record_id)
            if outcome is None:
                break
            processed += 1
            settled += outcome.get("settled", 0)
            differences += outcome.get("differences", 0)
        return {"processed": processed, "settled": settled, "differences": differences}

    def _process_next_receipt(self, record_id: int) -> Optional[Dict[str, int]]:
        """单记录单事务：取检查点之后最早一笔received回执并分类落账。

        检查点只用于“首达回执”的断点续跑；人工复核重新打开的回执id早于检查点，
        通过`processed_at IS NULL`分支优先捞取，避免被检查点跳过。
        """
        now = _now()
        checkpoint_name = "receipt_recon"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            checkpoint_row = connection.execute(
                "SELECT last_id FROM checkpoints WHERE name=? AND stream_id=?", (checkpoint_name, record_id)
            ).fetchone()
            last_id = int(checkpoint_row["last_id"]) if checkpoint_row else 0
            row = connection.execute(
                "SELECT * FROM receipt_inbox "
                "WHERE record_id=? AND status='received' AND (id>? OR processed_at IS NULL) "
                "ORDER BY id LIMIT 1",
                (record_id, last_id),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            receipt = dict(row)
            outcome = self._apply_receipt(connection, record_id, receipt, now)
            if int(receipt["id"]) > last_id:
                connection.execute(
                    "INSERT INTO checkpoints(name,stream_id,last_id,updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(name,stream_id) DO UPDATE SET last_id=excluded.last_id, updated_at=excluded.updated_at",
                    (checkpoint_name, record_id, int(receipt["id"]), now),
                )
            self.before_receipt_commit(connection, record_id, int(receipt["id"]))
            connection.commit()
        return outcome

    def _used_serials(self, connection: sqlite3.Connection) -> Set[str]:
        rows = connection.execute("SELECT bank_serial FROM settlements").fetchall()
        return {str(row["bank_serial"]) for row in rows}

    def _apply_receipt(self, connection: sqlite3.Connection, record_id: int, receipt: Dict[str, Any], now: str) -> Dict[str, int]:
        """对单条回执执行分类、核销或挂差异；全部与检查点同事务。"""
        receipt_id = int(receipt["id"])
        active_plan = connection.execute(
            "SELECT * FROM plans WHERE record_id=? AND status='active'", (record_id,)
        ).fetchone()
        if active_plan is not None:
            # 每期应收只对应当前有效方案：核销判定只在新方案账期内进行，
            # 旧方案已核销/已失效账期不参与匹配
            receivable_rows = connection.execute(
                "SELECT * FROM receivables WHERE record_id=? AND plan_id=? ORDER BY period_no",
                (record_id, int(active_plan["id"])),
            ).fetchall()
        else:
            receivable_rows = []
        receivables = [dict(item) for item in receivable_rows]
        prior = connection.execute(
            "SELECT bank_serial FROM receipt_inbox WHERE record_id=? AND id<? AND status='applied'",
            (record_id, receipt_id),
        ).fetchall()
        used_serials = self._used_serials(connection) | {str(row["bank_serial"]) for row in prior}
        decision = classify_receipt(receipt, dict(active_plan) if active_plan else None, receivables, used_serials)
        version = int(connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()["version"])
        bank_serial = str(receipt["bank_serial"])
        amount = round(float(receipt["amount"]), 2)

        if decision.kind == SETTLE:
            receivable_id = int(decision.receivable_id)
            connection.execute(
                "INSERT INTO settlements(receivable_id,receipt_id,record_id,bank_serial,amount,settled_at) VALUES(?,?,?,?,?,?)",
                (receivable_id, receipt_id, record_id, bank_serial, amount, now),
            )
            connection.execute(
                "UPDATE receivables SET status=?, paid_amount=?, settled_at=? WHERE id=?",
                (SETTLED, amount, now, receivable_id),
            )
            connection.execute("UPDATE receipt_inbox SET status='applied', processed_at=? WHERE id=?", (now, receipt_id))
            self._audit_once(
                connection, record_id, "receipt_settled", "system", version,
                {"receipt_id": receipt_id, "bank_serial": bank_serial, "amount": amount, "receivable_id": receivable_id, "period_no": next(int(r["period_no"]) for r in receivables if int(r["id"]) == receivable_id), "summary": decision.reason},
                idempotency_key="receipt-settle:%s" % receipt_id, created_at=now,
            )
            return {"settled": 1, "differences": 0}

        # 任何异常分类都先进待核差异，不核销任何应收
        connection.execute("UPDATE receipt_inbox SET status='difference', processed_at=? WHERE id=?", (now, receipt_id))
        connection.execute(
            "INSERT INTO reconciliation_discrepancies(record_id,receipt_id,difference_type,reason,expected_amount,status,created_by,created_at) "
            "VALUES(?,?,?,?,?,'open','system',?) ON CONFLICT(receipt_id) DO UPDATE SET "
            "difference_type=excluded.difference_type, reason=excluded.reason, expected_amount=excluded.expected_amount, "
            "status='open', resolution=NULL, resolved_by=NULL, resolved_at=NULL, created_at=excluded.created_at",
            (record_id, receipt_id, decision.difference_type, decision.reason, decision.expected_amount, now),
        )
        self._audit_once(
            connection, record_id, "receipt_difference", "system", version,
            {"receipt_id": receipt_id, "bank_serial": bank_serial, "amount": amount, "difference_type": decision.difference_type, "reason": decision.reason, "expected_amount": decision.expected_amount, "note": "重复/乱序/金额不符先挂待核差异，不修改履约状态"},
            idempotency_key="receipt-difference:%s" % receipt_id, created_at=now,
        )
        return {"settled": 0, "differences": 1}

    def recheck_discrepancy(self, discrepancy_id: int, actor_id: str) -> Dict[str, Any]:
        """人工复核后重新分类（可核销则核销），全程幂等。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            discrepancy_row = connection.execute(
                "SELECT * FROM reconciliation_discrepancies WHERE id=?", (discrepancy_id,)
            ).fetchone()
            if discrepancy_row is None:
                connection.rollback()
                raise NotFound("待核差异不存在")
            discrepancy = dict(discrepancy_row)
            if discrepancy["status"] != "open":
                connection.rollback()
                raise Conflict("待核差异已处理")
            receipt_row = connection.execute("SELECT * FROM receipt_inbox WHERE id=?", (int(discrepancy["receipt_id"]),)).fetchone()
            if receipt_row is None:
                connection.rollback()
                raise NotFound("回执不存在")
            record_id = int(discrepancy["record_id"])
            receipt = dict(receipt_row)
            connection.execute("UPDATE receipt_inbox SET status='received', processed_at=NULL WHERE id=?", (receipt["id"],))
            connection.execute("DELETE FROM reconciliation_discrepancies WHERE id=?", (discrepancy_id,))
            # 复核是对同一笔回执的重新分类：撤掉上一版分类结论（含其幂等审计），
            # 保留receipt_received原始事件；若重放结论不变会重新落同一结论。
            connection.execute(
                "DELETE FROM audit_events WHERE idempotency_key IN (?, ?)",
                ("receipt-settle:%s" % receipt["id"], "receipt-difference:%s" % receipt["id"]),
            )
            outcome = self._apply_receipt(connection, record_id, receipt, now)
            version = int(connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()["version"])
            self._audit_once(
                connection, record_id, "discrepancy_rechecked", actor_id, version,
                {"discrepancy_id": discrepancy_id, "receipt_id": int(receipt["id"]), "rechecked_by": actor_id, "settled": outcome.get("settled", 0), "differences": outcome.get("differences", 0)},
                idempotency_key="discrepancy-recheck:%s:%s" % (discrepancy_id, actor_id), created_at=now,
            )
            result = connection.execute(
                "SELECT d.*, i.bank_serial, i.amount AS receipt_amount, i.period_no AS requested_period, i.received_at "
                "FROM reconciliation_discrepancies d JOIN receipt_inbox i ON i.id=d.receipt_id WHERE d.receipt_id=?",
                (int(receipt["id"]),),
            ).fetchone()
            connection.commit()
        if result is not None:
            return dict(result)
        return {"id": discrepancy_id, "receipt_id": int(receipt["id"]), "status": "settled", "settled": True}

    def ignore_discrepancy(self, discrepancy_id: int, actor_id: str, note: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            discrepancy_row = connection.execute(
                "SELECT * FROM reconciliation_discrepancies WHERE id=?", (discrepancy_id,)
            ).fetchone()
            if discrepancy_row is None:
                connection.rollback()
                raise NotFound("待核差异不存在")
            if discrepancy_row["status"] != "open":
                connection.rollback()
                raise Conflict("待核差异已处理")
            record_id = int(discrepancy_row["record_id"])
            receipt_id = int(discrepancy_row["receipt_id"])
            connection.execute(
                "UPDATE reconciliation_discrepancies SET status='ignored', resolution=?, resolved_by=?, resolved_at=? WHERE id=?",
                (note, actor_id, now, discrepancy_id),
            )
            connection.execute("UPDATE receipt_inbox SET status='ignored', processed_at=? WHERE id=?", (now, receipt_id))
            version_row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            self._audit_once(
                connection, record_id, "discrepancy_ignored", actor_id, int(version_row["version"]),
                {"discrepancy_id": discrepancy_id, "receipt_id": receipt_id, "resolution": note},
                idempotency_key="discrepancy-ignore:%s" % discrepancy_id, created_at=now,
            )
            row = connection.execute("SELECT * FROM reconciliation_discrepancies WHERE id=?", (discrepancy_id,)).fetchone()
            connection.commit()
        return dict(row)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            self._audit_once(connection, record_id, action, actor_id, int(row["version"]), details)
            connection.commit()

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
