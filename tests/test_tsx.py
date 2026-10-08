from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_design_system.format import FormatError, load_json
from agent_design_system.project import _tsx_import_path, check_project
from agent_design_system.tsx import PARSER_SCRIPT, AdapterDriftError, inspect_static_tsx

ROOT = Path(__file__).resolve().parents[1]


class StaticTSXTests(unittest.TestCase):
    def test_relative_css_imports_cannot_escape_or_ambiguously_resolve(self) -> None:
        self.assertEqual(
            _tsx_import_path("components/App.tsx", "../layout.css"), "layout.css"
        )
        self.assertEqual(
            _tsx_import_path("components/App.tsx", "./button.css"),
            "components/button.css",
        )
        for specifier in (
            "../../outside.css",
            "https://x/a.css",
            "./x.css?q=1",
            "./x\\y.css",
            ".//x.css",
        ):
            with self.subTest(specifier=specifier):
                with self.assertRaises(FormatError):
                    _tsx_import_path("components/App.tsx", specifier)

    def test_mismatched_or_malformed_sidecar_output_fails_closed(self) -> None:
        source = "export default function App(){return <main className='stage'/>}"
        digest = hashlib.sha256(source.encode()).hexdigest()
        valid = {
            "protocol": "adsf-static-tsx-v1",
            "parser_version": "6.0.3",
            "source_sha256": digest,
            "imports": [],
            "classes": [{"name": "stage", "line": 1, "column": 1}],
            "issues": [],
        }
        with (
            patch("agent_design_system.tsx.subprocess.run") as run,
            patch("agent_design_system.tsx._node_binary", return_value="/trusted/node"),
            patch("agent_design_system.tsx._verify_installed_parser"),
        ):
            forged = dict(valid, source_sha256="0" * 64)
            run.return_value = subprocess.CompletedProcess(
                [], 0, json.dumps(forged), ""
            )
            with self.assertRaisesRegex(FormatError, "invalid envelope"):
                inspect_static_tsx("App.tsx", source, digest)
            run.return_value = subprocess.CompletedProcess([], 0, "not JSON", "")
            with self.assertRaisesRegex(FormatError, "invalid JSON"):
                inspect_static_tsx("App.tsx", source, digest)
            run.side_effect = FileNotFoundError("node")
            with self.assertRaisesRegex(FormatError, "unavailable"):
                inspect_static_tsx("App.tsx", source, digest)

    def test_modified_installed_parser_fails_before_launch(self) -> None:
        source = "export default function App(){return <main className='stage'/>}"
        digest = hashlib.sha256(source.encode()).hexdigest()
        with (
            patch(
                "agent_design_system.tsx.PARSER_SCRIPT",
                PARSER_SCRIPT.parent / "missing.js",
            ),
            patch("agent_design_system.tsx.subprocess.run") as run,
        ):
            with self.assertRaisesRegex(AdapterDriftError, "installed TypeScript"):
                inspect_static_tsx("App.tsx", source, digest)
            run.assert_not_called()

    def test_opt_in_without_coverage_or_parser_cannot_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest = directory / "assistant-app.project.json"
            shutil.copy2(ROOT / "examples/assistant-app.project.json", manifest)
            shutil.copy2(
                ROOT / "examples/aurora.design-system.json",
                directory / "aurora.design-system.json",
            )
            app = directory / "assistant-app"
            shutil.copytree(ROOT / "examples/assistant-app", app)
            data = load_json(manifest)
            data["tsx"] = {
                "profile": "static-jsx-v1",
                "adapter_sha256": "a" * 64,
                "lock_sha256": "b" * 64,
            }
            manifest.write_text(json.dumps(data))
            self.assertIn(
                "tsx-empty-coverage",
                [item["code"] for item in check_project(manifest)["issues"]],
            )
            (app / "App.tsx").write_text(
                "export default function App(){return <main/>}"
            )
            with patch(
                "agent_design_system.project.inspect_static_tsx",
                side_effect=FormatError("parser missing"),
            ):
                report = check_project(manifest)
            self.assertEqual(report["status"], "fail")
            self.assertIn(
                "tsx-adapter-error", [item["code"] for item in report["issues"]]
            )


if __name__ == "__main__":
    unittest.main()
