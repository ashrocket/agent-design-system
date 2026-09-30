from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import jsonschema

from agent_design_system.cli import main
from agent_design_system.format import FormatError
from agent_design_system.research import (
    ledger_lock,
    load_ledger,
    make_plan,
    record_run,
    reserve_run,
    reserve_run_checked,
    run_codex,
    status_report,
)


class ResearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 20, 3, 17, tzinfo=UTC)
        self.budget = {
            "daily_tokens": 12000,
            "weekly_tokens": 70000,
            "monthly_tokens": 280000,
            "max_runs_per_day": 1,
        }
        self.questions = {
            "questions": [
                {
                    "id": "a",
                    "prompt": "A?",
                    "reserved_tokens": 9000,
                    "cooldown_days": 1,
                    "priority": 10,
                },
                {
                    "id": "b",
                    "prompt": "B?",
                    "reserved_tokens": 9000,
                    "cooldown_days": 1,
                    "priority": 10,
                },
            ]
        }

    def test_empty_ledger_plans_one_run(self) -> None:
        plan = make_plan(self.budget, self.questions, [], now=self.now)
        self.assertEqual(plan["decision"], "run")
        self.assertEqual(plan["question_id"], "a")
        self.assertEqual(plan["reserved_tokens"], 9000)

    def test_daily_limit_prevents_second_run(self) -> None:
        ledger = [self._entry(self.now - timedelta(hours=1), "a")]
        plan = make_plan(self.budget, self.questions, ledger, now=self.now)
        self.assertEqual(plan["decision"], "skip")
        self.assertIn("daily run limit", plan["reason"])

    def test_weekly_budget_prevents_overspend(self) -> None:
        ledger = [
            self._entry(self.now - timedelta(days=index), f"q{index}")
            for index in range(7)
        ]
        budget = {**self.budget, "max_runs_per_day": 99}
        plan = make_plan(budget, self.questions, ledger, now=self.now)
        self.assertEqual(plan["decision"], "skip")
        self.assertIn("weekly", plan["reason"])

    def test_monthly_budget_prevents_overspend(self) -> None:
        month_end = datetime(2026, 10, 31, 3, 17, tzinfo=UTC)
        ledger = [
            self._entry(month_end - timedelta(days=index), f"q{index}")
            for index in range(31)
        ]
        budget = {**self.budget, "max_runs_per_day": 99}
        plan = make_plan(budget, self.questions, ledger, now=month_end)
        self.assertEqual(plan["decision"], "skip")
        self.assertIn("monthly", plan["reason"])

    def test_questions_rotate_after_a_recorded_run(self) -> None:
        ledger = [self._entry(self.now - timedelta(days=1), "a")]
        plan = make_plan(
            self.budget,
            self.questions,
            ledger,
            now=self.now + timedelta(days=1),
        )
        self.assertEqual(plan["decision"], "run")
        self.assertEqual(plan["question_id"], "b")

    def test_record_charges_greater_of_reservation_and_usage(self) -> None:
        plan = make_plan(self.budget, self.questions, [], now=self.now)
        result = {
            "run_id": plan["run_id"],
            "question_id": plan["question_id"],
            "summary": "Found one ambiguity.",
            "evidence": [],
            "proposals": [],
            "risks": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "ledger.jsonl"
            entry = record_run(
                plan,
                json.dumps(result),
                status="success",
                ledger_path=ledger_path,
                results_dir=Path(directory) / "results",
                usage={"input_tokens": 8000, "output_tokens": 2500},
                now=self.now + timedelta(minutes=2),
            )
            self.assertEqual(entry["charged_tokens"], 10500)
            self.assertEqual(load_ledger(ledger_path)[0]["status"], "succeeded")

    def test_result_contract_is_a_valid_json_schema(self) -> None:
        root = Path(__file__).resolve().parents[1]
        schema = json.loads(
            (
                root / "src/agent_design_system/schemas/research-result.schema.json"
            ).read_text()
        )
        jsonschema.Draft202012Validator.check_schema(schema)

    def test_invalid_result_is_still_charged(self) -> None:
        plan = make_plan(self.budget, self.questions, [], now=self.now)
        with tempfile.TemporaryDirectory() as directory:
            entry = record_run(
                plan,
                "not json",
                status="success",
                ledger_path=Path(directory) / "ledger.jsonl",
                results_dir=Path(directory) / "results",
                now=self.now + timedelta(minutes=1),
            )
            self.assertEqual(entry["status"], "invalid")
            self.assertEqual(entry["charged_tokens"], 9000)

    def test_malformed_usage_settles_as_invalid_at_reservation(self) -> None:
        plan = make_plan(self.budget, self.questions, [], now=self.now)
        result = {
            "run_id": plan["run_id"],
            "question_id": plan["question_id"],
            "summary": "One finding.",
            "evidence": [],
            "proposals": [],
            "risks": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.jsonl"
            reserve_run(plan, ledger)
            entry = record_run(
                plan,
                json.dumps(result),
                status="success",
                ledger_path=ledger,
                results_dir=Path(directory) / "results",
                usage={"input_tokens": "unknown", "output_tokens": 3000},
            )
            self.assertEqual(entry["status"], "invalid")
            self.assertEqual(entry["charged_tokens"], 9000)
            envelope = json.loads(Path(entry["result_path"]).read_text())
            self.assertIn("usage input_tokens", envelope["error"])

    def test_unhashable_result_field_still_settles_known_higher_usage(self) -> None:
        plan = make_plan(self.budget, self.questions, [], now=self.now)
        result = {
            "run_id": plan["run_id"],
            "question_id": plan["question_id"],
            "summary": "One finding.",
            "evidence": [],
            "proposals": [
                {
                    "title": "Candidate",
                    "rationale": "Reason",
                    "compatibility": [],
                    "changes": [],
                }
            ],
            "risks": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.jsonl"
            reserve_run(plan, ledger)
            entry = record_run(
                plan,
                json.dumps(result),
                status="success",
                ledger_path=ledger,
                results_dir=Path(directory) / "results",
                usage={"input_tokens": 15000, "output_tokens": 1000},
            )
            self.assertEqual(entry["status"], "invalid")
            self.assertEqual(entry["charged_tokens"], 16000)
            self.assertEqual(load_ledger(ledger)[0], entry)
            self.assertEqual(len(ledger.read_text().splitlines()), 2)

    def test_artifact_write_failure_still_settles_known_higher_usage(self) -> None:
        plan = make_plan(self.budget, self.questions, [], now=self.now)
        result = {
            "run_id": plan["run_id"],
            "question_id": plan["question_id"],
            "summary": "One finding.",
            "evidence": [],
            "proposals": [],
            "risks": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "ledger.jsonl"
            blocked_results = root / "existing-file"
            blocked_results.write_text("not a directory")
            reserve_run(plan, ledger)
            entry = record_run(
                plan,
                json.dumps(result),
                status="success",
                ledger_path=ledger,
                results_dir=blocked_results,
                usage={"input_tokens": 15000, "output_tokens": 1000},
            )
            self.assertEqual(entry["status"], "failed")
            self.assertEqual(entry["charged_tokens"], 16000)
            self.assertIsNone(entry["result_path"])
            self.assertIn("artifact_error", entry)
            self.assertEqual(load_ledger(ledger)[0], entry)

    def test_record_rejects_traversal_run_id_before_writing(self) -> None:
        plan = make_plan(self.budget, self.questions, [], now=self.now)
        plan["run_id"] = "../escaped"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(FormatError, "safe format"):
                record_run(
                    plan,
                    "",
                    status="failed",
                    ledger_path=root / "ledger.jsonl",
                    results_dir=root / "results",
                )
            self.assertFalse((root / "escaped.json").exists())
            self.assertFalse((root / "ledger.jsonl").exists())

    def test_result_write_does_not_follow_predictable_temp_symlink(self) -> None:
        plan = make_plan(self.budget, self.questions, [], now=self.now)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "results"
            results.mkdir()
            unrelated = root / "unrelated.txt"
            unrelated.write_text("preserve this")
            (results / f"{plan['run_id']}.json.tmp").symlink_to(unrelated)
            entry = record_run(
                plan,
                "",
                status="failed",
                ledger_path=root / "ledger.jsonl",
                results_dir=results,
            )
            self.assertEqual(unrelated.read_text(), "preserve this")
            self.assertTrue((results / f"{plan['run_id']}.json.tmp").is_symlink())
            self.assertEqual(
                json.loads(Path(entry["result_path"]).read_text())["charged_tokens"],
                9000,
            )

    def test_cli_reserve_command_charges_before_remote_agent_job(self) -> None:
        plan = make_plan(self.budget, self.questions, [], now=datetime.now(UTC))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = root / "plan.json"
            ledger_path = root / "ledger.jsonl"
            budget_path = root / "budget.json"
            questions_path = root / "questions.json"
            plan_path.write_text(json.dumps(plan))
            budget_path.write_text(json.dumps(self.budget))
            questions_path.write_text(json.dumps(self.questions))
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "research",
                        "reserve",
                        "--plan",
                        str(plan_path),
                        "--ledger",
                        str(ledger_path),
                        "--budget",
                        str(budget_path),
                        "--questions",
                        str(questions_path),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(load_ledger(ledger_path)[0]["status"], "reserved")
            self.assertEqual(len(ledger_path.read_text().splitlines()), 1)

    def test_checked_reservation_rejects_two_stale_plans_from_same_budget(self) -> None:
        first = make_plan(self.budget, self.questions, [], now=self.now)
        second = make_plan(self.budget, self.questions, [], now=self.now)
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.jsonl"
            reserve_run_checked(
                first, ledger, self.budget, self.questions, now=self.now
            )
            with self.assertRaisesRegex(
                FormatError, "stale or no longer within budget"
            ):
                reserve_run_checked(
                    second, ledger, self.budget, self.questions, now=self.now
                )
            self.assertEqual(len(load_ledger(ledger)), 1)
            self.assertEqual(load_ledger(ledger)[0]["charged_tokens"], 9000)

    def test_checked_reservation_rejects_old_plan_before_charging(self) -> None:
        plan = make_plan(self.budget, self.questions, [], now=self.now)
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.jsonl"
            with self.assertRaisesRegex(FormatError, "outside the current UTC day"):
                reserve_run_checked(
                    plan,
                    ledger,
                    self.budget,
                    self.questions,
                    now=self.now + timedelta(days=1),
                )
            self.assertFalse(ledger.exists())

    def test_record_is_idempotent_by_run_id(self) -> None:
        plan = make_plan(self.budget, self.questions, [], now=self.now)
        result = {
            "run_id": plan["run_id"],
            "question_id": plan["question_id"],
            "summary": "No change.",
            "evidence": [],
            "proposals": [],
            "risks": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "ledger.jsonl"
            first = record_run(
                plan,
                json.dumps(result),
                status="success",
                ledger_path=ledger_path,
                results_dir=Path(directory) / "results",
            )
            second = record_run(
                plan,
                "",
                status="failed",
                ledger_path=ledger_path,
                results_dir=Path(directory) / "results",
            )
            self.assertEqual(first, second)
            self.assertEqual(len(load_ledger(ledger_path)), 1)

    def test_reservation_survives_interruption_and_settles_once(self) -> None:
        plan = make_plan(self.budget, self.questions, [], now=self.now)
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "ledger.jsonl"
            reserved = reserve_run(plan, ledger_path)
            self.assertEqual(reserved["status"], "reserved")
            self.assertEqual(
                status_report(self.budget, load_ledger(ledger_path), self.now)["spend"][
                    "daily"
                ],
                9000,
            )
            self.assertEqual(
                make_plan(
                    self.budget, self.questions, load_ledger(ledger_path), now=self.now
                )["decision"],
                "skip",
            )
            with self.assertRaisesRegex(FormatError, "already reserved"):
                reserve_run(plan, ledger_path)
            settled = record_run(
                plan,
                "",
                status="failed",
                ledger_path=ledger_path,
                results_dir=Path(directory) / "results",
                usage={"input_tokens": 8000, "output_tokens": 3000},
                now=self.now + timedelta(minutes=1),
            )
            self.assertEqual(settled["charged_tokens"], 11000)
            self.assertEqual(len(load_ledger(ledger_path)), 1)
            self.assertEqual(load_ledger(ledger_path)[0]["status"], "failed")
            self.assertEqual(len(ledger_path.read_text().splitlines()), 2)
            self.assertEqual(
                record_run(
                    plan,
                    "",
                    status="failed",
                    ledger_path=ledger_path,
                    results_dir=Path(directory) / "results",
                ),
                settled,
            )

    def test_cli_reserves_before_agent_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            budget = root / "budget.json"
            questions = root / "questions.json"
            ledger = root / "ledger.jsonl"
            budget.write_text(json.dumps(self.budget))
            questions.write_text(json.dumps(self.questions))
            arguments = [
                "research",
                "run",
                "--budget",
                str(budget),
                "--questions",
                str(questions),
                "--ledger",
                str(ledger),
                "--results-dir",
                str(root / "results"),
                "--at",
                self.now.isoformat(),
            ]

            def interrupted(*args: object, **kwargs: object) -> tuple[str, None, str]:
                self.assertEqual(load_ledger(ledger)[0]["status"], "reserved")
                raise RuntimeError("interrupted after process launch")

            with patch("agent_design_system.cli.run_codex", side_effect=interrupted):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    main(arguments)
            self.assertEqual(load_ledger(ledger)[0]["charged_tokens"], 9000)
            self.assertEqual(len(ledger.read_text().splitlines()), 1)

    def test_failed_agent_output_remains_failed_not_invalid(self) -> None:
        plan = make_plan(self.budget, self.questions, [], now=self.now)
        with tempfile.TemporaryDirectory() as directory:
            entry = record_run(
                plan,
                '{"error":"runner unavailable"}',
                status="failed",
                ledger_path=Path(directory) / "ledger.jsonl",
                results_dir=Path(directory) / "results",
            )
            self.assertEqual(entry["status"], "failed")
            self.assertEqual(entry["charged_tokens"], 9000)
            envelope = json.loads(Path(entry["result_path"]).read_text())
            self.assertEqual(
                envelope["error"], "Codex agent attempt failed; raw diagnostic omitted"
            )
            self.assertNotIn("raw_result", envelope)

    def test_failed_agent_diagnostic_is_not_committed_as_raw_text(self) -> None:
        plan = make_plan(self.budget, self.questions, [], now=self.now)
        diagnostic = "pidgin OAuth invalid_grant refresh_token=SECRET_VALUE"
        with tempfile.TemporaryDirectory() as directory:
            entry = record_run(
                plan,
                json.dumps({"error": diagnostic}),
                status="failed",
                ledger_path=Path(directory) / "ledger.jsonl",
                results_dir=Path(directory) / "results",
            )
            artifact = Path(entry["result_path"]).read_text()
            self.assertNotIn("SECRET_VALUE", artifact)
            self.assertNotIn("refresh_token", artifact)
            self.assertIn("Pidgin OAuth refresh failed: invalid_grant", artifact)
            self.assertEqual(entry["charged_tokens"], 9000)

    def test_missing_codex_executable_returns_recordable_failure(self) -> None:
        plan = make_plan(self.budget, self.questions, [], now=self.now)
        result, usage, status = run_codex(
            plan,
            repository=".",
            schema_path="src/agent_design_system/schemas/research-result.schema.json",
            codex_executable="/definitely/missing/codex",
            timeout_seconds=1,
        )
        self.assertEqual(status, "failed")
        self.assertIsNone(usage)
        self.assertIn("error", json.loads(result))

    def test_research_agent_ignores_unrelated_user_plugin_config(self) -> None:
        plan = make_plan(self.budget, self.questions, [], now=self.now)
        failed = subprocess.CompletedProcess(
            [], 1, "", "pidgin OAuth invalid_grant refresh_token=SECRET_VALUE"
        )
        with patch(
            "agent_design_system.research.subprocess.run", return_value=failed
        ) as launched:
            result, usage, status = run_codex(
                plan,
                repository=".",
                schema_path="src/agent_design_system/schemas/research-result.schema.json",
            )
        command = launched.call_args.args[0]
        self.assertEqual(command[:3], ["codex", "exec", "--ignore-user-config"])
        self.assertIn("read-only", command)
        self.assertEqual(status, "failed")
        self.assertIsNone(usage)
        self.assertNotIn("SECRET_VALUE", result)
        self.assertEqual(
            json.loads(result)["error"], "Pidgin OAuth refresh failed: invalid_grant"
        )

    def test_status_exposes_remaining_windows(self) -> None:
        report = status_report(
            self.budget, [self._entry(self.now - timedelta(hours=1), "a")], self.now
        )
        self.assertEqual(report["remaining"]["daily"], 3000)
        self.assertEqual(report["remaining"]["weekly"], 61000)

    def test_ledger_lock_creates_a_sibling_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "ledger.jsonl"
            with ledger_lock(ledger_path):
                self.assertTrue(Path(f"{ledger_path}.lock").is_file())

    def test_impossible_question_reservation_is_rejected(self) -> None:
        questions = {
            "questions": [
                {
                    "id": "too-large",
                    "prompt": "Impossible?",
                    "reserved_tokens": 12001,
                }
            ]
        }
        with self.assertRaisesRegex(FormatError, "exceeds a configured budget"):
            make_plan(self.budget, questions, [], now=self.now)

    def _entry(self, started: datetime, question_id: str) -> dict[str, object]:
        return {
            "started_at": started.isoformat(),
            "question_id": question_id,
            "charged_tokens": 9000,
        }


if __name__ == "__main__":
    unittest.main()
