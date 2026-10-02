import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0, 'arrears': 12000.0, 'hardship_factor': 0.5, 'program_type': 'reduction', 'requested_months': 3}


class ReconciliationServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.servicer = Actor("srv", "servicer")
        record = self.service.create(Actor("creator", "intake_officer"), "MORT-40001", dict(CREATE_DATA))
        for action, role, data in [
            ('assess', 'intake_officer', {'assessment_note': '收入波动'}),
            ('approve', 'underwriter', {'exception_approved': False}),
            ('activate', 'servicer', {'borrower_ack': True}),
        ]:
            record = self.service.act(Actor("operator", role), record["id"], record["version"], action, data)
        self.record_id = record["id"]
        view = self.service.recon_view(self.servicer, self.record_id)
        self.amount = view["active_plan"]["installment_amount"]
        self.periods = view["active_plan"]["periods"]

    def tearDown(self):
        self.temp.cleanup()

    def _receipt(self, serial, amount=None, period_no=None):
        return self.service.intake_receipt(
            self.servicer, self.record_id,
            {"bank_serial": serial, "amount": self.amount if amount is None else amount, "period_no": period_no},
        )

    def test_activation_creates_single_active_plan_and_receivables(self):
        view = self.service.recon_view(self.servicer, self.record_id)
        active = [plan for plan in view["plans"] if plan["status"] == "active"]
        self.assertEqual(len(active), 1)
        self.assertEqual(len(view["receivables"]), self.periods)
        self.assertTrue(all(item["status"] == "payable" for item in view["receivables"]))
        self.assertEqual(view["performance"]["status"], "current")
        # activate审计仍是一条，时间线不被方案生成拆成多条
        timeline = self.service.timeline(Actor("creator", "intake_officer"), self.record_id)
        self.assertEqual([event["action"] for event in timeline], ["created", "assess", "approve", "activate"])
        self.assertIn("plan", timeline[-1]["details"])

    def test_receipt_settles_single_receivable_and_serial_is_unique(self):
        self._receipt("TX1", period_no=1)
        # 同流水第二次送达：不能再核销任何应收
        self._receipt("TX1", period_no=2)
        view = self.service.recon_view(self.servicer, self.record_id)
        statuses = {item["period_no"]: item["status"] for item in view["receivables"]}
        self.assertEqual(statuses[1], "settled")
        self.assertEqual(statuses[2], "payable")
        discrepancies = self.service.list_discrepancies(self.servicer)
        self.assertEqual(len(discrepancies), 1)
        self.assertEqual(discrepancies[0]["difference_type"], "duplicate")
        self.assertEqual(discrepancies[0]["bank_serial"], "TX1")

    def test_out_of_order_and_amount_mismatch_enter_discrepancies(self):
        self._receipt("TXA", period_no=3)
        self._receipt("TXB", amount=10.0, period_no=1)
        discrepancies = self.service.list_discrepancies(self.servicer, record_id=self.record_id)
        kinds = {item["difference_type"] for item in discrepancies}
        self.assertEqual(kinds, {"out_of_order", "amount_mismatch"})
        view = self.service.recon_view(self.servicer, self.record_id)
        # 履约状态不被异常回执改动，也没有应收被核销
        self.assertTrue(all(item["status"] == "payable" for item in view["receivables"]))

    def test_fifo_settlement_then_lagged_receipt_can_recheck(self):
        self._receipt("T1", period_no=1)
        self._receipt("T3", period_no=3)  # 第2期未收，乱序挂起
        self._receipt("T2")              # 不指定期号，最早未核销=第2期
        discrepancy = [d for d in self.service.list_discrepancies(self.servicer) if d["bank_serial"] == "T3"][0]
        result = self.service.recheck_discrepancy(self.servicer, discrepancy["id"])
        self.assertEqual(result["status"], "settled")
        view = self.service.recon_view(self.servicer, self.record_id)
        self.assertTrue(all(item["status"] == "settled" for item in view["receivables"]))
        self.assertEqual(view["performance"]["status"], "completed")

    def test_reopened_receipt_is_not_skipped_by_checkpoint(self):
        # 乱序回执（id靠前）先挂差异；前面期间补齐后人工复核，
        # 即使该回执id早于检查点也能被重新处理，且批量对账不会死循环
        self._receipt("T1", period_no=1)
        self._receipt("T3", period_no=3)  # 第2期未收 -> 乱序
        self.service.reconcile(self.servicer)
        self._receipt("T2")              # FIFO核销第2期，检查点越过T3
        discrepancies = self.service.list_discrepancies(self.servicer, status="open")
        self.assertEqual([d["difference_type"] for d in discrepancies], ["out_of_order"])
        result = self.service.recheck_discrepancy(self.servicer, discrepancies[0]["id"])
        self.assertEqual(result["status"], "settled")
        # 重新打开后若仍是差异（如金额不符），批量对账不能因检查点反复捞取形成死循环
        self.assertEqual(self.service.reconcile(self.servicer)["processed"], 0)

    def test_ignore_discrepancy(self):
        self._receipt("BAD", amount=1.0, period_no=1)
        discrepancy = self.service.list_discrepancies(self.servicer)[0]
        self.service.ignore_discrepancy(Actor("srv", "servicer"), discrepancy["id"], {"note": "退回"})
        open_items = self.service.list_discrepancies(self.servicer, status="open")
        self.assertEqual(open_items, [])
        # 已处理差异不能重复处理
        with self.assertRaises(Conflict):
            self.service.ignore_discrepancy(self.servicer, discrepancy["id"], {})

    def test_overdue_performance_is_derived_not_pushed_by_receipt(self):
        # 单独构造首期到期日在过去的记录：履约状态由应收推导为逾期，
        # 不需要任何回执或违约动作来“改履约状态”
        record = self.service.create(Actor("creator", "intake_officer"), "MORT-40002",
                                     dict(CREATE_DATA, borrower_id="B-40002"))
        for action, role, data, action_data in [
            ('assess', 'intake_officer', {'assessment_note': 'x'}, None),
            ('approve', 'underwriter', {'exception_approved': False}, None),
        ]:
            record = self.service.act(Actor("operator", role), record["id"], record["version"], action, data)
        record = self.service.act(
            Actor("operator", "servicer"), record["id"], record["version"], "activate",
            {'borrower_ack': True, 'first_due_date': '2000-01-01'},
        )
        view = self.service.recon_view(self.servicer, record["id"])
        self.assertEqual(view["performance"]["status"], "overdue")
        self.assertEqual(view["performance"]["overdue_periods"], list(range(1, view["active_plan"]["periods"] + 1)))


class PlanChangeDualControlTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        record = self.service.create(Actor("creator", "intake_officer"), "MORT-50001", dict(CREATE_DATA))
        for action, role, data in [
            ('assess', 'intake_officer', {'assessment_note': 'x'}),
            ('approve', 'underwriter', {'exception_approved': False}),
            ('activate', 'servicer', {'borrower_ack': True}),
        ]:
            record = self.service.act(Actor("operator", role), record["id"], record["version"], action, data)
        self.record_id = record["id"]
        self.agent = Actor("agent-a", "servicer")
        self.reviewer = Actor("reviewer-b", "reviewer")
        view = self.service.recon_view(Actor("s", "servicer"), self.record_id)
        self.old_amount = view["active_plan"]["installment_amount"]

    def tearDown(self):
        self.temp.cleanup()

    def _submit(self, actor=None):
        return self.service.submit_plan_change(actor or self.agent, self.record_id, {
            "periods": 2, "installment_amount": 2500.0, "reason": "收入再次下降",
        })

    def test_old_plan_keeps_collecting_until_confirmation(self):
        change = self._submit()
        self.assertEqual(change["status"], "pending")
        # 待复核期间旧计划继续收款
        self.service.intake_receipt(Actor("s", "servicer"), self.record_id, {"bank_serial": "K1", "amount": self.old_amount, "period_no": 1})
        view = self.service.recon_view(Actor("s", "servicer"), self.record_id)
        self.assertEqual(view["active_plan"]["version"], 1)
        self.assertEqual(view["receivables"][0]["status"], "settled")
        # 只能有一张待复核单
        with self.assertRaises(Conflict):
            self._submit()

    def test_submitter_cannot_confirm_and_reviewer_role_required(self):
        change = self._submit()
        with self.assertRaises(PermissionDenied):
            self.service.review_plan_change(Actor("agent-a", "reviewer"), change["id"], True, {})
        with self.assertRaises(PermissionDenied):
            self.service.review_plan_change(Actor("other", "servicer"), change["id"], True, {})

    def test_confirmation_supersedes_old_plan_and_recomputes_unsettled(self):
        change = self._submit()
        self.service.intake_receipt(Actor("s", "servicer"), self.record_id, {"bank_serial": "K1", "amount": self.old_amount, "period_no": 1})
        result = self.service.review_plan_change(self.reviewer, change["id"], True, {"review_note": "同意展期"})
        self.assertEqual(result["change"]["status"], "confirmed")
        self.assertEqual(result["new_plan"]["version"], 2)
        view = self.service.recon_view(Actor("s", "servicer"), self.record_id)
        statuses = [(item["plan_id"], item["period_no"], item["amount_due"], item["status"]) for item in view["receivables"]]
        # 旧方案：第1期已核销保留；第2/3期未核销失效
        old = [row for row in statuses if row[0] != result["new_plan"]["id"]]
        self.assertIn((result["change"]["superseded_plan_id"], 1, self.old_amount, "settled"), old)
        self.assertTrue(any(row[3] == "void" for row in old))
        # 新方案：两期应收按新金额重算
        new = [row for row in statuses if row[0] == result["new_plan"]["id"]]
        self.assertEqual([(row[1], row[2], row[3]) for row in new], [(1, 2500.0, "payable"), (2, 2500.0, "payable")])
        # 只有一个有效方案
        self.assertEqual(len([p for p in view["plans"] if p["status"] == "active"]), 1)
        # 不能重复确认
        with self.assertRaises(Conflict):
            self.service.review_plan_change(self.reviewer, change["id"], True, {})

    def test_new_receipt_after_confirm_settles_against_new_plan(self):
        change = self._submit()
        self.service.review_plan_change(self.reviewer, change["id"], True, {})
        self.service.intake_receipt(Actor("s", "servicer"), self.record_id, {"bank_serial": "N1", "amount": 2500.0, "period_no": 1})
        view = self.service.recon_view(Actor("s", "servicer"), self.record_id)
        new_plan = [p for p in view["plans"] if p["status"] == "active"][0]
        new_receivables = [r for r in view["receivables"] if r["plan_id"] == new_plan["id"]]
        self.assertEqual(new_receivables[0]["status"], "settled")

    def test_rejection_keeps_old_plan(self):
        change = self._submit()
        result = self.service.review_plan_change(self.reviewer, change["id"], False, {"review_note": "材料不足"})
        self.assertEqual(result["change"]["status"], "rejected")
        view = self.service.recon_view(Actor("s", "servicer"), self.record_id)
        self.assertEqual(view["active_plan"]["version"], 1)
        self.assertEqual(len(view["plans"]), 1)
        with self.assertRaises(Conflict):
            self.service.review_plan_change(self.reviewer, change["id"], False, {})

    def test_invalid_plan_change_payload(self):
        with self.assertRaises(ValidationError):
            self.service.submit_plan_change(self.agent, self.record_id, {"periods": 0, "installment_amount": 1.0, "reason": "x"})
        with self.assertRaises(ValidationError):
            self.service.submit_plan_change(self.agent, self.record_id, {"periods": 2, "installment_amount": 1.0})
