"""SQLite 表结构与事务访问。

对账相关表：
- plans：纾困方案，每个案件至多一个 status='active' 的方案（部分唯一索引保证）。
- installments：还款计划期次，未失效的 (record_id, period_no) 唯一。
- plan_changes：方案变更（经办提交、另一名复核人确认/驳回）。
- bank_receipts：银行扣款回执，bank_serial 全局唯一。
- recon_differences：待核差异（重复/乱序/金额不符/无生效方案）。
- recon_checkpoints：对账检查点，写入失败后从该水位继续。

audit_events.idempotency_key 与部分唯一索引保证同一业务事件的审计在重试时只落一条。
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


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
                    created_at TEXT NOT NULL,
                    idempotency_key TEXT
                );
                CREATE TABLE IF NOT EXISTS plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    plan_seq INTEGER NOT NULL,
                    program_type TEXT NOT NULL,
                    payment_amount REAL NOT NULL,
                    months INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS installments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    period_no INTEGER NOT NULL,
                    amount_due REAL NOT NULL,
                    due_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    receipt_id INTEGER REFERENCES bank_receipts(id),
                    settled_at TEXT
                );
                CREATE TABLE IF NOT EXISTS plan_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    old_plan_id INTEGER NOT NULL REFERENCES plans(id),
                    new_plan_id INTEGER REFERENCES plans(id),
                    payment_amount REAL NOT NULL,
                    months INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    submitted_by TEXT NOT NULL,
                    reviewed_by TEXT,
                    review_note TEXT,
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS bank_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    bank_serial TEXT NOT NULL UNIQUE,
                    amount REAL NOT NULL,
                    period_no INTEGER,
                    status TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recon_differences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    receipt_id INTEGER REFERENCES bank_receipts(id),
                    bank_serial TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    details TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    resolution TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE TABLE IF NOT EXISTS recon_checkpoints (
                    scope TEXT PRIMARY KEY,
                    last_receipt_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS uq_active_plan ON plans(record_id) WHERE status = 'active';
                CREATE UNIQUE INDEX IF NOT EXISTS uq_live_period
                    ON installments(record_id, period_no) WHERE status != 'void';
                CREATE INDEX IF NOT EXISTS idx_installment_record ON installments(record_id, period_no);
                CREATE INDEX IF NOT EXISTS idx_receipt_status ON bank_receipts(status, id);
                CREATE INDEX IF NOT EXISTS idx_difference_status ON recon_differences(status, id);
                """
            )
            # 兼容旧库：为已有的 audit_events 补幂等列，再建部分唯一索引（新库列已随建表生成）。
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(audit_events)")}
            if "idempotency_key" not in columns:
                connection.execute("ALTER TABLE audit_events ADD COLUMN idempotency_key TEXT")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_audit_idempotency "
                "ON audit_events(idempotency_key) WHERE idempotency_key IS NOT NULL"
            )

    @contextmanager
    def immediate(self):
        """提供 BEGIN IMMEDIATE 事务，供跨表业务操作使用。"""
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _audit(connection: sqlite3.Connection, record_id: int, action: str, actor_id: str,
               version: int, details: Dict[str, Any], idempotency_key: str = None) -> bool:
        """写审计；给定幂等键且该键已存在时跳过，返回是否真正插入。"""
        cursor = connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at,idempotency_key) "
            "VALUES(?,?,?,?,?,?,?)",
            (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True),
             _now(), idempotency_key),
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
                self._audit(connection, record_id, "created", actor_id, 1, {"state": state})
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

    def get_by_reference(self, reference: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE reference=?", (reference,)).fetchone()
        return self._row(row) if row is not None else None

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any],
               actor_id: str, action: str, details: Dict[str, Any],
               connection: sqlite3.Connection = None) -> Dict[str, Any]:
        return self._mutate(record_id, expected_version, state, payload, actor_id, action, details, connection)

    def _mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any],
                actor_id: str, action: str, details: Dict[str, Any],
                connection: sqlite3.Connection = None) -> Dict[str, Any]:
        now = _now()
        own = connection is None
        if own:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            self._audit(connection, record_id, action, actor_id, version, details)
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if own:
                connection.commit()
        except Exception:
            if own:
                connection.rollback()
            raise
        finally:
            if own:
                connection.close()
        return self._row(result)

    # ---- 方案与还款计划 -------------------------------------------------

    def activate_with_plan(self, record_id: int, expected_version: int, payload: Dict[str, Any],
                           actor_id: str, action: str, details: Dict[str, Any],
                           terms: List[Dict[str, Any]]) -> Dict[str, Any]:
        """方案生效与首版还款计划在同一事务落库：每期应收只属于一个有效方案。"""
        with self.immediate() as connection:
            record = self._mutate(record_id, expected_version, "active", payload, actor_id, action,
                                  details, connection)
            now = _now()
            cursor = connection.execute(
                "INSERT INTO plans(record_id,plan_seq,program_type,payment_amount,months,status,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (record_id, 1, payload["approved_program"], float(payload["approved_payment"]),
                 int(payload["approved_months"]), "active", actor_id, now),
            )
            plan_id = int(cursor.lastrowid)
            for term in terms:
                connection.execute(
                    "INSERT INTO installments(plan_id,record_id,period_no,amount_due,due_date,status) "
                    "VALUES(?,?,?,?,?,?)",
                    (plan_id, record_id, term["period_no"], term["amount_due"], term["due_date"], "active"),
                )
            self._audit(connection, record_id, "plan_created", actor_id, record["version"],
                        {"plan_id": plan_id, "months": len(terms),
                         "payment_amount": float(payload["approved_payment"])},
                        idempotency_key="plan_created:%s" % record_id)
        return record

    def get_active_plan(self, record_id: int, connection: sqlite3.Connection = None) -> Optional[Dict[str, Any]]:
        own = connection is None
        if own:
            connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM plans WHERE record_id=? AND status='active'", (record_id,)
            ).fetchone()
        finally:
            if own:
                connection.close()
        return dict(row) if row is not None else None

    def list_installments(self, record_id: int, connection: sqlite3.Connection = None) -> List[Dict[str, Any]]:
        own = connection is None
        if own:
            connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM installments WHERE record_id=? ORDER BY period_no, id", (record_id,)
            ).fetchall()
        finally:
            if own:
                connection.close()
        return [dict(row) for row in rows]

    def submit_plan_change(self, record_id: int, payment_amount: float, months: int, reason: str,
                           submitted_by: str) -> Dict[str, Any]:
        with self.immediate() as connection:
            plan = self.get_active_plan(record_id, connection)
            if plan is None:
                raise Conflict("案件尚无生效方案")
            pending = connection.execute(
                "SELECT id FROM plan_changes WHERE record_id=? AND status='pending'", (record_id,)
            ).fetchone()
            if pending is not None:
                raise Conflict("已有待复核的方案变更")
            now = _now()
            cursor = connection.execute(
                "INSERT INTO plan_changes(record_id,old_plan_id,payment_amount,months,reason,status,"
                "submitted_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (record_id, plan["id"], payment_amount, months, reason, "pending", submitted_by, now),
            )
            change_id = int(cursor.lastrowid)
            version = int(connection.execute("SELECT version FROM records WHERE id=?", (record_id,))
                          .fetchone()["version"])
            self._audit(connection, record_id, "plan_change_submitted", submitted_by, version,
                        {"change_id": change_id, "payment_amount": payment_amount, "months": months,
                         "reason": reason})
            row = connection.execute("SELECT * FROM plan_changes WHERE id=?", (change_id,)).fetchone()
        return dict(row)

    def get_plan_change(self, change_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM plan_changes WHERE id=?", (change_id,)).fetchone()
        if row is None:
            raise NotFound("方案变更不存在")
        return dict(row)

    def list_plan_changes(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM plan_changes WHERE record_id=? ORDER BY id", (record_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def confirm_plan_change(self, change_id: int, reviewer: str, review_note: str,
                            terms: List[Dict[str, Any]]) -> Dict[str, Any]:
        """确认方案变更：旧计划未核销应收失效，按新方案重算；旧方案在确认前继续收款。"""
        now = _now()
        with self.immediate() as connection:
            change = connection.execute("SELECT * FROM plan_changes WHERE id=?", (change_id,)).fetchone()
            if change is None:
                raise NotFound("方案变更不存在")
            change = dict(change)
            if change["status"] != "pending":
                raise Conflict("方案变更已处理")
            record_id = change["record_id"]
            # 先释放唯一有效方案位，再落新方案；旧计划未核销应收一并失效（已核销保留流水）。
            connection.execute("UPDATE plans SET status='superseded' WHERE id=?", (change["old_plan_id"],))
            connection.execute(
                "UPDATE installments SET status='void' WHERE plan_id=? AND status='active'",
                (change["old_plan_id"],),
            )
            cursor = connection.execute(
                "INSERT INTO plans(record_id,plan_seq,program_type,payment_amount,months,status,created_by,created_at) "
                "SELECT ?, plan_seq + 1, program_type, ?, ?, 'active', ?, ? FROM plans WHERE id=?",
                (record_id, change["payment_amount"], change["months"], reviewer, now, change["old_plan_id"]),
            )
            new_plan_id = int(cursor.lastrowid)
            for term in terms:
                connection.execute(
                    "INSERT INTO installments(plan_id,record_id,period_no,amount_due,due_date,status) "
                    "VALUES(?,?,?,?,?,?)",
                    (new_plan_id, record_id, term["period_no"], term["amount_due"], term["due_date"], "active"),
                )
            connection.execute(
                "UPDATE plan_changes SET status='confirmed',new_plan_id=?,reviewed_by=?,review_note=?,"
                "reviewed_at=? WHERE id=?",
                (new_plan_id, reviewer, review_note, now, change_id),
            )
            version = int(connection.execute("SELECT version FROM records WHERE id=?", (record_id,))
                          .fetchone()["version"])
            voided = connection.execute(
                "SELECT COUNT(*) AS c FROM installments WHERE plan_id=? AND status='void'",
                (change["old_plan_id"],),
            ).fetchone()["c"]
            self._audit(connection, record_id, "plan_change_confirmed", reviewer, version,
                        {"change_id": change_id, "old_plan_id": change["old_plan_id"],
                         "new_plan_id": new_plan_id, "voided_receivables": int(voided),
                         "payment_amount": change["payment_amount"]},
                        idempotency_key="plan_change_confirmed:%s" % change_id)
            row = connection.execute("SELECT * FROM plan_changes WHERE id=?", (change_id,)).fetchone()
        return dict(row)

    def reject_plan_change(self, change_id: int, reviewer: str, review_note: str) -> Dict[str, Any]:
        now = _now()
        with self.immediate() as connection:
            row = connection.execute("SELECT * FROM plan_changes WHERE id=?", (change_id,)).fetchone()
            if row is None:
                raise NotFound("方案变更不存在")
            change = dict(row)
            if change["status"] != "pending":
                raise Conflict("方案变更已处理")
            connection.execute(
                "UPDATE plan_changes SET status='rejected',reviewed_by=?,review_note=?,reviewed_at=? WHERE id=?",
                (reviewer, review_note, now, change_id),
            )
            version = int(connection.execute("SELECT version FROM records WHERE id=?", (change["record_id"],))
                          .fetchone()["version"])
            self._audit(connection, change["record_id"], "plan_change_rejected", reviewer, version,
                        {"change_id": change_id, "review_note": review_note},
                        idempotency_key="plan_change_rejected:%s" % change_id)
            result = connection.execute("SELECT * FROM plan_changes WHERE id=?", (change_id,)).fetchone()
        return dict(result)

    # ---- 扣款回执与待核差异 --------------------------------------------

    def get_receipt_by_serial(self, bank_serial: str, connection: sqlite3.Connection = None) -> Optional[Dict[str, Any]]:
        own = connection is None
        if own:
            connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM bank_receipts WHERE bank_serial=?", (bank_serial,)).fetchone()
        finally:
            if own:
                connection.close()
        return dict(row) if row is not None else None

    def insert_receipt(self, record_id: int, bank_serial: str, amount: float,
                       period_no: Optional[int], actor_id: str,
                       connection: sqlite3.Connection = None) -> Dict[str, Any]:
        now = _now()
        own = connection is None
        if own:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = connection.execute(
                "INSERT INTO bank_receipts(record_id,bank_serial,amount,period_no,status,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (record_id, bank_serial, amount, period_no, "received", actor_id, now),
            )
            receipt_id = int(cursor.lastrowid)
            version = int(connection.execute("SELECT version FROM records WHERE id=?", (record_id,))
                          .fetchone()["version"])
            self._audit(connection, record_id, "receipt_ingested", actor_id, version,
                        {"receipt_id": receipt_id, "bank_serial": bank_serial, "amount": amount,
                         "period_no": period_no},
                        idempotency_key="receipt_ingested:%s" % bank_serial)
            row = connection.execute("SELECT * FROM bank_receipts WHERE id=?", (receipt_id,)).fetchone()
            if own:
                connection.commit()
        except Exception:
            if own:
                connection.rollback()
            raise
        finally:
            if own:
                connection.close()
        return dict(row)

    def register_duplicate(self, existing: Dict[str, Any], actor_id: str,
                           connection: sqlite3.Connection = None) -> Dict[str, Any]:
        """重复流水不核销任何应收，只登记一条 duplicate 待核差异。"""
        now = _now()
        own = connection is None
        if own:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
        try:
            open_row = connection.execute(
                "SELECT id FROM recon_differences WHERE receipt_id=? AND reason='duplicate' AND status='open'",
                (existing["id"],),
            ).fetchone()
            if open_row is None:
                cursor = connection.execute(
                    "INSERT INTO recon_differences(record_id,receipt_id,bank_serial,reason,details,status,"
                    "attempts,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (existing["record_id"], existing["id"], existing["bank_serial"], "duplicate",
                     json.dumps({"message": "流水号重复，未核销应收", "existing_status": existing["status"]},
                                ensure_ascii=False, sort_keys=True),
                     "open", 0, actor_id, now),
                )
                diff_id = int(cursor.lastrowid)
                version = int(connection.execute("SELECT version FROM records WHERE id=?",
                                                 (existing["record_id"],)).fetchone()["version"])
                self._audit(connection, existing["record_id"], "difference_registered", actor_id, version,
                            {"difference_id": diff_id, "reason": "duplicate",
                             "bank_serial": existing["bank_serial"]},
                            idempotency_key="duplicate_serial:%s" % existing["bank_serial"])
            else:
                diff_id = int(open_row["id"])
            row = connection.execute("SELECT * FROM recon_differences WHERE id=?", (diff_id,)).fetchone()
            if own:
                connection.commit()
        except Exception:
            if own:
                connection.rollback()
            raise
        finally:
            if own:
                connection.close()
        return dict(row)

    def get_receipt(self, receipt_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM bank_receipts WHERE id=?", (receipt_id,)).fetchone()
        return dict(row) if row is not None else None

    def list_receipts(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM bank_receipts WHERE record_id=? ORDER BY id", (record_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def next_received_receipt(self, after_id: int, record_id: Optional[int] = None,
                              connection: sqlite3.Connection = None) -> Optional[Dict[str, Any]]:
        sql = "SELECT * FROM bank_receipts WHERE status='received' AND id > ?"
        params: List[Any] = [after_id]
        if record_id is not None:
            sql += " AND record_id=?"
            params.append(record_id)
        sql += " ORDER BY id LIMIT 1"
        own = connection is None
        if own:
            connection = self._connect()
        try:
            row = connection.execute(sql, params).fetchone()
        finally:
            if own:
                connection.close()
        return dict(row) if row is not None else None

    def open_differences(self, record_id: Optional[int] = None,
                         connection: sqlite3.Connection = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM recon_differences WHERE status='open'"
        params: List[Any] = []
        if record_id is not None:
            sql += " AND record_id=?"
            params.append(record_id)
        sql += " ORDER BY id"
        own = connection is None
        if own:
            connection = self._connect()
        try:
            rows = connection.execute(sql, params).fetchall()
        finally:
            if own:
                connection.close()
        return [dict(row) for row in rows]

    def get_difference(self, difference_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM recon_differences WHERE id=?", (difference_id,)).fetchone()
        if row is None:
            raise NotFound("差异不存在")
        item = dict(row)
        item["details"] = json.loads(item["details"])
        return item

    def list_differences(self, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if status:
                rows = connection.execute(
                    "SELECT * FROM recon_differences WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM recon_differences ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    # ---- 对账检查点 -----------------------------------------------------

    def get_checkpoint(self, scope: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT last_receipt_id FROM recon_checkpoints WHERE scope=?", (scope,)
            ).fetchone()
        return int(row["last_receipt_id"]) if row is not None else 0

    def advance_checkpoint(self, scope: str, receipt_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO recon_checkpoints(scope,last_receipt_id,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(scope) DO UPDATE SET last_receipt_id=excluded.last_receipt_id, "
                "updated_at=excluded.updated_at WHERE excluded.last_receipt_id > recon_checkpoints.last_receipt_id",
                (scope, receipt_id, _now()),
            )

    # ---- 对账执行（单笔事务，可从检查点安全重试） -----------------------

    @staticmethod
    def _open_difference_for_receipt(connection: sqlite3.Connection, receipt_id: int,
                                     reason: Optional[str] = None) -> Optional[sqlite3.Row]:
        sql = "SELECT * FROM recon_differences WHERE receipt_id=? AND status='open'"
        params: List[Any] = [receipt_id]
        if reason is not None:
            sql += " AND reason=?"
            params.append(reason)
        sql += " ORDER BY id LIMIT 1"
        return connection.execute(sql, params).fetchone()

    def settle_match(self, connection: sqlite3.Connection, receipt: Dict[str, Any],
                     installment: Dict[str, Any], actor_id: str,
                     difference: Dict[str, Any] = None) -> None:
        """核销期次。difference 非空表示差异重试后转正，需一并关闭。整个调用在调用方事务内。"""
        now = _now()
        connection.execute(
            "UPDATE installments SET status='settled',receipt_id=?,settled_at=? WHERE id=? AND status='active'",
            (receipt["id"], now, installment["id"]),
        )
        connection.execute("UPDATE bank_receipts SET status='matched' WHERE id=?", (receipt["id"],))
        record_id = receipt["record_id"]
        version = int(connection.execute("SELECT version FROM records WHERE id=?", (record_id,))
                      .fetchone()["version"])
        self._audit(connection, record_id, "receipt_matched", actor_id, version,
                    {"receipt_id": receipt["id"], "bank_serial": receipt["bank_serial"],
                     "installment_id": installment["id"], "period_no": installment["period_no"],
                     "amount": float(installment["amount_due"])},
                    idempotency_key="receipt_matched:%s" % receipt["id"])
        if difference is not None:
            connection.execute(
                "UPDATE recon_differences SET status='resolved',resolution='auto_matched',resolved_at=? WHERE id=?",
                (now, difference["id"]),
            )
            self._audit(connection, record_id, "difference_resolved", actor_id, version,
                        {"difference_id": difference["id"], "resolution": "auto_matched",
                         "receipt_id": receipt["id"]},
                        idempotency_key="difference_auto:%s" % difference["id"])

    def open_difference_for_receipt(self, connection: sqlite3.Connection, receipt: Dict[str, Any],
                                    reason: str, detail: Dict[str, Any], actor_id: str) -> int:
        """回执转待核差异；该回执已有未关闭差异时只加尝试次数，不重复登记、不重复审计。"""
        now = _now()
        existing = self._open_difference_for_receipt(connection, receipt["id"])
        connection.execute("UPDATE bank_receipts SET status='difference' WHERE id=?", (receipt["id"],))
        record_id = receipt["record_id"]
        version = int(connection.execute("SELECT version FROM records WHERE id=?", (record_id,))
                      .fetchone()["version"])
        if existing is not None:
            connection.execute(
                "UPDATE recon_differences SET attempts=attempts+1,reason=?,details=? WHERE id=?",
                (reason, json.dumps(detail, ensure_ascii=False, sort_keys=True), existing["id"]),
            )
            return int(existing["id"])
        cursor = connection.execute(
            "INSERT INTO recon_differences(record_id,receipt_id,bank_serial,reason,details,status,"
            "attempts,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (record_id, receipt["id"], receipt["bank_serial"], reason,
             json.dumps(detail, ensure_ascii=False, sort_keys=True), "open", 1, actor_id, now),
        )
        diff_id = int(cursor.lastrowid)
        self._audit(connection, record_id, "difference_registered", actor_id, version,
                    {"difference_id": diff_id, "reason": reason,
                     "bank_serial": receipt["bank_serial"], "detail": detail},
                    idempotency_key="difference_registered:%s" % receipt["id"])
        return diff_id

    def requeue_difference(self, connection: sqlite3.Connection, difference: Dict[str, Any],
                           reason: str, detail: Dict[str, Any]) -> None:
        """差异重试仍不符：留在待核队列并累加尝试次数。"""
        connection.execute(
            "UPDATE recon_differences SET attempts=attempts+1,reason=?,details=? WHERE id=?",
            (reason, json.dumps(detail, ensure_ascii=False, sort_keys=True), difference["id"]),
        )

    def resolve_difference(self, difference_id: int, resolution: str, reviewer: str) -> Dict[str, Any]:
        """人工关闭待核差异：write_off（核销挂账）或 ignore（忽略重复等）。"""
        now = _now()
        with self.immediate() as connection:
            row = connection.execute("SELECT * FROM recon_differences WHERE id=?", (difference_id,)).fetchone()
            if row is None:
                raise NotFound("差异不存在")
            difference = dict(row)
            if difference["status"] != "open":
                raise Conflict("差异已关闭")
            connection.execute(
                "UPDATE recon_differences SET status='resolved',resolution=?,resolved_at=? WHERE id=?",
                (resolution, now, difference_id),
            )
            version = int(connection.execute("SELECT version FROM records WHERE id=?",
                                             (difference["record_id"],)).fetchone()["version"])
            self._audit(connection, difference["record_id"], "difference_resolved", reviewer, version,
                        {"difference_id": difference_id, "resolution": resolution,
                         "reason": difference["reason"], "bank_serial": difference["bank_serial"]},
                        idempotency_key="difference_manual:%s" % difference_id)
            result = connection.execute("SELECT * FROM recon_differences WHERE id=?", (difference_id,)).fetchone()
        return dict(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            self._audit(connection, record_id, action, actor_id, int(row["version"]), details)

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
