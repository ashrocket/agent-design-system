from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jsonschema

from agent_design_system.cli import main
from agent_design_system.format import FormatError, load_json
from agent_design_system.project import check_project

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/assistant-app.project.json"


class ProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        shutil.copy2(EXAMPLE, self.directory / EXAMPLE.name)
        shutil.copy2(
            ROOT / "examples/aurora.design-system.json",
            self.directory / "aurora.design-system.json",
        )
        shutil.copytree(
            ROOT / "examples/assistant-app", self.directory / "assistant-app"
        )
        self.manifest_path = self.directory / EXAMPLE.name
        self.app = self.directory / "assistant-app"

    def report(self, *, expected: str | None = None) -> dict:
        return check_project(self.manifest_path, expected_sha256=expected)

    def _manifest(self, changes: dict) -> None:
        manifest = load_json(self.manifest_path)
        manifest.update(changes)
        self.manifest_path.write_text(json.dumps(manifest))

    def test_fixture_discovers_every_css_and_is_deterministic_read_only(self) -> None:
        before = {
            path.relative_to(self.directory): path.read_bytes()
            for path in self.directory.rglob("*")
            if path.is_file()
        }
        first = self.report()
        second = self.report(expected=first["contract_sha256"])
        self.assertEqual(first["status"], "pass")
        self.assertEqual(second["status"], "pass")
        self.assertEqual(
            [item["path"] for item in first["files"]],
            ["components/button.css", "layout.css"],
        )
        self.assertEqual(first["coverage"]["css_declarations"], 15)
        self.assertEqual(first["coverage"]["governed_declarations"], 11)
        self.assertEqual(first["coverage"]["html_sources"][0]["path"], "index.html")
        self.assertEqual(
            first["coverage"]["html_sources"][0]["stylesheets"],
            ["tokens.css", "layout.css", "components/button.css"],
        )
        self.assertEqual(len(first["coverage"]["html_sources"][0]["sha256"]), 64)
        self.assertEqual(first["files"], second["files"])
        self.assertEqual(
            before,
            {
                path.relative_to(self.directory): path.read_bytes()
                for path in self.directory.rglob("*")
                if path.is_file()
            },
        )

    def test_new_nested_and_uppercase_css_cannot_be_omitted(self) -> None:
        nested = self.app / "new" / "nested"
        nested.mkdir(parents=True)
        path = nested / "drift.CSS"
        path.write_text(".x { color: #123456; }")
        before = path.read_bytes()
        report = self.report()
        self.assertEqual(report["status"], "fail")
        self.assertIn(
            "new/nested/drift.CSS", [item["path"] for item in report["files"]]
        )
        self.assertIn("css-color", [item["code"] for item in report["issues"]])
        self.assertEqual(path.read_bytes(), before)

    def test_unknown_formats_and_assets_fail_with_hashed_inventory(self) -> None:
        samples = {
            "theme.oddstyle": b"button { color: red; }",
            "settings.json": b'{"accent":"red"}',
            "README": b"runtime style instructions",
            "logo.png": b"not approved brand art",
        }
        for name, content in samples.items():
            (self.app / name).write_bytes(content)
        report = self.report()
        self.assertEqual(report["status"], "fail")
        self.assertEqual(
            [item["path"] for item in report["coverage"]["unverified_sources"]],
            ["README", "logo.png", "settings.json", "theme.oddstyle"],
        )
        for item in report["coverage"]["unverified_sources"]:
            self.assertEqual(len(item["sha256"]), 64)
            self.assertEqual(item["bytes"], len(samples[item["path"]]))
            self.assertIn(
                {"path": item["path"], "code": "adapter-required"},
                [
                    {"path": issue["path"], "code": issue["code"]}
                    for issue in report["issues"]
                ],
            )

    def test_system_and_manifest_inside_root_are_trusted_by_exact_path(self) -> None:
        self._manifest({"root": ".", "tokens": "assistant-app/tokens.css"})
        report = self.report()
        self.assertEqual(report["status"], "pass")
        self.assertEqual(
            [
                (item["path"], item["role"])
                for item in report["coverage"]["trusted_sources"]
            ],
            [
                ("assistant-app.project.json", "manifest"),
                ("assistant-app/tokens.css", "tokens"),
                ("aurora.design-system.json", "system"),
            ],
        )

    def test_excessive_source_and_scan_depth_fail_closed(self) -> None:
        oversized = self.app / "oversized.css"
        with oversized.open("wb") as handle:
            handle.truncate(8 * 1024 * 1024 + 1)
        self.assertIn(
            "source-too-large", [item["code"] for item in self.report()["issues"]]
        )
        oversized.unlink()
        huge_asset = self.app / "huge.bin"
        with huge_asset.open("wb") as handle:
            handle.truncate(64 * 1024 * 1024 + 1)
        report = self.report()
        item = report["coverage"]["unverified_sources"][0]
        self.assertEqual(item["path"], "huge.bin")
        self.assertIsNone(item["sha256"])
        self.assertEqual(item["hash_status"], "size-limit")
        huge_asset.unlink()
        with patch("agent_design_system.project.MAX_SCAN_ENTRIES", 2):
            with self.assertRaisesRegex(FormatError, "scan exceeds"):
                self.report()
        directory = self.app
        for _ in range(65):
            directory /= "a"
            directory.mkdir()
        with self.assertRaisesRegex(FormatError, "scan depth"):
            self.report()

    def test_generated_token_parity_and_reference_are_required(self) -> None:
        token_file = self.app / "tokens.css"
        token_file.write_text(token_file.read_text().replace("16px", "17px"))
        self.assertIn(
            "tokens-stale", [item["code"] for item in self.report()["issues"]]
        )
        token_file.unlink()
        self.assertIn(
            "tokens-missing", [item["code"] for item in self.report()["issues"]]
        )
        index = self.app / "index.html"
        index.write_text(
            index.read_text().replace('<link rel="stylesheet" href="tokens.css">', "")
        )
        self.assertIn(
            "tokens-unreferenced", [item["code"] for item in self.report()["issues"]]
        )

    def test_each_html_page_must_reference_generated_tokens(self) -> None:
        second = self.app / "second.html"
        second.write_text(
            '<!doctype html><html><head><link rel="stylesheet" href="layout.css"></head><body></body></html>'
        )
        report = self.report()
        self.assertEqual(report["status"], "fail")
        self.assertIn(
            {
                "path": "second.html",
                "code": "tokens-unreferenced",
                "message": "static HTML page does not reference the generated token stylesheet",
            },
            report["issues"],
        )

    def test_inline_external_and_unsupported_sources_fail_coverage(self) -> None:
        index = self.app / "index.html"
        index.write_text(
            index.read_text().replace(
                "</body>",
                '<div style="color:red" onclick="go()"></div><style>.x{color:red}</style>'
                '<base href="https://example.com/">'
                '<link rel="stylesheet" href="https://example.com/override.css">'
                '<script src="app.js"></script></body>',
            )
        )
        (self.app / "app.tsx").write_text("export const App = () => null")
        (self.app / "icon.svg").write_text("<svg></svg>")
        (self.app / "other.xhtml").write_text(
            "<html><style>button{color:red}</style></html>"
        )
        codes = [item["code"] for item in self.report()["issues"]]
        self.assertIn("html-unverified-attribute", codes)
        self.assertIn("html-unverified-element", codes)
        self.assertIn("stylesheet-unverified", codes)
        self.assertEqual(codes.count("adapter-required"), 3)

    def test_base_rebasing_and_duplicate_link_attributes_fail(self) -> None:
        index = self.app / "index.html"
        original = index.read_text()
        index.write_text(
            original.replace("<head>", '<head><base href="https://example.com/">')
        )
        self.assertIn(
            "html-unverified-element",
            [item["code"] for item in self.report()["issues"]],
        )
        index.write_text(
            original.replace(
                'href="tokens.css"',
                'href="https://example.com/remote.css" href="tokens.css"',
            )
        )
        issues = self.report()["issues"]
        self.assertIn("html-duplicate-attribute", [item["code"] for item in issues])
        self.assertEqual(self.report()["status"], "fail")

    def test_legacy_presentational_attributes_fail(self) -> None:
        index = self.app / "index.html"
        index.write_text(
            index.read_text().replace("<body>", '<body text="#123456" link="red">')
        )
        issues = self.report()["issues"]
        self.assertEqual(self.report()["status"], "fail")
        self.assertEqual(
            sum(item["code"] == "html-unverified-attribute" for item in issues), 2
        )

    def test_exported_context_uses_reviewed_project_gate(self) -> None:
        fingerprint = self.report()["contract_sha256"]
        system = str(self.directory / "aurora.design-system.json")
        arguments = [
            "context",
            system,
            "--project-manifest",
            str(self.manifest_path),
            "--expected-sha256",
            fingerprint,
        ]

        def invoke(*extra: str) -> tuple[int, str]:
            stdout = io.StringIO()
            with (
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                code = main([*arguments, *extra])
            return code, stdout.getvalue()

        code, output = invoke("--format", "json")
        self.assertEqual(code, 0)
        context = json.loads(output)
        self.assertIn("project-check", context["gate"])
        self.assertIn(fingerprint, context["gate"])
        self.assertIn("PATH.css", context["fast_file_check"])
        code, markdown = invoke()
        self.assertEqual(code, 0)
        self.assertIn(context["gate"], markdown)
        self.assertIn("quick single-file diagnostic", markdown)
        arguments[-1] = "0" * 64
        self.assertEqual(invoke()[0], 2)
        arguments = ["context", system, "--project-manifest", str(self.manifest_path)]
        self.assertEqual(invoke()[0], 2)
        arguments = [
            "context",
            str(self.app / "index.html"),
            "--project-manifest",
            str(self.manifest_path),
            "--expected-sha256",
            fingerprint,
        ]
        self.assertEqual(invoke()[0], 2)

    def test_exclusions_are_visible_and_bound_to_reviewed_fingerprint(self) -> None:
        initial = self.report()["contract_sha256"]
        (self.app / "dist").mkdir()
        (self.app / "dist" / "drift.css").write_text(".x{color:red}")
        (self.app / "dist" / "unknown.styler").write_text("color: red")
        self._manifest(
            {
                "exclusions": [
                    {"path": "dist", "reason": "Reviewed generated build output"},
                    {"path": "absent", "reason": "Possible future output"},
                ]
            }
        )
        report = self.report()
        self.assertEqual(report["status"], "pass")
        self.assertNotEqual(report["contract_sha256"], initial)
        self.assertEqual(
            report["coverage"]["exclusions"][0],
            {
                "path": "dist",
                "reason": "Reviewed generated build output",
                "present": True,
                "matched": True,
            },
        )
        self.assertFalse(report["coverage"]["exclusions"][1]["present"])
        self.assertEqual(report["coverage"]["unverified_sources"], [])
        self.assertIn(
            "contract-mismatch",
            [item["code"] for item in self.report(expected=initial)["issues"]],
        )

    def test_malformed_manifest_and_reserved_exclusions_fail_closed(self) -> None:
        original = load_json(self.manifest_path)
        bad = [
            {"root": "../escape"},
            {"root": "/tmp"},
            {"tokens": "../tokens.css"},
            {"system": "../system.json"},
            {"exclusions": [{"path": "tokens.css", "reason": "hide"}]},
            {
                "exclusions": [
                    {"path": "components", "reason": "one"},
                    {"path": "components/button.css", "reason": "two"},
                ]
            },
            {"exclusions": [{"path": "dist", "reason": ""}]},
        ]
        for change in bad:
            with self.subTest(change=change):
                self.manifest_path.write_text(json.dumps({**original, **change}))
                with self.assertRaises(FormatError):
                    self.report()

    def test_symlinks_special_files_and_invalid_utf8_fail(self) -> None:
        (self.app / "escape").symlink_to(self.directory, target_is_directory=True)
        with self.assertRaisesRegex(FormatError, "symlink"):
            self.report()
        (self.app / "escape").unlink()
        (self.app / "escape.css").symlink_to(
            self.directory / "aurora.design-system.json"
        )
        with self.assertRaisesRegex(FormatError, "symlink"):
            self.report()
        (self.app / "escape.css").unlink()
        if hasattr(os, "mkfifo"):
            os.mkfifo(self.app / "pipe.css")
            with self.assertRaisesRegex(FormatError, "special file"):
                self.report()
            (self.app / "pipe.css").unlink()
        (self.app / "invalid.css").write_bytes(b"\xff")
        self.assertIn(
            "source-undecodable", [item["code"] for item in self.report()["issues"]]
        )

    def test_zero_governed_declarations_cannot_pass(self) -> None:
        for path in self.app.rglob("*.css"):
            if path.name != "tokens.css":
                path.write_text(".x { display: block; }")
        self.assertIn(
            "css-empty-coverage", [item["code"] for item in self.report()["issues"]]
        )

    def test_public_manifest_schema_and_cli_exit_codes(self) -> None:
        schema = load_json(
            ROOT / "src/agent_design_system/schemas/project-manifest.schema.json"
        )
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(load_json(self.manifest_path), schema)
        fingerprint = self.report()["contract_sha256"]

        def invoke(*args: str) -> tuple[int, str]:
            output = io.StringIO()
            with (
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                code = main(
                    [
                        "project-check",
                        str(self.manifest_path),
                        "--format",
                        "json",
                        *args,
                    ]
                )
            return code, output.getvalue()

        code, output = invoke("--expected-sha256", fingerprint)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["status"], "pass")
        self.assertEqual(invoke("--expected-sha256", "0" * 64)[0], 1)
        self._manifest({"root": "../escape"})
        self.assertEqual(invoke()[0], 2)


if __name__ == "__main__":
    unittest.main()
