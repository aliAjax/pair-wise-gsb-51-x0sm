import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.audit import AuditRecorder
from src.domain import Actor
from src.repository import Repository
from src.rules import DomainRules
from src.service import Service


CREATE_DATA = {'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0, 'arrears': 12000.0, 'hardship_factor': 0.5, 'program_type': 'reduction', 'requested_months': 3}


class FailOnFirstReceipt(Repository):
    """首笔回执提交前抛错：模拟对账写入失败。"""
    def __init__(self, path):
        super().__init__(path)
        self.failed = False

    def before_receipt_commit(self, connection, record_id, receipt_id):
        if not self.failed:
            self.failed = True
            raise RuntimeError("simulated write failure")


class FailOnSecondReceipt(Repository):
    """第二笔回执提交前抛错：验证检查点保留已提交的前缀。"""
    def __init__(self, path):
        super().__init__(path)
        self.calls = 0

    def before_receipt_commit(self, connection, record_id, receipt_id):
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("simulated write failure on second")


class FailOnPlanConfirm(Repository):
    def __init__(self, path):
        super().__init__(path)
        self.failed = False

    def before_plan_change_commit(self, connection):
        if not self.failed:
            self.failed = True
            raise RuntimeError("simulated plan confirm failure")


def activate_record(service):
    record = service.create(Actor("creator", "intake_officer"), "MORT-60001", dict(CREATE_DATA))
    for action, role, data in [
        ('assess', 'intake_officer', {'assessment_note': 'x'}),
        ('approve', 'underwriter', {'exception_approved': False}),
        ('activate', 'servicer', {'borrower_ack': True}),
    ]:
        record = service.act(Actor("operator", role), record["id"], record["version"], action, data)
    return record["id"]


class CheckpointRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.actor = Actor("srv", "servicer")

    def tearDown(self):
        self.temp.cleanup()

    def _service(self, repo):
        return Service(repo, DomainRules(), AuditRecorder(repo))

    def _enqueue(self, repo, record_id, count, amount):
        for index in range(1, count + 1):
            repo.insert_receipt(
                record_id,
                {"bank_serial": "R%s" % index, "amount": amount, "period_no": index, "received_at": "2026-10-02"},
                "srv",
            )

    def test_failed_txn_rolls_back_then_retry_succeeds_without_duplicate_audit(self):
        repo = FailOnFirstReceipt(self.db_path)
        service = self._service(repo)
        record_id = activate_record(service)
        amount = service.recon_view(self.actor, record_id)["active_plan"]["installment_amount"]
        self._enqueue(repo, record_id, 3, amount)

        with self.assertRaises(RuntimeError):
            service.reconcile(self.actor)

        connection = sqlite3.connect(self.db_path)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM settlements").fetchone()[0], 0)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0], 0)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM audit_events WHERE action IN ('receipt_settled','receipt_difference')").fetchone()[0],
            0,
        )
        connection.close()

        result = service.reconcile(self.actor)
        self.assertEqual(result["processed"], 3)
        self.assertEqual(result["settled"], 3)

        connection = sqlite3.connect(self.db_path)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM settlements").fetchone()[0], 3)
        # 同一业务事件只有一条审计，重放不产生重复
        duplicates = connection.execute(
            "SELECT idempotency_key, COUNT(*) AS c FROM audit_events "
            "WHERE idempotency_key LIKE 'receipt-%' GROUP BY idempotency_key HAVING c > 1"
        ).fetchall()
        self.assertEqual(duplicates, [])
        connection.close()

    def test_checkpoint_resumes_after_committed_prefix(self):
        repo = FailOnSecondReceipt(self.db_path)
        service = self._service(repo)
        record_id = activate_record(service)
        amount = service.recon_view(self.actor, record_id)["active_plan"]["installment_amount"]
        self._enqueue(repo, record_id, 3, amount)

        with self.assertRaises(RuntimeError):
            service.reconcile(self.actor)
        connection = sqlite3.connect(self.db_path)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM settlements").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT last_id FROM checkpoints").fetchone()[0], 1)
        connection.close()

        # 从检查点续做：第一笔不会被重复核销或重复审计
        result = service.reconcile(self.actor)
        self.assertEqual(result["processed"], 2)
        self.assertEqual(result["settled"], 2)
        connection = sqlite3.connect(self.db_path)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM settlements").fetchone()[0], 3)
        settled_audits = connection.execute(
            "SELECT COUNT(DISTINCT idempotency_key), COUNT(*) FROM audit_events WHERE action='receipt_settled'"
        ).fetchone()
        self.assertEqual(settled_audits, (3, 3))
        connection.close()

    def test_plan_change_confirm_failure_is_atomic_and_confirm_is_retryable(self):
        repo = FailOnPlanConfirm(self.db_path)
        service = self._service(repo)
        record_id = activate_record(service)
        change = service.submit_plan_change(Actor("agent", "servicer"), record_id, {
            "periods": 2, "installment_amount": 2500.0, "reason": "x",
        })
        with self.assertRaises(RuntimeError):
            service.review_plan_change(Actor("reviewer", "reviewer"), change["id"], True, {})

        connection = sqlite3.connect(self.db_path)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM plans WHERE status='active'").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT status FROM plan_changes").fetchone()[0], "pending")
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM audit_events WHERE action='plan_change_confirmed'").fetchone()[0], 0)
        connection.close()

        result = service.review_plan_change(Actor("reviewer", "reviewer"), change["id"], True, {})
        self.assertEqual(result["change"]["status"], "confirmed")
        connection = sqlite3.connect(self.db_path)
        self.assertEqual(connection.execute("SELECT status, COUNT(*) FROM plans GROUP BY status").fetchall(), [("active", 1), ("superseded", 1)])
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM audit_events WHERE action='plan_change_confirmed'").fetchone()[0], 1)
        connection.close()
