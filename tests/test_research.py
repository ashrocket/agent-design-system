from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jsonschema

from agent_design_system.format import FormatError
from agent_design_system.research import (
    ledger_lock,
    load_ledger,
    make_plan,
    record_run,
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
