from __future__ import annotations

import plistlib
import sys
import tempfile
import unittest
from pathlib import Path

from agent_design_system.format import FormatError
from agent_design_system.scheduler import (
    LAUNCH_AGENT_LABEL,
    render_macos_launch_agent,
    write_macos_launch_agent,
)


class SchedulerTests(unittest.TestCase):
    def test_rendered_launch_agent_uses_absolute_paths_and_schedule(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            codex = Path(directory) / "codex"
            codex.touch()
            payload = plistlib.loads(
                render_macos_launch_agent(
                    repository=repository,
                    python_executable=sys.executable,
                    codex_executable=codex,
                    hour=4,
                    minute=23,
                )
            )

        self.assertEqual(payload["Label"], LAUNCH_AGENT_LABEL)
        self.assertEqual(payload["StartCalendarInterval"], {"Hour": 4, "Minute": 23})
        self.assertEqual(payload["WorkingDirectory"], str(repository))
        self.assertEqual(payload["ProgramArguments"][0], str(Path(sys.executable)))
        self.assertEqual(
            payload["ProgramArguments"][1:5],
            [
                "-m",
                "agent_design_system",
                "research",
                "run",
            ],
        )
        self.assertEqual(payload["ProgramArguments"][-1], str(codex.absolute()))
        self.assertEqual(
            payload["EnvironmentVariables"]["PYTHONPATH"],
            str(repository / "src"),
        )
        self.assertFalse(payload["RunAtLoad"])
        self.assertTrue(
            payload["StandardErrorPath"].endswith("/.research/logs/nightly.stderr.log")
        )

    def test_executable_symlink_is_not_rewritten_to_versioned_target(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            target = directory_path / "codex-0.1.0"
            target.touch()
            stable = directory_path / "codex"
            stable.symlink_to(target)
            payload = plistlib.loads(
                render_macos_launch_agent(
                    repository=repository,
                    python_executable=sys.executable,
                    codex_executable=stable,
                )
            )

        self.assertEqual(payload["ProgramArguments"][-1], str(stable))

    def test_write_launch_agent_replaces_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "nested" / "agent.plist"
            write_macos_launch_agent(destination, b"first")
            write_macos_launch_agent(destination, b"second")
            self.assertEqual(destination.read_bytes(), b"second")
            self.assertFalse(destination.with_suffix(".plist.tmp").exists())

    def test_invalid_schedule_is_rejected(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            codex = Path(directory) / "codex"
            codex.touch()
            with self.assertRaisesRegex(FormatError, "hour"):
                render_macos_launch_agent(
                    repository=repository,
                    python_executable=sys.executable,
                    codex_executable=codex,
                    hour=24,
                )

    def test_non_repository_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            codex = directory_path / "codex"
            codex.touch()
            with self.assertRaisesRegex(FormatError, "not an Agent Design System"):
                render_macos_launch_agent(
                    repository=directory_path,
                    python_executable=sys.executable,
                    codex_executable=codex,
                )


if __name__ == "__main__":
    unittest.main()
