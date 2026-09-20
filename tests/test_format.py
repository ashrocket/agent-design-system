from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

import jsonschema

from agent_design_system.format import (
    collect_tokens,
    resolve_token_value,
    validate_design_system,
)
from agent_design_system.selectors import select


ROOT = Path(__file__).resolve().parents[1]


class FormatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.system = json.loads(
            (ROOT / "examples/aurora.design-system.json").read_text()
        )

    def test_example_is_valid(self) -> None:
        self.assertEqual(validate_design_system(self.system), [])

    def test_example_conforms_to_public_json_schema(self) -> None:
        schema = json.loads(
            (
                ROOT / "src/agent_design_system/schemas/design-system.schema.json"
            ).read_text()
        )
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(schema).validate(self.system)

    def test_duplicate_rule_ids_are_rejected(self) -> None:
        system = copy.deepcopy(self.system)
        system["rules"].append(copy.deepcopy(system["rules"][0]))
        self.assertIn(
            "duplicate rule id: foreground-color-token", validate_design_system(system)
        )

    def test_decision_requires_always_fallback(self) -> None:
        system = copy.deepcopy(self.system)
        system["rules"][0]["decision"] = system["rules"][0]["decision"][:1]
        errors = validate_design_system(system)
        self.assertTrue(any("always fallback" in error for error in errors))

    def test_document_root_cannot_be_coerced(self) -> None:
        system = copy.deepcopy(self.system)
        system["rules"] = [
            {
                "id": "root-coercion",
                "description": "Invalid root coercion.",
                "rationale": "Root replacement is intentionally unsupported.",
                "selector": "$",
                "assert": {"operator": "type", "value": "string"},
                "severity": "error",
                "decision": [
                    {
                        "when": {"operator": "always"},
                        "action": {
                            "type": "coerce",
                            "strategy": "replace",
                            "value": "fixed",
                        },
                    }
                ],
            }
        ]
        errors = validate_design_system(system)
        self.assertTrue(
            any("cannot coerce the document root" in error for error in errors)
        )

    def test_non_string_selector_is_reported_not_raised(self) -> None:
        system = copy.deepcopy(self.system)
        system["rules"][0]["selector"] = 42
        errors = validate_design_system(system)
        self.assertTrue(any("selector must be a string" in error for error in errors))

    def test_token_aliases_resolve_and_cycles_fail(self) -> None:
        tokens_tree = {
            "color": {
                "base": {"$type": "color", "$value": "#000000", "$description": "Base"},
                "alias": {
                    "$type": "color",
                    "$value": "{color.base}",
                    "$description": "Alias",
                },
            }
        }
        tokens = collect_tokens(tokens_tree)
        self.assertEqual(resolve_token_value("color.alias", tokens), "#000000")
        tokens_tree["color"]["base"]["$value"] = "{color.alias}"
        errors = validate_design_system({**self.system, "tokens": tokens_tree})
        self.assertTrue(any("cycle" in error for error in errors))

    def test_selector_supports_wildcard_and_recursive_descent(self) -> None:
        document = {
            "components": {
                "a": {"style": {"color": "red"}},
                "b": {"style": {"color": "blue"}},
            }
        }
        matches = select(document, "$.components.*.style.color")
        self.assertEqual(
            [match.path for match in matches],
            ["$.components.a.style.color", "$.components.b.style.color"],
        )
        recursive = select(document, "$.components.**.color")
        self.assertEqual([match.value for match in recursive], ["red", "blue"])


if __name__ == "__main__":
    unittest.main()
