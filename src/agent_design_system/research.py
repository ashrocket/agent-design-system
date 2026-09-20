from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .format import FormatError


@dataclass(frozen=True)
class BudgetWindow:
    name: str
    start: datetime
    limit: int


@contextmanager
def ledger_lock(ledger_path: str | Path) -> Iterator[None]:
    """Serialize local plan/run/record cycles against one ledger."""
    try:
        import fcntl
    except ImportError as error:
        raise FormatError("local research locking requires a POSIX platform") from error

    lock_path = Path(f"{ledger_path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def parse_time(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def load_ledger(path: str | Path) -> list[dict[str, Any]]:
    ledger_path = Path(path)
    if not ledger_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        ledger_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as error:
            raise FormatError(
                f"invalid ledger JSON at {ledger_path}:{line_number}: {error}"
            ) from error
        if (
            not isinstance(entry, dict)
            or "started_at" not in entry
            or "charged_tokens" not in entry
        ):
            raise FormatError(f"invalid ledger entry at {ledger_path}:{line_number}")
        entries.append(entry)
    return entries


def budget_windows(config: dict[str, Any], now: datetime) -> list[BudgetWindow]:
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=day_start.weekday())
    month_start = day_start.replace(day=1)
    return [
        BudgetWindow("daily", day_start, int(config["daily_tokens"])),
        BudgetWindow("weekly", week_start, int(config["weekly_tokens"])),
        BudgetWindow("monthly", month_start, int(config["monthly_tokens"])),
    ]


def make_plan(
    budget: dict[str, Any],
    questions_document: dict[str, Any],
    ledger: list[dict[str, Any]],
    *,
    now: datetime,
) -> dict[str, Any]:
    _validate_configuration(budget, questions_document)
    questions = [
        question
        for question in questions_document.get("questions", [])
        if question.get("enabled", True)
    ]
    if not questions:
        return _skip_plan(now, "no enabled research questions")

    spend = _spend_by_window(budget, ledger, now)
    daily_runs = sum(
        1
        for entry in ledger
        if parse_time(entry["started_at"]) >= budget_windows(budget, now)[0].start
    )
    if daily_runs >= int(budget.get("max_runs_per_day", 1)):
        return _skip_plan(now, "daily run limit reached", spend)

    eligible = [
        question for question in questions if _cooldown_elapsed(question, ledger, now)
    ]
    if not eligible:
        return _skip_plan(now, "all research questions are cooling down", spend)

    last_runs = _last_runs(ledger)
    eligible.sort(
        key=lambda question: (
            -int(question.get("priority", 0)),
            last_runs.get(question["id"], datetime.min.replace(tzinfo=UTC)),
            question["id"],
        )
    )

    blocked_reasons: list[str] = []
    for question in eligible:
        reservation = int(question["reserved_tokens"])
        exceeded = [
            window.name
            for window in budget_windows(budget, now)
            if spend[window.name] + reservation > window.limit
        ]
        if exceeded:
            blocked_reasons.append(
                f"{question['id']} exceeds {', '.join(exceeded)} budget"
            )
            continue
        return {
            "format": "adsf-research-plan",
            "version": "0.1.0",
            "decision": "run",
            "run_id": f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}",
            "planned_at": now.isoformat().replace("+00:00", "Z"),
            "question_id": question["id"],
            "question": question["prompt"],
            "reserved_tokens": reservation,
            "budget_before": spend,
            "budget_limits": {
                window.name: window.limit for window in budget_windows(budget, now)
            },
        }
    return _skip_plan(now, "; ".join(blocked_reasons), spend)


def build_prompt(plan: dict[str, Any]) -> str:
    if plan.get("decision") != "run":
        raise FormatError("cannot build a prompt for a skipped research plan")
    return f"""# Nightly Agent Design System research

Run ID: {plan["run_id"]}
Question ID: {plan["question_id"]}

## Research question

{plan["question"]}

## Boundaries

- Inspect this repository's current format, examples, conformance vectors, enforcement engine, tests, and docs.
- This is research, not implementation. Do not edit files.
- Prefer evidence from exact repository paths and current behavior.
- Identify at most three proposals. Favor small, backwards-compatible changes.
- Do not invent evidence or claim a test was run unless you ran it.
- Return only JSON matching `src/agent_design_system/schemas/research-result.schema.json`.
- Copy the Run ID and Question ID above exactly into the result.
"""


def record_run(
    plan: dict[str, Any],
    result_text: str,
    *,
    status: str,
    ledger_path: str | Path,
    results_dir: str | Path,
    usage: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if plan.get("decision") != "run":
        raise FormatError("cannot record a skipped research plan")
    for existing in load_ledger(ledger_path):
        if existing.get("run_id") == plan["run_id"]:
            return existing
    finished_at = (now or datetime.now(UTC)).astimezone(UTC)
    reservation = int(plan["reserved_tokens"])
    actual_tokens = _usage_total(usage)
    charged_tokens = max(reservation, actual_tokens)
    normalized_status = "succeeded" if status in {"success", "succeeded"} else "failed"
    result: dict[str, Any] | None = None
    result_error: str | None = None
    if result_text.strip():
        try:
            candidate = json.loads(result_text)
            _validate_research_result(candidate, plan)
            result = candidate
        except (json.JSONDecodeError, FormatError) as error:
            result_error = str(error)
            if normalized_status == "succeeded":
                normalized_status = "invalid"
    else:
        result_error = "agent returned an empty result"
        if normalized_status == "succeeded":
            normalized_status = "invalid"

    output_dir = Path(results_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / f"{plan['run_id']}.json"
    result_payload: dict[str, Any] = {
        "plan": plan,
        "status": normalized_status,
        "finished_at": finished_at.isoformat().replace("+00:00", "Z"),
        "usage": usage,
        "charged_tokens": charged_tokens,
        "result": result,
    }
    if result_error:
        result_payload["error"] = result_error
        if result_text:
            result_payload["raw_result"] = result_text[:32000]
    _write_json(result_path, result_payload)

    entry = {
        "run_id": plan["run_id"],
        "question_id": plan["question_id"],
        "started_at": plan["planned_at"],
        "finished_at": result_payload["finished_at"],
        "status": normalized_status,
        "reserved_tokens": reservation,
        "actual_tokens": actual_tokens if actual_tokens else None,
        "charged_tokens": charged_tokens,
        "result_path": str(result_path),
    }
    ledger = Path(ledger_path)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")
    return entry


def status_report(
    budget: dict[str, Any], ledger: list[dict[str, Any]], now: datetime
) -> dict[str, Any]:
    _validate_budget(budget)
    spend = _spend_by_window(budget, ledger, now)
    limits = {window.name: window.limit for window in budget_windows(budget, now)}
    return {
        "at": now.isoformat().replace("+00:00", "Z"),
        "spend": spend,
        "limits": limits,
        "remaining": {name: limits[name] - spend[name] for name in limits},
        "runs_recorded": len(ledger),
    }


def run_codex(
    plan: dict[str, Any],
    *,
    repository: str | Path,
    schema_path: str | Path,
    codex_executable: str = "codex",
    timeout_seconds: int = 1200,
) -> tuple[str, dict[str, Any] | None, str]:
    prompt = build_prompt(plan)
    with tempfile.TemporaryDirectory(prefix="adsys-research-") as temporary:
        result_path = Path(temporary) / "result.json"
        command = [
            codex_executable,
            "exec",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--config",
            'model_reasoning_effort="low"',
            "--config",
            'model_verbosity="low"',
            "--json",
            "--output-schema",
            str(Path(schema_path).resolve()),
            "--output-last-message",
            str(result_path),
            "-",
        ]
        try:
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=repository,
                check=False,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return json.dumps({"error": str(error)}), None, "failed"
        usage: dict[str, Any] | None = None
        for line in completed.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "turn.completed" and isinstance(
                event.get("usage"), dict
            ):
                usage = event["usage"]
        result_text = (
            result_path.read_text(encoding="utf-8") if result_path.exists() else ""
        )
        status = "success" if completed.returncode == 0 else "failed"
        if not result_text and completed.stderr:
            result_text = json.dumps({"error": completed.stderr[-4000:]})
        return result_text, usage, status


def _spend_by_window(
    budget: dict[str, Any], ledger: list[dict[str, Any]], now: datetime
) -> dict[str, int]:
    spend: dict[str, int] = {}
    for window in budget_windows(budget, now):
        spend[window.name] = sum(
            int(entry["charged_tokens"])
            for entry in ledger
            if window.start <= parse_time(entry["started_at"]) <= now
        )
    return spend


def _last_runs(ledger: list[dict[str, Any]]) -> dict[str, datetime]:
    last: dict[str, datetime] = {}
    for entry in ledger:
        question_id = entry.get("question_id")
        if not isinstance(question_id, str):
            continue
        started = parse_time(entry["started_at"])
        if question_id not in last or started > last[question_id]:
            last[question_id] = started
    return last


def _cooldown_elapsed(
    question: dict[str, Any], ledger: list[dict[str, Any]], now: datetime
) -> bool:
    last = _last_runs(ledger).get(question["id"])
    if last is None:
        return True
    return now - last >= timedelta(days=int(question.get("cooldown_days", 0)))


def _skip_plan(
    now: datetime, reason: str, spend: dict[str, int] | None = None
) -> dict[str, Any]:
    return {
        "format": "adsf-research-plan",
        "version": "0.1.0",
        "decision": "skip",
        "planned_at": now.isoformat().replace("+00:00", "Z"),
        "reason": reason,
        "budget_before": spend or {},
    }


def _usage_total(usage: dict[str, Any] | None) -> int:
    if not usage:
        return 0
    fields = ("input_tokens", "output_tokens", "reasoning_output_tokens")
    return sum(int(usage.get(field, 0) or 0) for field in fields)


def _validate_research_result(result: Any, plan: dict[str, Any]) -> None:
    if not isinstance(result, dict):
        raise FormatError("research result must be an object")
    for required_field in (
        "run_id",
        "question_id",
        "summary",
        "evidence",
        "proposals",
        "risks",
    ):
        if required_field not in result:
            raise FormatError(
                f"research result missing required property: {required_field}"
            )
    if result["run_id"] != plan["run_id"]:
        raise FormatError("research result run_id does not match the plan")
    if result["question_id"] != plan["question_id"]:
        raise FormatError("research result question_id does not match the plan")
    allowed = {
        "run_id",
        "question_id",
        "summary",
        "evidence",
        "proposals",
        "risks",
        "recommended_next_question",
    }
    unknown = sorted(set(result) - allowed)
    if unknown:
        raise FormatError(f"research result has unknown property: {unknown[0]}")
    if (
        not isinstance(result["summary"], str)
        or not result["summary"].strip()
        or len(result["summary"]) > 1200
    ):
        raise FormatError("research result summary must be 1 to 1200 characters")
    if not isinstance(result["evidence"], list) or len(result["evidence"]) > 8:
        raise FormatError(
            "research result evidence must be an array of at most 8 items"
        )
    for index, item in enumerate(result["evidence"]):
        _validate_string_object(
            item, {"path", "claim"}, f"research result evidence[{index}]"
        )
    if not isinstance(result["proposals"], list):
        raise FormatError("research result proposals must be an array")
    if len(result["proposals"]) > 3:
        raise FormatError("research result may contain at most three proposals")
    for index, proposal in enumerate(result["proposals"]):
        path = f"research result proposals[{index}]"
        if not isinstance(proposal, dict):
            raise FormatError(f"{path} must be an object")
        proposal_fields = {"title", "rationale", "compatibility", "changes"}
        if set(proposal) != proposal_fields:
            raise FormatError(f"{path} must contain exactly {sorted(proposal_fields)}")
        for field in ("title", "rationale"):
            if not isinstance(proposal[field], str) or not proposal[field].strip():
                raise FormatError(f"{path}.{field} must be a non-empty string")
        if proposal["compatibility"] not in {
            "backwards-compatible",
            "breaking",
            "research-only",
        }:
            raise FormatError(f"{path}.compatibility is invalid")
        changes = proposal["changes"]
        if (
            not isinstance(changes, list)
            or len(changes) > 6
            or any(
                not isinstance(change, str) or not change.strip() for change in changes
            )
        ):
            raise FormatError(
                f"{path}.changes must contain at most 6 non-empty strings"
            )
    if (
        not isinstance(result["risks"], list)
        or len(result["risks"]) > 6
        or any(
            not isinstance(risk, str) or not risk.strip() for risk in result["risks"]
        )
    ):
        raise FormatError(
            "research result risks must contain at most 6 non-empty strings"
        )
    next_question = result.get("recommended_next_question")
    if next_question is not None and not isinstance(next_question, str):
        raise FormatError("research result recommended_next_question must be a string")


def _validate_string_object(value: Any, fields: set[str], path: str) -> None:
    if not isinstance(value, dict) or set(value) != fields:
        raise FormatError(f"{path} must contain exactly {sorted(fields)}")
    for field in fields:
        if not isinstance(value[field], str) or not value[field].strip():
            raise FormatError(f"{path}.{field} must be a non-empty string")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _validate_configuration(
    budget: dict[str, Any], questions_document: dict[str, Any]
) -> None:
    _validate_budget(budget)

    questions = questions_document.get("questions")
    if not isinstance(questions, list):
        raise FormatError("research questions must be an array")
    seen_ids: set[str] = set()
    for index, question in enumerate(questions):
        path = f"research questions[{index}]"
        if not isinstance(question, dict):
            raise FormatError(f"{path} must be an object")
        question_id = question.get("id")
        if not isinstance(question_id, str) or not question_id.strip():
            raise FormatError(f"{path}.id must be a non-empty string")
        if question_id in seen_ids:
            raise FormatError(f"duplicate research question id: {question_id}")
        seen_ids.add(question_id)
        if (
            not isinstance(question.get("prompt"), str)
            or not question["prompt"].strip()
        ):
            raise FormatError(f"{path}.prompt must be a non-empty string")
        if not _is_positive_integer(question.get("reserved_tokens")):
            raise FormatError(f"{path}.reserved_tokens must be a positive integer")
        if int(question["reserved_tokens"]) > min(
            int(budget["daily_tokens"]),
            int(budget["weekly_tokens"]),
            int(budget["monthly_tokens"]),
        ):
            raise FormatError(
                f"{path}.reserved_tokens exceeds a configured budget window"
            )
        cooldown = question.get("cooldown_days", 0)
        if not isinstance(cooldown, int) or isinstance(cooldown, bool) or cooldown < 0:
            raise FormatError(f"{path}.cooldown_days must be a non-negative integer")
        priority = question.get("priority", 0)
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise FormatError(f"{path}.priority must be an integer")
        if "enabled" in question and not isinstance(question["enabled"], bool):
            raise FormatError(f"{path}.enabled must be a boolean")


def _validate_budget(budget: dict[str, Any]) -> None:
    required_budget = (
        "daily_tokens",
        "weekly_tokens",
        "monthly_tokens",
        "max_runs_per_day",
    )
    for field in required_budget:
        if not _is_positive_integer(budget.get(field)):
            raise FormatError(f"research budget {field} must be a positive integer")
    if budget["daily_tokens"] > budget["weekly_tokens"]:
        raise FormatError("research budget daily_tokens must not exceed weekly_tokens")
    if budget["weekly_tokens"] > budget["monthly_tokens"]:
        raise FormatError(
            "research budget weekly_tokens must not exceed monthly_tokens"
        )


def _is_positive_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
