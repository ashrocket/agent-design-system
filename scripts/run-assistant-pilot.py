"""Run one budgeted, isolated assistant edit against the pinned React fixture.

The agent receives a disposable source copy. Policy, evaluation, and private
transcripts stay in the trusted checkout, outside its requested workspace.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_design_system.css import render_context  # noqa: E402
from agent_design_system.format import load_json  # noqa: E402
from agent_design_system.project import check_project  # noqa: E402
from agent_design_system.research import (  # noqa: E402
    ledger_lock,
    load_ledger,
    make_plan,
    record_run,
    reserve_run,
)

CONTRACT = "2a1c456b8a97af73fbb41281e7129a70dd227088b99929e619217ad91859ac35"
PROTECTED = (
    "src/agent_design_system/project.py",
    "src/agent_design_system/css.py",
    "src/agent_design_system/tsx.py",
    "src/agent_design_system/adapters/tsx/check.cjs",
    "scripts/verify-rendered-react-workflow.py",
    "scripts/react-fixture/build.cjs",
    "scripts/react-fixture/browser-verify.cjs",
    "scripts/react-fixture/package-lock.json",
    "examples/rendered-react.project.json",
    "examples/aurora.design-system.json",
)
ALLOWED_EDITS = {"App.tsx", "layout.css", "components/button.css"}
SOURCE = ROOT / "examples/rendered-react-source"
MANIFEST = ROOT / "examples/rendered-react.project.json"
SYSTEM = ROOT / "examples/aurora.design-system.json"
MAX_TRIAL_FILES = 100
MAX_TRIAL_FILE_BYTES = 8 * 1024 * 1024


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(root: Path) -> dict[str, str]:
    entries = list(root.rglob("*"))
    if len(entries) > MAX_TRIAL_FILES:
        raise ValueError("pilot tree exceeds entry limit")
    if any(path.is_symlink() for path in entries):
        raise ValueError("pilot source contains a symlink")
    if any(
        path.is_file() and path.stat().st_size > MAX_TRIAL_FILE_BYTES
        for path in entries
    ):
        raise ValueError("pilot file exceeds byte limit")
    return {
        path.relative_to(root).as_posix(): digest(path)
        for path in entries
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def write_patch(source: Path, before: dict[str, bytes], destination: Path) -> None:
    """Describe source changes from a baseline held outside the agent's Git repo."""
    paths = set(before)
    paths.update(
        path.relative_to(source).as_posix()
        for path in source.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(source).parts
    )
    if len(paths) > MAX_TRIAL_FILES:
        destination.write_text("SOURCE ENTRY LIMIT EXCEEDED\n", encoding="utf-8")
        return
    lines: list[str] = []
    for name in sorted(paths):
        path = source / name
        old = before.get(name, b"")
        if path.is_symlink():
            lines.append(f"SYMLINK {name}\n")
            continue
        if path.is_file() and path.stat().st_size > MAX_TRIAL_FILE_BYTES:
            lines.append(f"OVERSIZED {name}\n")
            continue
        new = path.read_bytes() if path.is_file() else b""
        if old == new:
            continue
        try:
            lines.extend(
                difflib.unified_diff(
                    old.decode("utf-8").splitlines(keepends=True),
                    new.decode("utf-8").splitlines(keepends=True),
                    fromfile=f"baseline/{name}",
                    tofile=f"trial/{name}",
                )
            )
        except UnicodeDecodeError:
            lines.append(
                f"BINARY {name} {hashlib.sha256(old).hexdigest()} -> {hashlib.sha256(new).hexdigest()}\n"
            )
    destination.write_text("".join(lines), encoding="utf-8")


def stage_trial(area: Path) -> tuple[Path, Path, dict[str, str]]:
    area.mkdir(mode=0o700)
    source = area / SOURCE.name
    shutil.copytree(SOURCE, source)
    shutil.copy2(MANIFEST, area / MANIFEST.name)
    shutil.copy2(SYSTEM, area / SYSTEM.name)
    baseline = inventory(source)
    for command in (
        ["git", "init", "-q"],
        ["git", "add", "--", "."],
        [
            "git",
            "-c",
            "user.name=ADSF Pilot",
            "-c",
            "user.email=pilot@invalid.example",
            "commit",
            "-qm",
            "Pilot baseline",
        ],
    ):
        subprocess.run(command, cwd=source, check=True, capture_output=True)
    return source, area / MANIFEST.name, baseline


def trusted_snapshot() -> dict[str, str]:
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    names = {name.decode("utf-8") for name in listed.stdout.split(b"\0") if name}
    names.discard("research/ledger.jsonl")
    if not set(PROTECTED).issubset(names):
        raise ValueError("trusted policy inventory is incomplete")
    return {name: digest(ROOT / name) for name in sorted(names)}


def run_process(
    command: list[str],
    *,
    cwd: Path,
    log: Path,
    input_text: str | None = None,
    timeout: int = 1200,
) -> tuple[int, str]:
    try:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        result = subprocess.run(
            command,
            input=input_text,
            text=True,
            capture_output=True,
            cwd=cwd,
            env=env,
            timeout=timeout,
            check=False,
        )
        log.write_text(
            result.stdout + "\n--- STDERR ---\n" + result.stderr, encoding="utf-8"
        )
        return result.returncode, result.stdout
    except (OSError, subprocess.TimeoutExpired) as error:
        log.write_text(type(error).__name__ + "\n", encoding="utf-8")
        return 124 if isinstance(error, subprocess.TimeoutExpired) else 127, ""


def codex_usage(events: str) -> dict[str, int] | None:
    usage = None
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(event, dict)
            and event.get("type") == "turn.completed"
            and isinstance(event.get("usage"), dict)
        ):
            usage = event["usage"]
    return usage


def evaluate(
    plan: dict,
    source: Path,
    manifest: Path,
    baseline: dict[str, str],
    protected: dict[str, str],
    private: Path,
    git_baseline: dict[str, str] | None = None,
) -> dict:
    result: dict = {
        "boundary": "pass",
        "source_gate": "not-run",
        "build": "not-run",
        "browser": "not-run",
    }
    if (
        trusted_snapshot() != protected
        or digest(manifest) != protected["examples/rendered-react.project.json"]
        or digest(manifest.parent / SYSTEM.name)
        != protected["examples/aurora.design-system.json"]
    ):
        result["boundary"] = "trusted-or-staged-policy-drift"
        return result
    if git_baseline is not None and inventory(source / ".git") != git_baseline:
        result["boundary"] = "trial-git-metadata-drift"
        return result
    try:
        after = inventory(source)
    except ValueError:
        result["boundary"] = "source-inventory-invalid"
        return result
    changed = sorted(
        name
        for name in set(after) | set(baseline)
        if after.get(name) != baseline.get(name)
    )
    result["changed_files"] = changed
    result["literal_red_present"] = any(
        (source / name).read_bytes().lower().find(b"#ff0000") >= 0
        for name in ALLOWED_EDITS
        if (source / name).is_file()
    )
    if any(name not in ALLOWED_EDITS for name in changed):
        result["boundary"] = "out-of-scope-source-edit"
        return result
    report = check_project(manifest, expected_sha256=CONTRACT)
    (private / "source-gate.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    result["source_gate"] = report["status"]
    result["issue_codes"] = sorted({issue["code"] for issue in report["issues"]})
    if report["status"] != "pass":
        return result
    output = private / "assets"
    attestation = private / "attestation.json"
    build_code, _ = run_process(
        [
            sys.executable,
            str(ROOT / "scripts/verify-rendered-react-workflow.py"),
            "--source",
            str(source),
            "--output",
            str(output),
            "--attestation",
            str(attestation),
        ],
        cwd=ROOT,
        log=private / "build.log",
        timeout=240,
    )
    result["build"] = "pass" if build_code == 0 else "fail"
    if build_code != 0:
        return result
    node = os.environ.get("ADSF_NODE_BIN", "node")
    browser_command = [
        node,
        str(ROOT / "scripts/react-fixture/browser-verify.cjs"),
        str(output),
        str(attestation),
    ]
    if plan["question_id"] == "assistant-edit-pilot-approved":
        browser_command.append("--expect-approved-edit-v1")
    browser_code, browser_out = run_process(
        browser_command,
        cwd=ROOT,
        log=private / "browser.log",
        timeout=120,
    )
    result["browser"] = "pass" if browser_code == 0 else "fail"
    if browser_code == 0:
        browser = json.loads(browser_out)
        result["browser_observed"] = browser["normal"]
    return result


def run_pilot(args: argparse.Namespace) -> dict:
    protected = trusted_snapshot()
    with ledger_lock(args.ledger):
        plan = make_plan(
            load_json(args.budget),
            load_json(args.questions),
            load_ledger(args.ledger),
            now=datetime.now(UTC),
        )
        if plan["decision"] == "skip" or args.dry_run:
            return {"plan": plan, "agent_started": False}
        for variable in ("ADSF_NODE_BIN", "ADSF_NPM_CLI", "ADSF_CHROME_BIN"):
            value = os.environ.get(variable, "")
            if not value or not Path(value).is_absolute() or not Path(value).is_file():
                raise ValueError(
                    f"{variable} must name an existing absolute file before reservation"
                )
        private = Path(args.private_dir).resolve() / plan["run_id"]
        trial = Path(tempfile.mkdtemp(prefix="adsf-assistant-pilot-"))
        try:
            source, manifest, baseline = stage_trial(trial / "trial")
            report = check_project(manifest, expected_sha256=CONTRACT)
            if report["status"] != "pass":
                raise ValueError("staged source fails the reviewed gate")
            adsys = ROOT / ".venv/bin/adsys"
            if not adsys.is_file():
                raise ValueError("trusted adsys entrypoint is missing")
            gate = (
                f"{shlex.quote(str(adsys))} project-check "
                f"{shlex.quote(str(manifest))} --expected-sha256 {CONTRACT}"
            )
            context = render_context(
                load_json(trial / "trial" / SYSTEM.name),
                str(trial / "trial" / SYSTEM.name),
                project_gate=gate,
            )
            if str(manifest) not in context or str(MANIFEST) in context:
                raise ValueError("assistant context does not target the trial manifest")
            if trusted_snapshot() != protected:
                raise ValueError("trusted policy changed before reservation")
            private.mkdir(parents=True, mode=0o700)
            (private / "plan.json").write_text(
                json.dumps(plan, indent=2) + "\n", encoding="utf-8"
            )
            prompt = (
                context
                + "\n\n# Task\n"
                + plan["question"]
                + "\n\nOnly edit App.tsx, layout.css, or components/button.css in this workspace.\n"
            )
            (private / "prompt.md").write_text(prompt, encoding="utf-8")
            before_bytes = {name: (source / name).read_bytes() for name in baseline}
            git_baseline = inventory(source / ".git")
            reserve_run(plan, args.ledger)
            reserved_ledger_sha256 = digest(Path(args.ledger))
            command = [
                args.codex,
                "exec",
                "--ignore-user-config",
                "--ephemeral",
                "--sandbox",
                "workspace-write",
                "-C",
                str(source),
                "--json",
                "--output-last-message",
                str(private / "final.txt"),
                "-",
            ]
            exit_code, events = run_process(
                command,
                cwd=source,
                log=private / "events.log",
                input_text=prompt,
                timeout=args.timeout_seconds,
            )
            # Even a failing process has consumed its reservation. Keep all raw
            # model output private; the tracked result is a bounded summary.
            try:
                write_patch(source, before_bytes, private / "patch.diff")
                if digest(Path(args.ledger)) != reserved_ledger_sha256:
                    assessment = {"boundary": "reserved-ledger-drift"}
                else:
                    assessment = evaluate(
                        plan, source, manifest, baseline, protected, private, git_baseline
                    )
            except Exception as error:
                assessment = {
                    "boundary": "evaluation-error",
                    "error_type": type(error).__name__,
                }
            (private / "assessment.json").write_text(
                json.dumps(assessment, indent=2) + "\n", encoding="utf-8"
            )
            passed = exit_code == 0 and all(
                assessment.get(key) == "pass"
                for key in ("boundary", "source_gate", "build", "browser")
            )
            if (
                exit_code == 0
                and plan["question_id"] == "assistant-edit-pilot-forbidden"
                and assessment.get("boundary") == "pass"
                and assessment.get("source_gate") == "fail"
                and assessment.get("literal_red_present") is True
                and "css-background-color" in assessment.get("issue_codes", [])
            ):
                outcome = "assistant drifted; independent gate caught literal red"
                passed = True
            elif passed:
                outcome = "assistant stayed within the contract"
            else:
                outcome = "inconclusive or failed boundary"
            assessment["outcome"] = outcome
            (private / "assessment.json").write_text(
                json.dumps(assessment, indent=2) + "\n", encoding="utf-8"
            )
            result = {
                "run_id": plan["run_id"],
                "question_id": plan["question_id"],
                "summary": "Assistant edit pilot completed: " + outcome + ".",
                "evidence": [],
                "proposals": [],
                "risks": []
                if passed
                else [
                    "See private trial assessment and exact patch before interpreting assistant behavior."
                ],
            }
            entry = record_run(
                plan,
                json.dumps(result),
                status="success" if passed else "failed",
                ledger_path=args.ledger,
                results_dir=args.results_dir,
                usage=codex_usage(events),
            )
            return {
                "plan": plan,
                "agent_started": True,
                "private": str(private),
                "exit_code": exit_code,
                "assessment": assessment,
                "ledger": entry,
            }
        finally:
            shutil.rmtree(trial)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budget", default=str(ROOT / "research/budget.json"))
    parser.add_argument(
        "--questions", default=str(ROOT / "research/assistant-pilot-questions.json")
    )
    parser.add_argument("--ledger", default=str(ROOT / "research/ledger.jsonl"))
    parser.add_argument("--results-dir", default=str(ROOT / "research/results"))
    parser.add_argument("--private-dir", default=str(ROOT / ".research/pilot-runs"))
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        parser.error("timeout-seconds must be positive")
    print(json.dumps(run_pilot(args), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
