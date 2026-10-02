import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict


CREATE_DATA = {'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0, 'arrears': 12000.0, 'hardship_factor': 0.5, 'program_type': 'reduction', 'requested_months': 9}
FLOW = [('assess', 'intake_officer', {'assessment_note': '收入波动'}, 'assessed'), ('approve', 'underwriter', {'exception_approved': False}, 'approved'), ('activate', 'servicer', {'borrower_ack': True}, 'active'), ('cure', 'servicer', {'arrears_cleared': True}, 'cured')]


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_workflow_and_audit(self):
        record = self.service.create(Actor("creator", "intake_officer"), "MORT-27001", CREATE_DATA)
        self.assertEqual(record["state"], "submitted")
        for action, role, data, expected_state in FLOW:
            record = self.service.act(Actor("operator", role), record["id"], record["version"], action, data)
            self.assertEqual(record["state"], expected_state)
        timeline = self.service.timeline(Actor("creator", "intake_officer"), record["id"])
        # created + 4 个动作；activate 同事务额外审计 plan_created
        self.assertEqual(len(timeline), len(FLOW) + 2)
        self.assertEqual(timeline[-1]["action"], FLOW[-1][0])

    def test_activate_creates_single_active_plan_and_installments(self):
        record = self.service.create(Actor("creator", "intake_officer"), "MORT-27010", CREATE_DATA)
        for action, role, data, expected_state in FLOW[:3]:
            record = self.service.act(Actor("operator", role), record["id"], record["version"], action, data)
        self.assertEqual(record["state"], "active")
        terms = self.service.installments(Actor("operator", "servicer"), record["id"])
        self.assertEqual(len(terms), record["payload"]["approved_months"])
        self.assertTrue(all(term["status"] == "active" for term in terms))
        self.assertEqual([term["period_no"] for term in terms], list(range(1, len(terms) + 1)))
