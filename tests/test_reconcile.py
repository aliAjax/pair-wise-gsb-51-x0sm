"""纾困方案、还款计划与扣款回执的可恢复对账流程测试。"""
import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError
from src import reconcile


CREATE_DATA = {'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0,
               'arrears': 12000.0, 'hardship_factor': 0.5, 'program_type': 'reduction', 'requested_months': 9}
FLOW = [('assess', 'intake_officer', {'assessment_note': '收入波动'}, 'assessed'),
        ('approve', 'underwriter', {'exception_approved': False}, 'approved'),
        ('activate', 'servicer', {'borrower_ack': True}, 'active')]

# reduction 方案：max(7000 - 9000*0.4, 0) = 3400
PAYMENT = 3400.0
SERVICER = Actor("s1", "servicer")
REVIEWER = Actor("r1", "reviewer")


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.record = self._activate("MORT-31001")

    def tearDown(self):
        self.temp.cleanup()

    def _activate(self, reference):
        record = self.service.create(Actor("creator", "intake_officer"), reference, CREATE_DATA)
        for action, role, data, expected_state in FLOW:
            record = self.service.act(Actor("operator", role), record["id"], record["version"], action, data)
        self.assertEqual(record["state"], "active")
        return record

    def _receipt(self, serial, amount=PAYMENT, period_no=None):
        return self.service.ingest_receipt(
            SERVICER, self.record["id"],
            {"bank_serial": serial, "amount": amount, "period_no": period_no},
        )

    def _active_plan(self):
        return self.service.repository.get_active_plan(self.record["id"])

    # ---- 正常核销 -------------------------------------------------------

    def test_receipts_match_sequentially(self):
        r1 = self._receipt("TX-1", period_no=1)
        self.assertEqual(r1["outcome"], "matched")
        terms = self.service.installments(SERVICER, self.record["id"])
        self.assertEqual(terms[0]["status"], "settled")
        self.assertTrue(all(term["status"] == "active" for term in terms[1:]))
        r2 = self._receipt("TX-2", period_no=2)
        self.assertEqual(r2["outcome"], "matched")

    # ---- 同一流水只能核销一笔 -------------------------------------------

    def test_duplicate_serial_never_writes_twice(self):
        self._receipt("TX-DUP", period_no=1)
        dup = self._receipt("TX-DUP", period_no=2)
        self.assertEqual(dup["outcome"], "duplicate")
        terms = self.service.installments(SERVICER, self.record["id"])
        self.assertEqual([t["status"] for t in terms].count("settled"), 1)
        # 重复重复登记也只保留一条差异
        self._receipt("TX-DUP", period_no=3)
        diffs = [d for d in self.service.list_differences(REVIEWER) if d["reason"] == "duplicate"]
        self.assertEqual(len(diffs), 1)

    # ---- 乱序：先挂差异，补齐后自动转正 ---------------------------------

    def test_out_of_order_pending_then_recovers(self):
        late = self._receipt("TX-2", period_no=2)
        self.assertEqual(late["outcome"], "difference")
        self.assertEqual(late["reason"], "out_of_order")
        self._receipt("TX-1", period_no=1)
        summary = self.service.run_reconciliation(SERVICER)
        self.assertEqual(summary["matched"], 1)
        terms = self.service.installments(SERVICER, self.record["id"])
        self.assertEqual(terms[1]["status"], "settled")
        open_diffs = self.service.list_differences(REVIEWER, status="open")
        self.assertEqual(open_diffs, [])

    # ---- 金额不符 -------------------------------------------------------

    def test_amount_mismatch_pending_and_manual_write_off(self):
        bad = self._receipt("TX-BAD", amount=PAYMENT - 100, period_no=1)
        self.assertEqual(bad["reason"], "amount_mismatch")
        terms = self.service.installments(SERVICER, self.record["id"])
        self.assertEqual(terms[0]["status"], "active")
        # 重跑对账不会重复登记，只会累加尝试次数
        self.service.run_reconciliation(SERVICER)
        diff = self.service.repository.get_difference(bad["difference_id"])
        self.assertEqual(diff["attempts"], 2)
        resolved = self.service.resolve_difference(REVIEWER, bad["difference_id"], {"resolution": "write_off"})
        self.assertEqual(resolved["resolution"], "write_off")

    # ---- 方案生效前到达的回执，方案生效后可补核 --------------------------

    def test_receipt_before_plan_then_matches_after_activation(self):
        other = self.service.create(Actor("creator", "intake_officer"), "MORT-31002", CREATE_DATA)
        self.service.act(Actor("operator", "intake_officer"), other["id"], other["version"],
                         "assess", {"assessment_note": "x"})
        other = self.service.get_record(SERVICER, other["id"])
        self.service.act(Actor("operator", "underwriter"), other["id"], other["version"],
                         "approve", {"exception_approved": False})
        early = self.service.ingest_receipt(
            SERVICER, other["id"], {"bank_serial": "TX-EARLY", "amount": PAYMENT, "period_no": 1}
        )
        self.assertEqual(early["reason"], "no_active_plan")
        other = self.service.get_record(SERVICER, other["id"])
        self.service.act(Actor("operator", "servicer"), other["id"], other["version"],
                         "activate", {"borrower_ack": True})
        summary = self.service.run_reconciliation(SERVICER, record_id=other["id"])
        self.assertEqual(summary["matched"], 1)

    # ---- 方案变更双人复核与失效重算 -------------------------------------

    def test_plan_change_review_keeps_old_plan_until_confirmation(self):
        self._receipt("TX-1", period_no=1)
        change = self.service.submit_plan_change(
            SERVICER, self.record["id"], {"payment_amount": 1000.0, "months": 6, "reason": "再次失业"}
        )
        self.assertEqual(change["status"], "pending")
        # 待复核期间旧计划继续收款
        ongoing = self._receipt("TX-2", period_no=2)
        self.assertEqual(ongoing["outcome"], "matched")
        # 经办人不能自审
        with self.assertRaises(PermissionDenied):
            self.service.review_plan_change(Actor("s1", "reviewer"), change["id"], "confirm", {})
        # 有待复核变更时不能重复提交
        with self.assertRaises(Conflict):
            self.service.submit_plan_change(
                SERVICER, self.record["id"], {"payment_amount": 800.0, "months": 3, "reason": "x"}
            )
        confirmed = self.service.review_plan_change(REVIEWER, change["id"], "confirm", {"review_note": "同意"})
        self.assertEqual(confirmed["status"], "confirmed")

        terms = self.service.installments(SERVICER, self.record["id"])
        old_terms = [t for t in terms if t["plan_id"] == change["old_plan_id"]]
        self.assertEqual([t["status"] for t in old_terms].count("settled"), 2)
        self.assertTrue(all(t["status"] == "void" for t in old_terms if t["period_no"] >= 3))
        # 新计划从第3期开始，金额 1000
        new_active = [t for t in terms if t["status"] == "active"]
        self.assertEqual([t["period_no"] for t in new_active], list(range(3, 9)))
        self.assertTrue(all(abs(t["amount_due"] - 1000.0) < 0.001 for t in new_active))
        # 每期应收仍只对应一个有效方案：只有一个 active plan
        with self.service.repository._connect() as conn:
            active_plans = conn.execute(
                "SELECT COUNT(*) AS c FROM plans WHERE record_id=? AND status='active'",
                (self.record["id"],),
            ).fetchone()["c"]
        self.assertEqual(active_plans, 1)

        # 新回执按新计划核销
        shifted = self._receipt("TX-3", amount=1000.0, period_no=3)
        self.assertEqual(shifted["outcome"], "matched")

    def test_plan_change_rejection_keeps_old_plan(self):
        change = self.service.submit_plan_change(
            SERVICER, self.record["id"], {"payment_amount": 1000.0, "months": 6, "reason": "x"}
        )
        self.service.review_plan_change(REVIEWER, change["id"], "reject", {"review_note": "材料不足"})
        terms = self.service.installments(SERVICER, self.record["id"])
        self.assertTrue(all(t["status"] == "active" for t in terms))
        receipt = self._receipt("TX-1", period_no=1)
        self.assertEqual(receipt["outcome"], "matched")

    def test_non_reviewer_cannot_confirm(self):
        change = self.service.submit_plan_change(
            SERVICER, self.record["id"], {"payment_amount": 1000.0, "months": 6, "reason": "x"}
        )
        with self.assertRaises(PermissionDenied):
            self.service.review_plan_change(Actor("s2", "servicer"), change["id"], "confirm", {})

    # ---- 检查点恢复与审计幂等 -------------------------------------------

    def test_checkpoint_resume_without_duplicate_audit(self):
        repo = self.service.repository
        # 直接落入 received 状态以驱动检查点流水线
        for serial in ["C-1", "C-2", "C-3"]:
            repo.insert_receipt(self.record["id"], serial, PAYMENT, None, "bank")
        with self.assertRaises(RuntimeError):
            self.service.run_reconciliation(SERVICER, fail_after=1)
        checkpoint = repo.get_checkpoint("global")
        self.assertGreaterEqual(checkpoint, 1)
        # 恢复执行：首笔已在崩溃前提交核销，剩余两笔从检查点继续
        summary = self.service.run_reconciliation(SERVICER)
        self.assertEqual(summary["matched"], 2)
        terms = self.service.installments(SERVICER, self.record["id"])
        self.assertEqual([t["status"] for t in terms[:3]].count("settled"), 3)
        # 每笔回执只有一条 receipt_matched 审计，幂等键无重复
        timeline = self.service.timeline(SERVICER, self.record["id"])
        matched_events = [e for e in timeline if e["action"] == "receipt_matched"]
        self.assertEqual(len(matched_events), 3)
        keys = [e["idempotency_key"] for e in timeline if e["idempotency_key"]]
        self.assertEqual(len(keys), len(set(keys)))

    def test_audit_idempotency_key_blocks_double_insert(self):
        # 直接验证：相同幂等键的审计第二次插入被数据库拒绝
        self._receipt("TX-X", period_no=1)
        timeline = self.service.timeline(SERVICER, self.record["id"])
        key = next(e["idempotency_key"] for e in timeline if e["action"] == "receipt_matched")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            with self.service.repository._connect() as conn:
                conn.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at,"
                    "idempotency_key) VALUES(?,?,?,?,?,?,?)",
                    (self.record["id"], "receipt_matched", "x", 1, "{}", "now", key),
                )

    # ---- 纯匹配逻辑单元测试 ---------------------------------------------

    def test_classify_out_of_order_to_voided_period(self):
        plan = self._active_plan()
        terms = self.service.installments(SERVICER, self.record["id"])
        receipt = {"id": 99, "record_id": self.record["id"], "bank_serial": "X", "amount": PAYMENT,
                   "period_no": 5}
        kind, reason, _ = reconcile.classify(receipt, plan, terms)
        self.assertEqual(kind, "difference")
        self.assertEqual(reason, "out_of_order")


if __name__ == "__main__":
    unittest.main()
