from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .conformance import run_suite
from .css import agent_context, enforce_css, render_context, render_tokens
from .enforce import enforce
from .format import FormatError, load_json, validate_design_system
from .project import check_project
from .reporters import SEVERITY_LEVEL, render, should_fail
from .research import (
    build_prompt,
    ledger_lock,
    load_ledger,
    make_plan,
    parse_time,
    record_run,
    run_codex,
    status_report,
)
from .scheduler import render_macos_launch_agent, write_macos_launch_agent

DEFAULT_CONFORMANCE_MANIFEST = (
    Path(__file__).resolve().parent / "conformance_suite" / "manifest.json"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adsys", description="Agent Design System tools"
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser(
        "validate", help="validate an ADSF design-system document"
    )
    validate.add_argument("system")
    validate.set_defaults(handler=_validate)

    check = commands.add_parser(
        "check", help="check JSON or CSS source without changing it"
    )
    _add_enforcement_arguments(check)
    check.set_defaults(handler=_check)

    fix = commands.add_parser(
        "fix", help="apply approved repairs to JSON or CSS source"
    )
    _add_enforcement_arguments(fix)
    destination = fix.add_mutually_exclusive_group(required=True)
    destination.add_argument(
        "--output", help="write the coerced document to a new path"
    )
    destination.add_argument(
        "--write", action="store_true", help="replace the input document"
    )
    fix.set_defaults(handler=_fix)

    context = commands.add_parser(
        "context", help="export the approved CSS contract for a coding assistant"
    )
    context.add_argument("system")
    context.add_argument(
        "--project-manifest", help="reviewed project manifest for the primary gate"
    )
    context.add_argument(
        "--expected-sha256",
        help="required reviewed fingerprint with --project-manifest",
    )
    context.add_argument("--format", choices=("markdown", "json"), default="markdown")
    context.add_argument("--output", help="write a file instead of stdout")
    context.set_defaults(handler=_context)

    tokens = commands.add_parser(
        "tokens", help="generate the contract's CSS custom-property stylesheet"
    )
    tokens.add_argument("system")
    tokens.add_argument("--output", help="write a file instead of stdout")
    tokens.set_defaults(handler=_tokens)

    project_check = commands.add_parser(
        "project-check", help="check non-excluded CSS under a reviewed project root"
    )
    project_check.add_argument("manifest", help="reviewed CSS project manifest")
    project_check.add_argument(
        "--expected-sha256", help="reviewed system and manifest fingerprint"
    )
    project_check.add_argument("--format", choices=("text", "json"), default="text")
    project_check.set_defaults(handler=_project_check)

    conformance = commands.add_parser(
        "conformance", help="run language-neutral ADSF conformance vectors"
    )
    conformance.add_argument(
        "manifest", nargs="?", default=str(DEFAULT_CONFORMANCE_MANIFEST)
    )
    conformance.add_argument("--case", help="run one case by id")
    conformance.add_argument("--format", choices=("text", "json"), default="text")
    conformance.set_defaults(handler=_conformance)

    research = commands.add_parser(
        "research", help="operate the budgeted research harness"
    )
    research_commands = research.add_subparsers(dest="research_command", required=True)

    plan = research_commands.add_parser(
        "plan", help="select a question if all budget gates allow it"
    )
    _add_research_inputs(plan)
    plan.add_argument("--at", help="ISO-8601 planning time; defaults to now")
    plan.add_argument("--out", required=True, help="write the machine-readable plan")
    plan.add_argument(
        "--prompt-out", help="write the generated prompt when the plan runs"
    )
    plan.add_argument(
        "--github-output",
        help="append should_run and run_id outputs for GitHub Actions",
    )
    plan.set_defaults(handler=_research_plan)

    status = research_commands.add_parser(
        "status", help="show rolling budget consumption"
    )
    status.add_argument("--budget", default="research/budget.json")
    status.add_argument("--ledger", default="research/ledger.jsonl")
    status.add_argument("--at", help="ISO-8601 report time; defaults to now")
    status.set_defaults(handler=_research_status)

    record = research_commands.add_parser(
        "record", help="record an attempted research run"
    )
    record.add_argument("--plan", required=True)
    result_input = record.add_mutually_exclusive_group()
    result_input.add_argument(
        "--result", help="path containing the agent's JSON result"
    )
    result_input.add_argument(
        "--result-env", help="environment variable containing the agent's JSON result"
    )
    record.add_argument("--status", default="success")
    record.add_argument("--usage", help="optional Codex usage JSON")
    record.add_argument("--ledger", default="research/ledger.jsonl")
    record.add_argument("--results-dir", default="research/results")
    record.set_defaults(handler=_research_record)

    run = research_commands.add_parser(
        "run", help="plan, invoke local Codex, and record one run"
    )
    _add_research_inputs(run)
    run.add_argument("--at", help="ISO-8601 planning time; defaults to now")
    run.add_argument("--repository", default=".")
    run.add_argument(
        "--schema",
        default="src/agent_design_system/schemas/research-result.schema.json",
    )
    run.add_argument("--codex", default="codex", help="Codex executable")
    run.add_argument(
        "--timeout-seconds",
        type=int,
        default=1200,
        help="terminate a local agent process after this many seconds",
    )
    run.add_argument("--results-dir", default="research/results")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan and prompt without invoking Codex",
    )
    run.set_defaults(handler=_research_run)

    scheduler = research_commands.add_parser(
        "render-macos-scheduler", help="render a nightly macOS LaunchAgent plist"
    )
    scheduler.add_argument("--output", required=True)
    scheduler.add_argument("--repository", default=".")
    scheduler.add_argument("--python", default=sys.executable)
    scheduler.add_argument("--codex", required=True)
    scheduler.add_argument("--hour", type=int, default=3)
    scheduler.add_argument("--minute", type=int, default=17)
    scheduler.set_defaults(handler=_research_scheduler)
    return parser


def _add_enforcement_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("system", help="ADSF design-system JSON")
    parser.add_argument("document", help="JSON document or CSS source to inspect")
    parser.add_argument(
        "--input-format",
        choices=("json", "css"),
        default="json",
        help="inspect a JSON document or authored CSS source",
    )
    parser.add_argument("--format", choices=("text", "json", "sarif"), default="text")
    parser.add_argument("--fail-on", choices=tuple(SEVERITY_LEVEL), default="error")
    parser.add_argument("--log", help="append each violation as JSON Lines")


def _add_research_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--budget", default="research/budget.json")
    parser.add_argument("--questions", default="research/questions.json")
    parser.add_argument("--ledger", default="research/ledger.jsonl")


def _validate(args: argparse.Namespace) -> int:
    system = load_json(args.system)
    errors = validate_design_system(system)
    if errors:
        for error in errors:
            print(f"ERROR {error}")
        return 1
    print(f"PASS {args.system}: valid ADSF design system")
    return 0


def _check(args: argparse.Namespace) -> int:
    _guard_enforcement_paths(args, fixing=False)
    result = _enforcement_result(args, apply_fixes=False)
    print(render(result, args.format))
    _append_log(args.log, result.to_dict()["violations"], args.document)
    return 1 if should_fail(result, args.fail_on) else 0


def _fix(args: argparse.Namespace) -> int:
    _guard_enforcement_paths(args, fixing=True)
    result = _enforcement_result(args, apply_fixes=True)
    destination = Path(args.document if args.write else args.output)
    system = load_json(args.system)
    if args.input_format == "css":
        final = enforce_css(system, result.document, source=str(destination))
        content = result.document
    else:
        final = enforce(system, result.document, source=str(destination))
        content = json.dumps(result.document, indent=2, ensure_ascii=False) + "\n"
    _atomic_write(destination, content)
    print(render(result, args.format))
    print(f"WROTE {destination}", file=sys.stderr)
    _append_log(args.log, result.to_dict()["violations"], args.document)
    rejected = any(violation.disposition == "reject" for violation in result.violations)
    return 1 if rejected or should_fail(final, args.fail_on) else 0


def _same_path(left: str | Path, right: str | Path) -> bool:
    a, b = Path(left), Path(right)
    try:
        return a.resolve() == b.resolve() or (
            a.exists() and b.exists() and a.samefile(b)
        )
    except RuntimeError as error:
        raise FormatError(f"invalid path: {error}") from error


def _guard_enforcement_paths(args: argparse.Namespace, *, fixing: bool) -> None:
    protected = [args.system, args.document]
    if fixing:
        if args.write and Path(args.document).is_symlink():
            raise FormatError("--write requires a direct input path, not a symlink")
        destination = args.document if args.write else args.output
        if _same_path(destination, args.system):
            raise FormatError("output must not overwrite the design system")
        if not args.write and _same_path(destination, args.document):
            raise FormatError(
                "output aliases the input; use --write for an in-place edit"
            )
        protected.append(destination)
    if args.log and any(_same_path(args.log, path) for path in protected):
        raise FormatError("log must not alias the design system, input, or output")


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        if path.exists():
            temporary.chmod(path.stat().st_mode & 0o777)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _conformance(args: argparse.Namespace) -> int:
    result = run_suite(args.manifest, only_case=args.case)
    if args.format == "json":
        print(
            json.dumps(result.to_dict(), indent=2, sort_keys=True, ensure_ascii=False)
        )
    else:
        state = "PASS" if result.passed else "FAIL"
        print(f"{state} {result.manifest}: {len(result.cases)} conformance case(s)")
        for case in result.cases:
            case_state = "PASS" if case.passed else "FAIL"
            print(f"{case_state} {case.case_id}: {case.message}")
    return 0 if result.passed else 1


def _enforcement_result(args: argparse.Namespace, *, apply_fixes: bool):
    system = load_json(args.system)
    if args.input_format == "css":
        with Path(args.document).open(encoding="utf-8", newline="") as handle:
            return enforce_css(
                system, handle.read(), source=args.document, apply_fixes=apply_fixes
            )
    document = load_json(args.document)
    return enforce(system, document, source=args.document, apply_fixes=apply_fixes)


def _export(args: argparse.Namespace, content: str) -> int:
    if args.output:
        if _same_path(args.output, args.system):
            raise FormatError("export must not overwrite the design system")
        _atomic_write(Path(args.output), content)
    else:
        print(content, end="")
    return 0


def _context(args: argparse.Namespace) -> int:
    system = load_json(args.system)
    project_gate = None
    if bool(args.project_manifest) != bool(args.expected_sha256):
        raise FormatError(
            "project context requires both --project-manifest and --expected-sha256"
        )
    if args.project_manifest:
        report = check_project(
            args.project_manifest, expected_sha256=args.expected_sha256
        )
        if report["status"] != "pass":
            raise FormatError("project gate must pass before exporting its context")
        manifest = load_json(args.project_manifest)
        declared_system = (
            Path(args.project_manifest).absolute().parent / manifest["system"]
        )
        if system != load_json(declared_system):
            raise FormatError(
                "context system differs from the project's declared system"
            )
        project_gate = (
            f"adsys project-check {shlex.quote(args.project_manifest)} "
            f"--expected-sha256 {args.expected_sha256}"
        )
    content = (
        json.dumps(
            agent_context(system, args.system, project_gate=project_gate),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
        if args.format == "json"
        else render_context(system, args.system, project_gate=project_gate)
    )
    return _export(args, content)


def _tokens(args: argparse.Namespace) -> int:
    return _export(args, render_tokens(load_json(args.system)))


def _project_check(args: argparse.Namespace) -> int:
    report = check_project(args.manifest, expected_sha256=args.expected_sha256)
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        coverage = report["coverage"]
        print(
            f"{report['status'].upper()} project CSS: {coverage['css_files']} file(s), "
            f"{coverage['css_declarations']} declaration(s), "
            f"{coverage['governed_declarations']} governed; "
            f"{len(report['issues'])} issue(s); contract {report['contract_sha256']}"
        )
        for issue in report["issues"]:
            print(f"  {issue['path']}: {issue['code']}: {issue['message']}")
        for exclusion in coverage["exclusions"]:
            print(
                f"  UNINSPECTED {exclusion['path']}: {exclusion['reason']} "
                f"({'present' if exclusion['present'] else 'unused'})"
            )
    return 0 if report["status"] == "pass" else 1


def _append_log(
    path: str | None, violations: list[dict[str, Any]], source: str
) -> None:
    if path is None or not violations:
        return
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    observed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    with log_path.open("a", encoding="utf-8") as handle:
        for violation in violations:
            handle.write(
                json.dumps(
                    {"observed_at": observed_at, "source": source, **violation},
                    sort_keys=True,
                )
                + "\n"
            )


def _research_plan(args: argparse.Namespace) -> int:
    plan = _make_plan_from_args(args)
    _write_json(Path(args.out), plan)
    if args.prompt_out and plan["decision"] == "run":
        prompt_path = Path(args.prompt_out)
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(build_prompt(plan), encoding="utf-8")
    if args.github_output:
        with Path(args.github_output).open("a", encoding="utf-8") as handle:
            handle.write(
                f"should_run={'true' if plan['decision'] == 'run' else 'false'}\n"
            )
            handle.write(f"run_id={plan.get('run_id', '')}\n")
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


def _research_status(args: argparse.Namespace) -> int:
    report = status_report(
        load_json(args.budget), load_ledger(args.ledger), parse_time(args.at)
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _research_record(args: argparse.Namespace) -> int:
    plan = load_json(args.plan)
    result_text = ""
    if args.result:
        result_text = Path(args.result).read_text(encoding="utf-8")
    elif args.result_env:
        result_text = os.environ.get(args.result_env, "")
    usage = load_json(args.usage) if args.usage else None
    entry = record_run(
        plan,
        result_text,
        status=args.status,
        ledger_path=args.ledger,
        results_dir=args.results_dir,
        usage=usage,
    )
    print(json.dumps(entry, indent=2, sort_keys=True))
    return 0


def _research_run(args: argparse.Namespace) -> int:
    if args.timeout_seconds <= 0:
        raise FormatError("timeout-seconds must be a positive integer")
    with ledger_lock(args.ledger):
        return _research_run_locked(args)


def _research_run_locked(args: argparse.Namespace) -> int:
    plan = _make_plan_from_args(args)
    if plan["decision"] == "skip":
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    if args.dry_run:
        print(json.dumps(plan, indent=2, sort_keys=True))
        print("\n--- PROMPT ---\n")
        print(build_prompt(plan))
        return 0
    result_text, usage, status = run_codex(
        plan,
        repository=args.repository,
        schema_path=args.schema,
        codex_executable=args.codex,
        timeout_seconds=args.timeout_seconds,
    )
    entry = record_run(
        plan,
        result_text,
        status=status,
        ledger_path=args.ledger,
        results_dir=args.results_dir,
        usage=usage,
    )
    print(json.dumps(entry, indent=2, sort_keys=True))
    return 0 if entry["status"] == "succeeded" else 1


def _research_scheduler(args: argparse.Namespace) -> int:
    repository = Path(args.repository).resolve()
    (repository / ".research" / "logs").mkdir(parents=True, exist_ok=True)
    content = render_macos_launch_agent(
        repository=repository,
        python_executable=args.python,
        codex_executable=args.codex,
        hour=args.hour,
        minute=args.minute,
    )
    write_macos_launch_agent(args.output, content)
    print(f"WROTE {Path(args.output).resolve()}")
    return 0


def _make_plan_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return make_plan(
        load_json(args.budget),
        load_json(args.questions),
        load_ledger(args.ledger),
        now=parse_time(args.at),
    )


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (FormatError, OSError, ValueError) as error:
        print(f"adsys: {error}", file=sys.stderr)
        return 2
