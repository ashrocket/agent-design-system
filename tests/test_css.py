from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

import jsonschema

from agent_design_system.cli import main
from agent_design_system.css import (
    agent_context,
    enforce_css,
    load_css_policy,
    render_context,
    render_tokens,
)
from agent_design_system.format import FormatError, load_json
from agent_design_system.reporters import render_sarif, render_text, should_fail

ROOT = Path(__file__).resolve().parents[1]


class CSSTests(unittest.TestCase):
    def setUp(self) -> None:
        self.system = load_json(ROOT / "examples/aurora.design-system.json")

    def test_real_valid_css_passes_with_coverage(self) -> None:
        source = (ROOT / "examples/button.valid.css").read_text()
        result = enforce_css(self.system, source, source="button.css")
        self.assertFalse(result.violations)
        self.assertEqual(
            result.coverage,
            {
                "adapter": "css",
                "declarations": 7,
                "governed": 5,
                "unrestricted": 2,
                "selector_classes": ["primary-button"],
            },
        )
        self.assertIn("7 CSS declaration", render_text(result))

    def test_context_examples_match_actual_enforcement(self) -> None:
        context = agent_context(self.system, "system.json")
        for example in context["examples"]:
            with self.subTest(example=example):
                result = enforce_css(self.system, example["source"])
                self.assertEqual(bool(result.violations), example["expected"] != "pass")
        self.assertIn(
            "Passing and failing examples", render_context(self.system, "system.json")
        )

    def test_schema_integer_maximum_and_embedded_policy_agree(self) -> None:
        schema_dir = ROOT / "src/agent_design_system/schemas"
        policy_schema = load_json(schema_dir / "css-policy.schema.json")
        embedded = load_json(schema_dir / "design-system.schema.json")["$defs"][
            "cssPolicy"
        ]
        structural = {
            key: value
            for key, value in policy_schema.items()
            if key not in {"$schema", "$id", "title"}
        }
        self.assertEqual(embedded, structural)
        self.system["extensions"]["css"]["properties"]["color"]["max_values"] = 1.0
        jsonschema.validate(self.system["extensions"]["css"], policy_schema)
        load_css_policy(self.system)
        self.assertFalse(
            enforce_css(self.system, ".x{color:var(--color-text-primary)}").violations
        )

    def test_excessive_grouping_nesting_is_a_controlled_error(self) -> None:
        source = (
            "@media all {" * 1200 + ".x{color:var(--color-text-primary)}" + "}" * 1200
        )
        with self.assertRaisesRegex(FormatError, "nesting exceeds"):
            enforce_css(self.system, source)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "deep.css"
            path.write_text(source)
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                code = main(
                    [
                        "check",
                        str(ROOT / "examples/aurora.design-system.json"),
                        str(path),
                        "--input-format",
                        "css",
                    ]
                )
            self.assertEqual(code, 2)
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_sarif_declares_codepoint_columns_for_unicode_source(self) -> None:
        result = enforce_css(
            self.system, "/* 🙂 */ .x { color: red; }", source="emoji.css"
        )
        sarif = json.loads(render_sarif(result))
        self.assertEqual(sarif["runs"][0]["columnKind"], "unicodeCodePoints")
        self.assertEqual(result.violations[0].column, 21)

    def test_real_drift_is_repaired_rechecked_and_idempotent(self) -> None:
        source = (ROOT / "examples/button.drift.css").read_text()
        check = enforce_css(self.system, source)
        fixed = enforce_css(self.system, source, apply_fixes=True)
        self.assertEqual(check.violations, fixed.violations)
        self.assertEqual(
            [v.rule_id for v in fixed.violations],
            ["css-color", "css-background-color", "css-padding", "css-border-radius"],
        )
        self.assertTrue(should_fail(check, "error"))
        self.assertFalse(check.changed)
        self.assertEqual(check.document, source)
        self.assertTrue(fixed.changed)
        second = enforce_css(self.system, fixed.document, apply_fixes=True)
        self.assertFalse(second.violations)
        self.assertFalse(second.changed)
        self.assertEqual(second.document, fixed.document)

    def test_source_locations_in_all_report_formats(self) -> None:
        result = enforce_css(
            self.system, "/* hello */\n.x {\n  color: red;\n}\n", source="test.css"
        )
        violation = result.violations[0]
        self.assertEqual((violation.line, violation.column), (3, 10))
        self.assertEqual(
            result.to_dict()["violations"][0]["location"], {"line": 3, "column": 10}
        )
        self.assertIn("test.css:3:10", render_text(result))
        sarif = json.loads(render_sarif(result))
        self.assertEqual(
            sarif["runs"][0]["results"][0]["locations"][0]["physicalLocation"][
                "region"
            ],
            {"startLine": 3, "startColumn": 10},
        )

    def test_known_bypasses_are_rejected_without_repairs(self) -> None:
        declarations = [
            "color: red",
            "color: rgb(30,41,59)",
            "color: var(--missing)",
            "color: var(--color-text-primary-extra)",
            "color: var(--color-text)",
            "color: var(--color-text-primary, red)",
            "color: var(--color-action-primary)",
            "color: var(--color-text-primary) !important",
            "color: VAR(--color-text-primary)",
            "background: linear-gradient(red, blue)",
            "padding: calc(var(--space-3) + 1px)",
            "padding: var(--space-3) 17px",
            "padding: 1rem",
            "padding: 12px / 16px",
            "padding: 12px 12px 12px 12px 12px",
            "--color-text-primary: red",
            "--other: var(--color-text-primary)",
            "all: unset",
            "border: 1px solid red",
            "font: 14px serif",
        ]
        for declaration in declarations:
            with self.subTest(declaration=declaration):
                source = ".x { " + declaration + "; }"
                result = enforce_css(self.system, source, apply_fixes=True)
                self.assertTrue(should_fail(result, "never"))
                self.assertEqual(result.document, source)
                self.assertFalse(result.changed)

    def test_case_variants_and_duplicate_declarations_are_inspected(self) -> None:
        result = enforce_css(
            self.system,
            ".x { COLOR: var(--color-text-primary); color: red; color: var(--color-text-primary); }",
        )
        self.assertEqual(result.coverage["declarations"], 3)
        self.assertEqual(len(result.violations), 1)

    def test_grouping_at_rules_and_multiple_blocks(self) -> None:
        source = '@layer components { @supports (display: grid) { @media (width > 40rem) { .x[data-kind="a;b"] { padding: var(--space-3) var(--space-4); } } } } .y { color: var(--color-text-primary); }'
        result = enforce_css(self.system, source)
        self.assertFalse(result.violations)
        self.assertEqual(result.coverage["declarations"], 2)

    def test_only_simple_unconditional_class_selectors_are_indexed(self) -> None:
        source = """/* .comment { color: red; } */
.alpha, .beta { color: var(--color-text-primary); }
.empty { }
.hover:hover { color: var(--color-text-primary); }
.other:not(.negative) { color: var(--color-text-primary); }
:is(.other, .fragment, .third) { color: var(--color-text-primary); }
@supports selector(.conditional) { .conditional { color: var(--color-text-primary); } }
@media screen { .responsive { color: var(--color-text-primary); } }
"""
        result = enforce_css(self.system, source)
        self.assertFalse(result.violations)
        self.assertEqual(result.coverage["selector_classes"], ["alpha", "beta"])

    def test_comments_strings_and_newlines_survive_exact_fixes(self) -> None:
        source = '/* untouched */\r\n.x { content: "a;{}:/*not-comment*/"; color: /* before */ #1E293B /* after */; padding: 12.0PX 16px; }\r\n'
        result = enforce_css(self.system, source, apply_fixes=True)
        expected = source.replace("#1E293B", "var(--color-text-primary)").replace(
            "12.0PX 16px", "var(--space-3) var(--space-4)"
        )
        self.assertEqual(result.document, expected)
        self.assertFalse(enforce_css(self.system, result.document).violations)

    def test_internal_comments_and_spacing_survive_multivalue_repair(self) -> None:
        source = '.x { padding: 12px\t/* retain this */\n 16px; content: "Hello!"; }'
        result = enforce_css(self.system, source, apply_fixes=True)
        expected = source.replace("12px", "var(--space-3)").replace(
            "16px", "var(--space-4)"
        )
        self.assertEqual(result.document, expected)
        self.assertFalse(enforce_css(self.system, result.document).violations)

    def test_typed_token_units_are_validated(self) -> None:
        for token_type, value in (
            ("dimension", "12"),
            ("dimension", "12ms"),
            ("dimension", "12qu"),
            ("duration", "12px"),
            ("duration", "12"),
        ):
            with (
                self.subTest(token_type=token_type, value=value),
                self.assertRaises(FormatError),
            ):
                system = copy.deepcopy(self.system)
                system["tokens"]["space"]["3"]["$type"] = token_type
                system["tokens"]["space"]["3"]["$value"] = value
                render_tokens(system)

    def test_literal_policy_rejects_unbalanced_syntax(self) -> None:
        for literal in ("bad(", '"unterminated', "x)"):
            with self.subTest(literal=literal), self.assertRaises(FormatError):
                system = copy.deepcopy(self.system)
                system["extensions"]["css"]["properties"]["color"]["literals"] = [
                    literal
                ]
                load_css_policy(system)

    def test_malformed_unsupported_or_partial_css_does_not_pass(self) -> None:
        sources = [
            "/* unclosed",
            '.x { content: "unclosed; }',
            ".x { color: var(--color-text-primary);",
            ".x { color var(--color-text-primary); }",
            ".x { color:; }",
            ".x { color: var(--color-text-primary]; }",
            ".x { color: var(--color-text-primary; }",
            ".x { color: var(--color-text-primary); }}",
            ".x { color: var(--color-text-primary); } trailing",
            '@import "arbitrary.css";',
            '@IMPORT url("arbitrary.css");',
            "@font-face { font-family: whatever; }",
            '@property --color-text-primary { syntax: "*"; }',
            ".x { & .y { color: var(--color-text-primary); } }",
            ".x { @media (width > 1px) { color: var(--color-text-primary); } }",
            r".x { co\6cor: red; }",
            r".x { color: v\61r(--color-text-primary); }",
            ".x { co/**/lor: red; }",
            '.x { content: "line\nline"; }',
            ".x { color\u00a0: var(--color-text-primary); }",
        ]
        for source in sources:
            with self.subTest(source=source), self.assertRaises(FormatError):
                enforce_css(self.system, source)

    def test_empty_coverage_rejects(self) -> None:
        for source in (
            "",
            "/* only comments */",
            ".x {}",
            "@media (width > 1px) { .x {} }",
        ):
            result = enforce_css(self.system, source)
            self.assertEqual(result.violations[0].rule_id, "css-empty-coverage")
            self.assertTrue(should_fail(result, "never"))

    def test_approved_literal_exceptions_and_mixed_multivalue(self) -> None:
        result = enforce_css(
            self.system,
            ".x { padding: 0 var(--space-3) 16px; margin: 0 auto; }",
            apply_fixes=True,
        )
        self.assertEqual(len(result.violations), 1)
        self.assertIn("padding: 0 var(--space-3) var(--space-4)", result.document)
        self.assertFalse(enforce_css(self.system, result.document).violations)

    def test_ambiguous_exact_values_need_an_explicit_choice(self) -> None:
        self.system["tokens"]["color"]["text"]["alternate"] = {
            "$type": "color",
            "$value": "#1e293b",
            "$description": "Different semantic role, same current value.",
        }
        self.system["extensions"]["css"]["bindings"]["color.text.alternate"] = (
            "--alternate"
        )
        self.system["extensions"]["css"]["properties"]["color"]["tokens"].append(
            "color.text.alternate"
        )
        source = ".x { color: #1e293b; }"
        result = enforce_css(self.system, source, apply_fixes=True)
        self.assertEqual(result.violations[0].disposition, "reject")
        self.assertEqual(result.document, source)

    def test_policy_rejects_unknown_fields_duplicate_bindings_and_reset(self) -> None:
        bad_policies = []
        for key, value in (
            ("version", "99"),
            ("bindings", []),
            ("properties", []),
            ("unrestricted_properties", ["all"]),
            ("unrestricted_properties", ["display", "display"]),
            ("unknown", True),
        ):
            policy = copy.deepcopy(self.system["extensions"]["css"])
            policy[key] = value
            bad_policies.append(policy)
        duplicate = copy.deepcopy(self.system["extensions"]["css"])
        duplicate["bindings"]["space.3"] = "--space-4"
        bad_policies.append(duplicate)
        for rule in (
            {"tokens": ["missing"]},
            {"tokens": []},
            {"tokens": ["space.3"], "max_values": True},
            {"tokens": ["space.3"], "max_values": 5},
            {"tokens": ["space.3"], "literals": ["var(--unapproved)"]},
        ):
            policy = copy.deepcopy(self.system["extensions"]["css"])
            policy["properties"]["padding"] = rule
            bad_policies.append(policy)
        reset = copy.deepcopy(self.system["extensions"]["css"])
        reset["properties"]["all"] = {"tokens": ["space.3"]}
        bad_policies.append(reset)
        for policy in bad_policies:
            with self.subTest(policy=policy), self.assertRaises(FormatError):
                load_css_policy({**self.system, "extensions": {"css": policy}})

    def test_token_generation_and_context_are_stable_and_share_identity(self) -> None:
        tokens = render_tokens(self.system)
        context = agent_context(self.system, "my system.json")
        self.assertIn("--space-4: 16px;", tokens)
        self.assertIn(context["contract_sha256"], tokens)
        self.assertIn("'my system.json'", context["gate"])
        self.assertEqual(
            tokens, render_tokens(json.loads(json.dumps(self.system, sort_keys=True)))
        )
        self.assertIn(
            context["contract_sha256"], render_context(self.system, "my system.json")
        )
        self.assertIn("inline styles", render_context(self.system, "my system.json"))
        modified = copy.deepcopy(self.system)
        modified["tokens"]["space"]["4"]["$value"] = 20
        self.assertNotEqual(
            context["contract_sha256"],
            agent_context(modified, "my system.json")["contract_sha256"],
        )

    def test_token_serialization_rejects_source_injection(self) -> None:
        for value in (
            "red; color: blue",
            "var(--wrong)",
            'url("unterminated)',
            "not-a-color",
            True,
            [1, 2],
        ):
            with self.subTest(value=value), self.assertRaises(FormatError):
                system = copy.deepcopy(self.system)
                system["tokens"]["color"]["text"]["primary"]["$value"] = value
                render_tokens(system)

    def test_css_policy_has_public_structural_schema(self) -> None:
        path = ROOT / "src/agent_design_system/schemas/css-policy.schema.json"
        schema = load_json(path)
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(self.system["extensions"]["css"], schema)

    def test_cli_check_fix_exports_and_error_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            system = directory / "system.json"
            system.write_text(json.dumps(self.system))
            source = directory / "source.css"
            source.write_bytes(b".x { color: #1e293b; }\r\n")
            before = source.read_bytes()
            output = directory / "fixed.css"

            def invoke(*args: str) -> int:
                with (
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    return main(list(args))

            common = (str(system), str(source), "--input-format", "css")
            self.assertEqual(invoke("check", *common), 1)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(invoke("fix", *common, "--output", str(output)), 0)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(
                output.read_bytes(), b".x { color: var(--color-text-primary); }\r\n"
            )
            self.assertEqual(
                invoke("check", str(system), str(output), "--input-format", "css"), 0
            )
            self.assertEqual(invoke("fix", *common, "--output", str(source)), 2)
            self.assertEqual(invoke("check", *common, "--log", str(source)), 2)
            for command in ("tokens", "context"):
                self.assertEqual(
                    invoke(command, str(system), "--output", str(system)), 2
                )
                self.assertEqual(
                    invoke(command, str(system), "--output", str(directory / command)),
                    0,
                )
            source.write_text(".x { color: #123456; }")
            self.assertEqual(invoke("fix", *common, "--output", str(output)), 1)
            source.write_text(".x { color: var(--color-text-primary); } trailing")
            self.assertEqual(invoke("check", *common), 2)


if __name__ == "__main__":
    unittest.main()
