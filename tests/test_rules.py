import unittest

from src.domain import Actor, ValidationError
from src.rules import DomainRules


CREATE_DATA = {'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0, 'arrears': 12000.0, 'hardship_factor': 0.5, 'program_type': 'reduction', 'requested_months': 9}
FLOW = [('assess', 'intake_officer', {'assessment_note': '收入波动'}, 'assessed'), ('approve', 'underwriter', {'exception_approved': False}, 'approved'), ('activate', 'servicer', {'borrower_ack': True}, 'active'), ('cure', 'servicer', {'arrears_cleared': True}, 'cured')]


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def test_prepare_create(self):
        prepared = self.rules.prepare_create(CREATE_DATA)
        self.assertEqual(prepared["disposable_income"], 9000.0)
        self.assertEqual(prepared["eligible_months"], 9)
        self.assertTrue(prepared["housing_ratio"] < 0.4)

    def test_action_calculation(self):
        action, role, data, expected_state = FLOW[0]
        record = {"id": 1, "state": self.rules.INITIAL_STATE, "payload": self.rules.prepare_create(CREATE_DATA)}
        state, payload, summary = self.rules.apply_action(record, action, data)
        self.assertEqual(state, expected_state)
        self.assertTrue(payload["eligibility"])

    def test_invalid_input(self):
        invalid = dict(CREATE_DATA)
        invalid["program_type"] = 'pause'
        with self.assertRaises(ValidationError):
            self.rules.prepare_create(invalid)
