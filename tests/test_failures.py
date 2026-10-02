import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied


CREATE_DATA = {'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0, 'arrears': 12000.0, 'hardship_factor': 0.5, 'program_type': 'reduction', 'requested_months': 9}
FLOW = [('assess', 'intake_officer', {'assessment_note': '收入波动'}, 'assessed'), ('approve', 'underwriter', {'exception_approved': False}, 'approved'), ('activate', 'servicer', {'borrower_ack': True}, 'active'), ('cure', 'servicer', {'arrears_cleared': True}, 'cured')]


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_permission_and_duplicate(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(Actor("outsider", "outsider"), "MORT-27001", CREATE_DATA)
        self.service.create(Actor("creator", "intake_officer"), "MORT-27001", CREATE_DATA)
        with self.assertRaises(Conflict):
            self.service.create(Actor("creator", "intake_officer"), "MORT-27001", CREATE_DATA)

    def test_stale_version_is_rejected(self):
        record = self.service.create(Actor("creator", "intake_officer"), "MORT-27001", CREATE_DATA)
        first = FLOW[0]
        record = self.service.act(Actor("operator", first[1]), record["id"], record["version"], first[0], first[2])
        second = FLOW[1]
        with self.assertRaises(Conflict):
            self.service.act(Actor("operator", second[1]), record["id"], record["version"] - 1, second[0], second[2])
