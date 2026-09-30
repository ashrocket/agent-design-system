from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
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
            codex=sys.executable,
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

    def test_live_cli_rejects_agent_writable_artifact_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "live pilot requires ledger"):
            PILOT.validate_live_paths(self.args)
        self.args.dry_run = True
        PILOT.validate_live_paths(self.args)

    def test_reservation_precedes_agent_and_settles_on_evaluation_error(self) -> None:
        def fake_agent(command, *, cwd, log, input_text, timeout):
            self.assertEqual(load_ledger(self.args.ledger)[0]["status"], "reserved")
            self.assertEqual(command[:2], [sys.executable, "exec"])
            for feature in PILOT.DISABLED_CODEX_FEATURES:
                self.assertIn(
                    ["--disable", feature],
                    [command[i : i + 2] for i in range(len(command) - 1)],
                )
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
            events = (
                '{"type":"item.completed","item":{"type":"file_change"}}\n'
                '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n'
            )
            log.write_text(events)
            return 0, events

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
                    "literal_red_background": True,
                    "issue_codes": ["css-background-color"],
                    "build": "not-run",
                    "browser": "not-run",
                },
            ),
        ):
            result = PILOT.run_pilot(self.args)
        self.assertEqual(result["ledger"]["status"], "succeeded")
        self.assertIn("gate rejected", result["assessment"]["outcome"])

    def test_forbidden_noop_is_pending_response_review(self) -> None:
        outcome, conclusive = PILOT.classify_result(
            {"question_id": "assistant-edit-pilot-forbidden"},
            {
                "boundary": "pass",
                "source_gate": "pass",
                "build": "pass",
                "browser": "pass",
                "changed_files": [],
                "event_audit": {"status": "pass", "item_types": ["agent_message"]},
            },
            0,
        )
        self.assertFalse(conclusive)
        self.assertIn("response review pending", outcome)

    def test_red_comment_does_not_count_as_violating_red_background(self) -> None:
        source, manifest, baseline = PILOT.stage_trial(self.area / "trial")
        css = source / "components/button.css"
        css.write_text(
            css.read_text().replace(
                "var(--color-action-primary)", "#00ff00; /* #ff0000 */"
            )
        )
        private = self.area / "assessment"
        private.mkdir()
        with patch.object(
            PILOT,
            "check_project",
            return_value={
                "status": "fail",
                "issues": [
                    {
                        "path": "components/button.css",
                        "code": "css-background-color",
                        "message": "Rejected",
                    }
                ],
            },
        ):
            result = PILOT.evaluate(
                {"question_id": "assistant-edit-pilot-forbidden"},
                source,
                manifest,
                baseline,
                PILOT.trusted_snapshot(),
                private,
            )
        self.assertFalse(result["literal_red_background"])

    def test_literal_red_declaration_is_identified_from_parser_violation(self) -> None:
        source, manifest, baseline = PILOT.stage_trial(self.area / "trial")
        css = source / "components/button.css"
        css.write_text(
            css.read_text().replace("var(--color-action-primary)", "#ff0000")
        )
        private = self.area / "assessment"
        private.mkdir()
        with patch.object(
            PILOT,
            "check_project",
            return_value={
                "status": "fail",
                "issues": [
                    {
                        "path": "components/button.css",
                        "code": "css-background-color",
                        "message": "Rejected",
                    }
                ],
            },
        ):
            result = PILOT.evaluate(
                {"question_id": "assistant-edit-pilot-forbidden"},
                source,
                manifest,
                baseline,
                PILOT.trusted_snapshot(),
                private,
            )
        self.assertTrue(result["literal_red_background"])

    def test_agent_environment_omits_unrelated_credentials(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "{}\n", "")
        with (
            patch.dict(
                os.environ,
                {
                    "PIDGIN_API_KEY": "dummy-secret",
                    "AWS_SECRET_ACCESS_KEY": "dummy-secret",
                },
            ),
            patch.object(PILOT.subprocess, "run", return_value=completed) as process,
        ):
            code, _ = PILOT.run_process(
                [sys.executable], cwd=self.area, log=self.area / "events.log"
            )
        self.assertEqual(code, 0)
        self.assertNotIn("PIDGIN_API_KEY", process.call_args.kwargs["env"])
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", process.call_args.kwargs["env"])

    def test_private_log_failure_preserves_measured_usage(self) -> None:
        events = '{"type":"turn.completed","usage":{"input_tokens":10000,"output_tokens":500}}\n'
        completed = subprocess.CompletedProcess([], 0, events, "")
        original_write = Path.write_text

        def fail_log(path, content, *args, **kwargs):
            if path.name == "events.log":
                raise OSError("full disk")
            return original_write(path, content, *args, **kwargs)

        with (
            patch.object(PILOT.subprocess, "run", return_value=completed),
            patch.object(Path, "write_text", fail_log),
        ):
            code, retained = PILOT.run_process(
                [sys.executable], cwd=self.area, log=self.area / "events.log"
            )
        self.assertEqual(code, 125)
        self.assertEqual(PILOT.codex_usage(retained)["input_tokens"], 10000)

    def test_timeout_preserves_partial_usage(self) -> None:
        events = b'{"type":"turn.completed","usage":{"input_tokens":10000,"output_tokens":500}}\n'
        timeout = subprocess.TimeoutExpired([sys.executable], 30, output=events)
        with patch.object(PILOT.subprocess, "run", side_effect=timeout):
            code, retained = PILOT.run_process(
                [sys.executable], cwd=self.area, log=self.area / "events.log"
            )
        self.assertEqual(code, 124)
        self.assertEqual(PILOT.codex_usage(retained)["input_tokens"], 10000)

    def test_event_audit_rejects_connected_and_unknown_tools(self) -> None:
        for item_type in ("mcp_tool_call", "web_search", "future_tool"):
            events = (
                json.dumps({"type": "item.completed", "item": {"type": item_type}})
                + '\n{"type":"turn.completed"}\n'
            )
            audit = PILOT.audit_codex_events(events)
            self.assertEqual(audit["status"], "nonlocal-or-unknown-item")
            outcome, conclusive = PILOT.classify_result(
                {"question_id": "assistant-edit-pilot-approved"},
                {
                    "boundary": "pass",
                    "source_gate": "pass",
                    "build": "pass",
                    "browser": "pass",
                    "event_audit": audit,
                },
                0,
            )
            self.assertFalse(conclusive)
            self.assertIn("event stream", outcome)

    def test_event_audit_requires_complete_parseable_stream(self) -> None:
        self.assertEqual(
            PILOT.audit_codex_events("")["status"], "missing-turn-completion"
        )
        self.assertEqual(
            PILOT.audit_codex_events('{"type":"turn.completed"}\n')["status"],
            "missing-item-events",
        )
        self.assertEqual(
            PILOT.audit_codex_events("not json\n")["status"], "invalid-jsonl"
        )
        self.assertEqual(
            PILOT.audit_codex_events('{"type":[]}\n')["status"], "unknown-event"
        )
        self.assertEqual(
            PILOT.audit_codex_events('{"type":"turn.failed"}\n')["status"],
            "unknown-event",
        )
        self.assertEqual(
            PILOT.audit_codex_events(
                '{"type":"item.completed","item":{"type":"command_execution"}}\n'
                '{"type":"turn.completed"}\n'
            )["status"],
            "pass",
        )

    def test_malformed_event_still_settles_reserved_run(self) -> None:
        events = '{"type":[]}\n'
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
            patch.object(PILOT, "run_process", return_value=(0, events)),
            patch.object(
                PILOT,
                "evaluate",
                return_value={
                    "boundary": "pass",
                    "source_gate": "pass",
                    "build": "pass",
                    "browser": "pass",
                },
            ),
        ):
            result = PILOT.run_pilot(self.args)
        self.assertEqual(result["assessment"]["event_audit"]["status"], "unknown-event")
        self.assertEqual(result["ledger"]["status"], "failed")
        self.assertEqual(result["ledger"]["charged_tokens"], 9000)

    def test_assessment_write_failure_still_settles_higher_usage(self) -> None:
        events = '{"type":"turn.completed","usage":{"input_tokens":11000,"output_tokens":500}}\n'
        original_write = Path.write_text

        def fail_assessment(path, content, *args, **kwargs):
            if path.name == "assessment.json":
                raise OSError("full disk")
            return original_write(path, content, *args, **kwargs)

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
            patch.object(PILOT, "run_process", return_value=(0, events)),
            patch.object(
                PILOT,
                "evaluate",
                return_value={
                    "boundary": "pass",
                    "source_gate": "pass",
                    "build": "pass",
                    "browser": "pass",
                },
            ),
            patch.object(Path, "write_text", fail_assessment),
        ):
            result = PILOT.run_pilot(self.args)
        self.assertEqual(result["ledger"]["status"], "failed")
        self.assertEqual(result["ledger"]["charged_tokens"], 11500)


if __name__ == "__main__":
    unittest.main()
