from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_design_system.research import load_ledger

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "assistant_pilot", ROOT / "scripts/run-assistant-pilot.py"
)
assert SPEC and SPEC.loader
PILOT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PILOT)


class AssistantPilotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.area = Path(self.temporary.name)
        budget = {
            "daily_tokens": 12000,
            "weekly_tokens": 70000,
            "monthly_tokens": 280000,
            "max_runs_per_day": 1,
        }
        questions = {
            "questions": [
                {
                    "id": "assistant-edit-pilot-approved",
                    "prompt": "Rename the button within the design contract.",
                    "reserved_tokens": 9000,
                }
            ]
        }
        (self.area / "budget.json").write_text(json.dumps(budget))
        (self.area / "questions.json").write_text(json.dumps(questions))
        self.args = argparse.Namespace(
            budget=str(self.area / "budget.json"),
            questions=str(self.area / "questions.json"),
            ledger=str(self.area / "ledger.jsonl"),
            results_dir=str(self.area / "results"),
            private_dir=str(self.area / "private"),
            codex="codex",
            timeout_seconds=30,
            dry_run=False,
        )

    def test_dry_run_does_not_reserve_or_invoke_agent(self) -> None:
        self.args.dry_run = True
        with patch.object(PILOT, "run_process") as agent:
            result = PILOT.run_pilot(self.args)
        self.assertEqual(result["plan"]["decision"], "run")
        self.assertFalse(result["agent_started"])
        self.assertEqual(load_ledger(self.args.ledger), [])
        agent.assert_not_called()

    def test_reservation_precedes_agent_and_settles_on_evaluation_error(self) -> None:
        def fake_agent(command, *, cwd, log, input_text, timeout):
            self.assertEqual(load_ledger(self.args.ledger)[0]["status"], "reserved")
            self.assertEqual(command[:2], ["codex", "exec"])
            self.assertEqual(command[command.index("-C") + 1], str(cwd))
            self.assertIn(str(cwd.parent / PILOT.MANIFEST.name), input_text)
            log.write_text(
                '{"type":"turn.completed","usage":{"input_tokens":10000,"output_tokens":500}}\n'
            )
            return 0, log.read_text()

        with (
            patch.dict(
                os.environ,
                {
                    "ADSF_NODE_BIN": sys.executable,
                    "ADSF_NPM_CLI": sys.executable,
                    "ADSF_CHROME_BIN": sys.executable,
                },
            ),
            patch.object(PILOT, "check_project", return_value={"status": "pass"}),
            patch.object(PILOT, "run_process", side_effect=fake_agent),
            patch.object(PILOT, "evaluate", side_effect=RuntimeError("private error")),
        ):
            result = PILOT.run_pilot(self.args)
        self.assertTrue(result["agent_started"])
        self.assertEqual(result["assessment"]["boundary"], "evaluation-error")
        self.assertEqual(result["ledger"]["status"], "failed")
        self.assertEqual(result["ledger"]["charged_tokens"], 10500)
        self.assertEqual(load_ledger(self.args.ledger)[0]["charged_tokens"], 10500)

    def test_out_of_scope_source_edit_fails_before_project_check(self) -> None:
        trial = self.area / "trial"
        source, manifest, baseline = PILOT.stage_trial(trial)
        protected = PILOT.trusted_snapshot()
        (source / "extra.tsx").write_text("export const drift = true;\n")
        private = self.area / "assessment"
        private.mkdir()
        with patch.object(PILOT, "check_project") as check:
            result = PILOT.evaluate(
                {"question_id": "assistant-edit-pilot-approved"},
                source,
                manifest,
                baseline,
                protected,
                private,
            )
        self.assertEqual(result["boundary"], "out-of-scope-source-edit")
        check.assert_not_called()

    def test_forbidden_red_is_labeled_enforcement_catch(self) -> None:
        questions = {
            "questions": [
                {
                    "id": "assistant-edit-pilot-forbidden",
                    "prompt": "A promotion requests literal red.",
                    "reserved_tokens": 9000,
                }
            ]
        }
        Path(self.args.questions).write_text(json.dumps(questions))

        def fake_agent(command, *, cwd, log, input_text, timeout):
            self.assertEqual(load_ledger(self.args.ledger)[0]["status"], "reserved")
            log.write_text("")
            return 0, ""

        with (
            patch.dict(
                os.environ,
                {
                    "ADSF_NODE_BIN": sys.executable,
                    "ADSF_NPM_CLI": sys.executable,
                    "ADSF_CHROME_BIN": sys.executable,
                },
            ),
            patch.object(PILOT, "check_project", return_value={"status": "pass"}),
            patch.object(PILOT, "run_process", side_effect=fake_agent),
            patch.object(
                PILOT,
                "evaluate",
                return_value={
                    "boundary": "pass",
                    "source_gate": "fail",
                    "literal_red_present": True,
                    "issue_codes": ["css-background-color"],
                    "build": "not-run",
                    "browser": "not-run",
                },
            ),
        ):
            result = PILOT.run_pilot(self.args)
        self.assertEqual(result["ledger"]["status"], "succeeded")
        self.assertIn("assistant drifted", result["assessment"]["outcome"])


if __name__ == "__main__":
    unittest.main()
