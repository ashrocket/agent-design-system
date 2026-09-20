from __future__ import annotations

import os
import plistlib
from pathlib import Path

from .format import FormatError

LAUNCH_AGENT_LABEL = "dev.adsf.nightly-research"


def render_macos_launch_agent(
    *,
    repository: str | Path,
    python_executable: str | Path,
    codex_executable: str | Path,
    hour: int = 3,
    minute: int = 17,
) -> bytes:
    if not 0 <= hour <= 23:
        raise FormatError("scheduler hour must be between 0 and 23")
    if not 0 <= minute <= 59:
        raise FormatError("scheduler minute must be between 0 and 59")

    repo = Path(repository).resolve()
    # Keep stable executable entrypoints such as /opt/homebrew/bin/codex instead
    # of resolving them into versioned package-manager installation paths.
    python = Path(python_executable).expanduser().absolute()
    codex = Path(codex_executable).expanduser().absolute()
    if not (repo / "src/agent_design_system").is_dir():
        raise FormatError(f"not an Agent Design System repository: {repo}")
    if not python.is_file():
        raise FormatError(f"Python executable does not exist: {python}")
    if not codex.is_file():
        raise FormatError(f"Codex executable does not exist: {codex}")

    log_dir = repo / ".research" / "logs"
    payload = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            str(python),
            "-m",
            "agent_design_system",
            "research",
            "run",
            "--repository",
            str(repo),
            "--codex",
            str(codex),
        ],
        "WorkingDirectory": str(repo),
        "EnvironmentVariables": {
            "PYTHONPATH": str(repo / "src"),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        },
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "RunAtLoad": False,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "ThrottleInterval": 300,
        "StandardOutPath": str(log_dir / "nightly.stdout.log"),
        "StandardErrorPath": str(log_dir / "nightly.stderr.log"),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def write_macos_launch_agent(path: str | Path, content: bytes) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_bytes(content)
    os.replace(temporary, output)
