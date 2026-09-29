from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from agent_design_system.cli import main
from agent_design_system.enforce import enforce
from agent_design_system.format import FormatError, load_json, validate_design_system
from agent_design_system.selectors import select

ROOT = Path(__file__).resolve().parents[1]


class SafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.system = load_json(ROOT / "examples/aurora.design-system.json")
        self.system["rules"] = [
            {
                "id": "exact-value",
                "description": "Use the required value.",
                "rationale": "Repairs must actually satisfy the contract.",
                "selector": "$.value",
                "assert": {"operator": "equals", "value": "right"},
                "severity": "error",
                "decision": [
                    {
                        "when": {"operator": "always"},
                        "action": {
                            "type": "coerce",
                            "strategy": "replace",
                            "value": "right",
                        },
                    }
                ],
            }
        ]
        self.system_path = self.directory / "system.json"
        self.system_path.write_text(json.dumps(self.system))
        self.input = self.directory / "input.json"
        self.input.write_text('{"value": "wrong"}\n')

    def invoke(self, *args: str) -> int:
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return main(list(args))

    def test_check_log_cannot_alias_either_input(self) -> None:
        for path in (self.input, self.system_path):
            with self.subTest(path=path):
                before = path.read_bytes()
                self.assertEqual(
                    self.invoke(
                        "check",
                        str(self.system_path),
                        str(self.input),
                        "--log",
                        str(path),
                    ),
                    2,
                )
                self.assertEqual(path.read_bytes(), before)

    def test_fix_protects_inputs_and_symlink_hardlink_aliases(self) -> None:
        symlink = self.directory / "symlink.json"
        symlink.symlink_to(self.input)
        hardlink = self.directory / "hardlink.json"
        os.link(self.input, hardlink)
        for destination in (self.input, self.system_path, symlink, hardlink):
            with self.subTest(destination=destination):
                original = self.input.read_bytes(), self.system_path.read_bytes()
                self.assertEqual(
                    self.invoke(
                        "fix",
                        str(self.system_path),
                        str(self.input),
                        "--output",
                        str(destination),
                    ),
                    2,
                )
                self.assertEqual(
                    (self.input.read_bytes(), self.system_path.read_bytes()), original
                )

    def test_fix_log_cannot_alias_output_before_writing(self) -> None:
        output = self.directory / "fixed.json"
        self.assertEqual(
            self.invoke(
                "fix",
                str(self.system_path),
                str(self.input),
                "--output",
                str(output),
                "--log",
                str(output),
            ),
            2,
        )
        self.assertFalse(output.exists())

    def test_fix_write_is_explicit_and_rechecked(self) -> None:
        self.assertEqual(
            self.invoke("fix", str(self.system_path), str(self.input), "--write"), 0
        )
        self.assertEqual(load_json(self.input), {"value": "right"})

    def test_reject_still_fails_when_another_rule_repairs_it(self) -> None:
        rejection = copy.deepcopy(self.system["rules"][0])
        rejection["id"] = "hard-stop"
        rejection["decision"][0]["action"] = {"type": "reject"}
        self.system["rules"].insert(0, rejection)
        self.system_path.write_text(json.dumps(self.system))
        self.assertEqual(
            self.invoke(
                "fix",
                str(self.system_path),
                str(self.input),
                "--write",
                "--fail-on",
                "never",
            ),
            1,
        )
        self.assertEqual(load_json(self.input), {"value": "right"})

    def test_symlink_write_and_cycle_fail_cleanly(self) -> None:
        link = self.directory / "link.json"
        link.symlink_to(self.input)
        self.assertEqual(
            self.invoke("fix", str(self.system_path), str(link), "--write"), 2
        )
        self.assertTrue(link.is_symlink())
        cycle = self.directory / "cycle.json"
        cycle.symlink_to(cycle)
        self.assertEqual(
            self.invoke(
                "fix", str(self.system_path), str(self.input), "--output", str(cycle)
            ),
            2,
        )

    def test_bad_repair_and_last_rule_conflict_do_not_pass(self) -> None:
        for conflict in (False, True):
            with self.subTest(conflict=conflict):
                system = copy.deepcopy(self.system)
                if conflict:
                    second = copy.deepcopy(system["rules"][0])
                    second["id"] = "conflicting-value"
                    second["assert"]["value"] = "other"
                    second["decision"][0]["action"]["value"] = "other"
                    system["rules"].append(second)
                else:
                    system["rules"][0]["decision"][0]["action"]["value"] = "still wrong"
                self.system_path.write_text(json.dumps(system))
                output = self.directory / "fixed.json"
                self.assertEqual(
                    self.invoke(
                        "fix",
                        str(self.system_path),
                        str(self.input),
                        "--output",
                        str(output),
                    ),
                    1,
                )
                self.assertEqual(
                    self.invoke("check", str(self.system_path), str(output)), 1
                )

    def test_null_replacement_and_noop_changed(self) -> None:
        self.system["rules"][0]["assert"]["value"] = None
        self.system["rules"][0]["decision"][0]["action"]["value"] = None
        result = enforce(self.system, {"value": "wrong"}, apply_fixes=True)
        self.assertEqual(result.document, {"value": None})
        self.assertEqual(result.violations[0].to_dict()["replacement"], None)
        self.system["rules"][0]["decision"][0]["action"]["value"] = "wrong"
        self.assertFalse(
            enforce(self.system, {"value": "wrong"}, apply_fixes=True).changed
        )

    def test_ancestor_descendant_repair_fails_without_output(self) -> None:
        self.system["rules"][0]["selector"] = "$.value.child"
        parent = copy.deepcopy(self.system["rules"][0])
        parent["id"] = "parent-value"
        parent["selector"] = "$.value"
        self.system["rules"].insert(0, parent)
        self.system_path.write_text(json.dumps(self.system))
        self.input.write_text('{"value": {"child": "wrong"}}')
        output = self.directory / "fixed.json"
        self.assertEqual(
            self.invoke(
                "fix", str(self.system_path), str(self.input), "--output", str(output)
            ),
            2,
        )
        self.assertFalse(output.exists())

    def test_large_adjacent_numbers_are_distinct_and_huge_numbers_work(self) -> None:
        for value in (9007199254740993, 10**400):
            with self.subTest(value=value):
                self.system["rules"][0]["assert"]["value"] = value
                self.assertTrue(enforce(self.system, {"value": value - 1}).violations)
                self.assertFalse(enforce(self.system, {"value": value}).violations)

    def test_recursive_selection_is_unique_and_root_coercion_invalid(self) -> None:
        self.assertEqual(len(select({"x": {"y": "wrong"}}, "$.**.**.y")), 1)
        for selector in ("$.**", "$.**.**"):
            self.system["rules"][0]["selector"] = selector
            self.assertTrue(validate_design_system(self.system))

    def test_malformed_fields_are_errors_not_crashes(self) -> None:
        for field, value in (
            ("severity", []),
            ("assert", {"operator": []}),
            ("decision", [{"when": [], "action": {"type": []}}]),
        ):
            with self.subTest(field=field):
                system = copy.deepcopy(self.system)
                system["rules"][0][field] = value
                self.assertTrue(validate_design_system(system))

    def test_strict_json_rejects_nonfinite_and_duplicate_keys(self) -> None:
        for content in ('{"x":NaN}', '{"x":1e400}', '{"x":1,"x":2}'):
            with self.subTest(content=content):
                self.input.write_text(content)
                with self.assertRaises(FormatError):
                    load_json(self.input)

    def test_ambiguous_token_keys_are_invalid(self) -> None:
        for key in ("color.text", "color text", "", "{}"):
            with self.subTest(key=key):
                self.system["tokens"] = {
                    key: {
                        "$type": "color",
                        "$value": "#000000",
                        "$description": "Invalid key",
                    }
                }
                self.assertTrue(validate_design_system(self.system))


if __name__ == "__main__":
    unittest.main()
