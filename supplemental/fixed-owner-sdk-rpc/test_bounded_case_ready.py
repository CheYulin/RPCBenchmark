import ast
import copy
import json
from pathlib import Path
import tempfile
from fixed_case_fixture import create_case
import unittest

from validate_bounded_arrival import ADMISSION_PROFILE

HERE = Path(__file__).resolve().parent
with tempfile.TemporaryDirectory(prefix='bounded-ready-fixture-') as directory:
    case = create_case(Path(directory), mode='off', smoke=True)
    ORIGINAL = json.loads((case['case']/'ready.json').read_text())


def validate_ready(value, metric, arrivals):
    tree = ast.parse((HERE / 'validate_bounded_case.py').read_text())
    names = {'need', 'exact_fields', '_ready_admission_profile'}
    definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = dict(ADMISSION_PROFILE=ADMISSION_PROFILE)
    exec(compile(ast.Module(body=definitions, type_ignores=[]), 'actual_adapter_ready_functions', 'exec'), namespace)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'validate_case')
    shape = next(node for node in ast.walk(function) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == 'exact_fields'
                 and isinstance(node.args[0], ast.Name) and node.args[0].id == 'ready')
    namespace['exact_fields'](value, ast.literal_eval(shape.args[1]), 'ready')
    if '_ready_admission_profile' in namespace:
        assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id == '_ready_admission_profile' for node in ast.walk(function))
        namespace['_ready_admission_profile'](value, metric, arrivals)


class ReadySchema(unittest.TestCase):
    def setUp(self):
        self.ready = copy.deepcopy(ORIGINAL)
        self.ready['admission_profile'] = copy.deepcopy(ADMISSION_PROFILE)
        self.metric = dict(admission_profile=copy.deepcopy(ADMISSION_PROFILE))
        self.arrivals = dict(profile=dict(admission_profile=copy.deepcopy(ADMISSION_PROFILE)))

    def test_new_producer_shape_qualifies(self):
        validate_ready(self.ready, self.metric, self.arrivals)

    def test_old_ready_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_ready(ORIGINAL, self.metric, self.arrivals)

    def test_boolean_cannot_impersonate_integer(self):
        self.ready['admission_profile']['soft_late_budget_numerator'] = True
        with self.assertRaises(ValueError):
            validate_ready(self.ready, self.metric, self.arrivals)

    def test_profile_protocol_must_match(self):
        self.ready['admission_profile']['protocol'] = 'old-max1ms'
        with self.assertRaises(ValueError):
            validate_ready(self.ready, self.metric, self.arrivals)

    def test_validated_profile_cannot_drift(self):
        self.arrivals['profile']['admission_profile']['owner_period_ns'] -= 1
        with self.assertRaises(ValueError):
            validate_ready(self.ready, self.metric, self.arrivals)


if __name__ == '__main__':
    unittest.main()
