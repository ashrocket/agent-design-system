from __future__ import annotations

import json
import copy
import unittest
from pathlib import Path

from agent_design_system.enforce import enforce
from agent_design_system.reporters import render_sarif, should_fail


ROOT = Path(__file__).resolve().parents[1]


class EnforcementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.system = json.loads(
            (ROOT / "examples/aurora.design-system.json").read_text()
        )

    def test_valid_document_passes(self) -> None:
        document = json.loads((ROOT / "examples/button.valid.json").read_text())
        result = enforce(self.system, document, source="button.valid.json")
        self.assertEqual(result.violations, [])
        self.assertFalse(should_fail(result, "error"))

    def test_invalid_document_has_deterministic_decisions(self) -> None:
        document = json.loads((ROOT / "examples/button.invalid.json").read_text())
        result = enforce(self.system, document, source="button.invalid.json")
        self.assertEqual(len(result.violations), 4)
        replacements = {
            violation.rule_id: violation.replacement for violation in result.violations
        }
        self.assertEqual(replacements["foreground-color-token"], "{color.text.primary}")
        self.assertEqual(
            replacements["background-color-token"], "{color.action.primary}"
        )
        self.assertEqual(replacements["spacing-token"], "{space.4}")
        self.assertIsNone(replacements["accessible-name"])
        self.assertTrue(should_fail(result, "error"))

    def test_fix_applies_only_coercions(self) -> None:
        document = json.loads((ROOT / "examples/button.invalid.json").read_text())
        fixed = enforce(self.system, document, apply_fixes=True)
        style = fixed.document["components"]["primaryButton"]["style"]
        self.assertEqual(style["color"], "{color.text.primary}")
        self.assertEqual(style["background"], "{color.action.primary}")
        self.assertEqual(style["padding"], "{space.4}")
        second_pass = enforce(self.system, fixed.document)
        self.assertEqual(
            [violation.rule_id for violation in second_pass.violations],
            ["accessible-name"],
        )
        self.assertFalse(should_fail(second_pass, "error"))

    def test_missing_nested_property_is_reported_from_parent(self) -> None:
        document = json.loads((ROOT / "examples/button.valid.json").read_text())
        del document["components"]["primaryButton"]["accessibility"]
        result = enforce(self.system, document)
        self.assertEqual(
            [violation.rule_id for violation in result.violations],
            ["accessible-name-present"],
        )
        self.assertEqual(result.violations[0].path, "$.components.primaryButton")

    def test_sarif_is_machine_readable(self) -> None:
        document = json.loads((ROOT / "examples/button.invalid.json").read_text())
        result = enforce(self.system, document, source="examples/button.invalid.json")
        sarif = json.loads(render_sarif(result))
        self.assertEqual(sarif["version"], "2.1.0")
        self.assertEqual(len(sarif["runs"][0]["results"]), 4)

    def test_json_equality_does_not_treat_boolean_as_number(self) -> None:
        system = copy.deepcopy(self.system)
        system["rules"] = [
            {
                "id": "exact-number",
                "description": "Value must equal numeric one.",
                "rationale": "JSON booleans and numbers are distinct types.",
                "selector": "$.value",
                "assert": {"operator": "equals", "value": 1},
                "severity": "error",
                "decision": [
                    {"when": {"operator": "always"}, "action": {"type": "report"}}
                ],
            }
        ]
        result = enforce(system, {"value": True})
        self.assertEqual(len(result.violations), 1)

    def test_ignore_is_logged_but_never_fails(self) -> None:
        system = copy.deepcopy(self.system)
        system["rules"] = [
            {
                "id": "documented-exception",
                "description": "Demonstrate an explicit exception.",
                "rationale": "Ignored violations remain auditable.",
                "selector": "$.value",
                "assert": {"operator": "equals", "value": "allowed"},
                "severity": "error",
                "decision": [
                    {"when": {"operator": "always"}, "action": {"type": "ignore"}}
                ],
            }
        ]
        result = enforce(system, {"value": "exception"})
        self.assertEqual(result.violations[0].disposition, "ignore")
        self.assertFalse(should_fail(result, "info"))

    def test_reject_always_fails(self) -> None:
        system = copy.deepcopy(self.system)
        system["rules"] = [
            {
                "id": "hard-stop",
                "description": "Reject unsafe values.",
                "rationale": "Some values cannot be accepted at any threshold.",
                "selector": "$.value",
                "assert": {"operator": "equals", "value": "safe"},
                "severity": "info",
                "decision": [
                    {"when": {"operator": "always"}, "action": {"type": "reject"}}
                ],
            }
        ]
        result = enforce(system, {"value": "unsafe"})
        self.assertTrue(should_fail(result, "never"))


if __name__ == "__main__":
    unittest.main()
