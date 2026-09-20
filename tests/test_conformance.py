from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import jsonschema

from agent_design_system.conformance import run_suite
from agent_design_system.format import FormatError


ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "src/agent_design_system/conformance_suite"


class ConformanceTests(unittest.TestCase):
    def test_normative_suite_passes(self) -> None:
        result = run_suite(SUITE / "manifest.json")
        self.assertTrue(result.passed)
        self.assertEqual(len(result.cases), 7)

    def test_one_case_can_be_selected(self) -> None:
        result = run_suite(SUITE / "manifest.json", only_case="reject-always-fails")
        self.assertTrue(result.passed)
        self.assertEqual(result.cases[0].case_id, "reject-always-fails")

    def test_unknown_case_is_rejected(self) -> None:
        with self.assertRaisesRegex(FormatError, "unknown conformance case"):
            run_suite(SUITE / "manifest.json", only_case="missing")

    def test_changed_expectation_fails_the_suite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "suite"
            shutil.copytree(SUITE, copied)
            manifest_path = copied / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["cases"][0]["expected"]["changed"] = True
            manifest_path.write_text(json.dumps(manifest))
            result = run_suite(manifest_path, only_case="valid-document")
            self.assertFalse(result.passed)

    def test_manifest_schema_is_valid_and_accepts_manifest(self) -> None:
        schema = json.loads(
            (
                ROOT
                / "src/agent_design_system/schemas/conformance-manifest.schema.json"
            ).read_text()
        )
        manifest = json.loads((SUITE / "manifest.json").read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(schema).validate(manifest)


if __name__ == "__main__":
    unittest.main()
