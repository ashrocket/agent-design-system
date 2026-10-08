"""Exercise the actual assistant source workflow; usable locally and in CI."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEM = "examples/aurora.design-system.json"


def run(*args: str, expected: int = 0) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [sys.executable, "-m", "agent_design_system", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != expected:
        raise RuntimeError(
            f"{args!r}: expected exit {expected}, got {completed.returncode}\n{completed.stdout}\n{completed.stderr}"
        )
    return completed


def main() -> None:
    source = ROOT / "examples/button.drift.css"
    before = source.read_bytes()
    run("check", SYSTEM, "examples/button.valid.css", "--input-format", "css")
    report = json.loads(
        run(
            "check",
            SYSTEM,
            str(source),
            "--input-format",
            "css",
            "--format",
            "json",
            expected=1,
        ).stdout
    )
    assert report["coverage"]["governed"] == 4
    assert len(report["violations"]) == 4
    assert all(
        v["location"]["line"] > 0 and v["location"]["column"] > 0
        for v in report["violations"]
    )
    with tempfile.TemporaryDirectory(prefix="adsys-css-verification-") as temporary:
        directory = Path(temporary)
        fixed = directory / "fixed.css"
        run("fix", SYSTEM, str(source), "--input-format", "css", "--output", str(fixed))
        run("check", SYSTEM, str(fixed), "--input-format", "css")
        fixed_before = fixed.read_bytes()
        second = json.loads(
            run(
                "fix",
                SYSTEM,
                str(fixed),
                "--input-format",
                "css",
                "--write",
                "--format",
                "json",
            ).stdout
        )
        assert not second["changed"] and fixed.read_bytes() == fixed_before
        tokens = directory / "tokens.css"
        run("tokens", SYSTEM, "--output", str(tokens))
        context = json.loads(run("context", SYSTEM, "--format", "json").stdout)
        assert context["contract_sha256"] == report["contract_sha256"]
        assert context["contract_sha256"] in tokens.read_text()
        assert "--space-4: 16px;" in tokens.read_text()
        bypass = directory / "bypass.css"
        bypass.write_text(
            ".button { --color-action-primary: red; background-color: var(--color-action-primary, red); }"
        )
        run("check", SYSTEM, str(bypass), "--input-format", "css", expected=1)
        unsupported = directory / "unsupported.css"
        unsupported.write_text('@import "uninspected.css";')
        run("check", SYSTEM, str(unsupported), "--input-format", "css", expected=2)
    assert source.read_bytes() == before
    print(
        "PASS CSS workflow: real source drift, source locations, exact repair, fresh recheck, idempotence, exports, bypass rejection, and unchanged input"
    )


if __name__ == "__main__":
    main()
