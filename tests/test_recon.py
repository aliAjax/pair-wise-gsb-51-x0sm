import unittest

from src.recon import (
    AMOUNT_MISMATCH,
    DIFFERENCE,
    DUP_SERIAL,
    NO_ACTIVE_PLAN,
    NO_PAYABLE,
    OUT_OF_ORDER,
    SETTLE,
    SETTLED,
    VOID,
    add_months,
    classify_receipt,
    monthly_schedule,
    performance_summary,
)


def receipt(bank_serial, amount, period_no=None):
    return {"id": 1, "bank_serial": bank_serial, "amount": amount, "period_no": period_no, "received_at": "2026-10-02"}


def receivables(amount=3400.0):
    return [
        {"id": 11, "plan_id": 1, "period_no": 1, "due_date": "2026-11-01", "amount_due": amount, "status": "payable"},
        {"id": 12, "plan_id": 1, "period_no": 2, "due_date": "2026-12-01", "amount_due": amount, "status": "payable"},
        {"id": 13, "plan_id": 1, "period_no": 3, "due_date": "2027-01-01", "amount_due": amount, "status": "payable"},
    ]


PLAN = {"id": 1, "status": "active"}


class ScheduleTest(unittest.TestCase):
    def test_monthly_schedule(self):
        schedule = monthly_schedule("2026-01-31", 3, 3400.0)
        self.assertEqual([item["period_no"] for item in schedule], [1, 2, 3])
        self.assertEqual([item["due_date"] for item in schedule], ["2026-01-31", "2026-02-28", "2026-03-31"])
        self.assertTrue(all(item["amount_due"] == 3400.0 for item in schedule))

    def test_add_months_clamps_to_month_end(self):
        self.assertEqual(add_months("2026-01-31", 1), "2026-02-28")
        self.assertEqual(add_months("2024-12-15", 2), "2025-02-15")


class ClassifyTest(unittest.TestCase):
    def test_exact_settlement(self):
        decision = classify_receipt(receipt("T1", 3400.0, 1), PLAN, receivables(), set())
        self.assertEqual(decision.kind, SETTLE)
        self.assertEqual(decision.receivable_id, 11)

    def test_fifo_without_period(self):
        decision = classify_receipt(receipt("T1", 3400.0), PLAN, receivables(), set())
        self.assertEqual(decision.kind, SETTLE)
        self.assertEqual(decision.receivable_id, 11)

    def test_same_serial_can_only_settle_once(self):
        items = receivables()
        items[0]["status"] = SETTLED
        decision = classify_receipt(receipt("T1", 3400.0, 2), PLAN, items, {"T1"})
        self.assertEqual(decision.kind, DIFFERENCE)
        self.assertEqual(decision.difference_type, DUP_SERIAL)

    def test_out_of_order_goes_to_discrepancy(self):
        decision = classify_receipt(receipt("T3", 3400.0, 3), PLAN, receivables(), set())
        self.assertEqual(decision.kind, DIFFERENCE)
        self.assertEqual(decision.difference_type, OUT_OF_ORDER)

    def test_in_order_after_earlier_paid(self):
        items = receivables()
        items[0]["status"] = SETTLED
        decision = classify_receipt(receipt("T2", 3400.0, 2), PLAN, items, {"T1"})
        self.assertEqual(decision.kind, SETTLE)
        self.assertEqual(decision.receivable_id, 12)

    def test_amount_mismatch(self):
        decision = classify_receipt(receipt("T1", 99.0, 1), PLAN, receivables(), set())
        self.assertEqual(decision.kind, DIFFERENCE)
        self.assertEqual(decision.difference_type, AMOUNT_MISMATCH)
        self.assertEqual(decision.expected_amount, 3400.0)

    def test_amount_within_tolerance_settles(self):
        decision = classify_receipt(receipt("T1", 3400.004, 1), PLAN, receivables(), set())
        self.assertEqual(decision.kind, SETTLE)

    def test_unknown_period_is_no_payable(self):
        decision = classify_receipt(receipt("T9", 3400.0, 9), PLAN, receivables(), set())
        self.assertEqual(decision.kind, DIFFERENCE)
        self.assertEqual(decision.difference_type, NO_PAYABLE)

    def test_voided_receivable_is_no_payable(self):
        items = receivables()
        items[0]["status"] = VOID
        decision = classify_receipt(receipt("T1", 3400.0, 1), PLAN, items, set())
        self.assertEqual(decision.kind, DIFFERENCE)
        self.assertEqual(decision.difference_type, NO_PAYABLE)

    def test_without_active_plan(self):
        decision = classify_receipt(receipt("T1", 3400.0, 1), None, receivables(), set())
        self.assertEqual(decision.kind, DIFFERENCE)
        self.assertEqual(decision.difference_type, NO_ACTIVE_PLAN)

    def test_settled_period_is_duplicate(self):
        items = receivables()
        items[0]["status"] = SETTLED
        decision = classify_receipt(receipt("T2", 3400.0, 1), PLAN, items, {"T1"})
        self.assertEqual(decision.difference_type, DUP_SERIAL)


class PerformanceTest(unittest.TestCase):
    def test_current(self):
        summary = performance_summary(PLAN, receivables(), today="2026-10-02")
        self.assertEqual(summary["status"], "current")
        self.assertEqual(summary["settled_periods"], 0)

    def test_overdue(self):
        items = receivables()
        items[0]["due_date"] = "2026-10-01"  # 早于today(2026-10-02)且未核销 -> 逾期
        summary = performance_summary(PLAN, items, today="2026-10-02")
        self.assertEqual(summary["status"], "overdue")
        self.assertEqual(summary["overdue_periods"], [1])

    def test_completed_only_counts_active_plan(self):
        items = [
            {"id": 11, "plan_id": 1, "period_no": 1, "due_date": "2026-09-01", "amount_due": 3400.0, "status": SETTLED, "paid_amount": 3400.0},
            {"id": 12, "plan_id": 1, "period_no": 2, "due_date": "2026-10-01", "amount_due": 3400.0, "status": SETTLED, "paid_amount": 3400.0},
            # 旧方案遗留应收不影响新方案履约判定
            {"id": 1, "plan_id": 0, "period_no": 1, "due_date": "2026-08-01", "amount_due": 5000.0, "status": "payable"},
        ]
        summary = performance_summary(PLAN, items, today="2026-10-02")
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["total_periods"], 2)
        self.assertEqual(summary["paid_total"], 6800.0)

    def test_no_plan_returns_none(self):
        self.assertIsNone(performance_summary(None, receivables()))
