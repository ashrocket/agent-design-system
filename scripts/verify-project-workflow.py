"""Exercise project-wide discovery, parity, coverage, and the pinned CLI gate."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "examples/assistant-app.project.json"
FINGERPRINT = "4be9001fef49440bee48e19b1aa938e95d44007960b52df28cdc4310dcf3315a"


def check(manifest: Path, expected: int = 0) -> dict:
    command = [
        sys.executable,
        "-m",
        "agent_design_system",
        "project-check",
        str(manifest),
        "--expected-sha256",
        FINGERPRINT,
        "--format",
        "json",
    ]
    completed = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, check=False
    )
    if completed.returncode != expected:
        raise RuntimeError(
            f"expected exit {expected}, got {completed.returncode}: {completed.stdout}\n{completed.stderr}"
        )
    return json.loads(completed.stdout)


def main() -> None:
    original = {
        path.relative_to(ROOT): path.read_bytes()
        for path in (ROOT / "examples/assistant-app").rglob("*")
        if path.is_file()
    }
    report = check(MANIFEST)
    assert report["status"] == "pass"
    assert [item["path"] for item in report["files"]] == [
        "components/button.css",
        "layout.css",
    ]
    assert report["coverage"]["governed_declarations"] == 11
    context_run = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_design_system",
            "context",
            str(ROOT / "examples/aurora.design-system.json"),
            "--project-manifest",
            str(MANIFEST),
            "--expected-sha256",
            FINGERPRINT,
            "--format",
            "json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assistant_context = json.loads(context_run.stdout)
    assert "project-check" in assistant_context["gate"]
    assert FINGERPRINT in assistant_context["gate"]
    assert "PATH.css" in assistant_context["fast_file_check"]
    assert original == {
        path.relative_to(ROOT): path.read_bytes()
        for path in (ROOT / "examples/assistant-app").rglob("*")
        if path.is_file()
    }

    with tempfile.TemporaryDirectory(prefix="adsys-project-verification-") as temporary:
        directory = Path(temporary).resolve()
        manifest = directory / MANIFEST.name
        shutil.copy2(MANIFEST, manifest)
        shutil.copy2(
            ROOT / "examples/aurora.design-system.json",
            directory / "aurora.design-system.json",
        )
        app = directory / "assistant-app"
        shutil.copytree(ROOT / "examples/assistant-app", app)

        (app / "nested").mkdir()
        (app / "nested" / "new.CSS").write_text(".x { color: #123456; }")
        drift = check(manifest, expected=1)
        assert any(
            issue["path"] == "nested/new.CSS" and issue["code"] == "css-color"
            for issue in drift["issues"]
        )
        (app / "nested" / "new.CSS").unlink()

        tokens = app / "tokens.css"
        approved = tokens.read_bytes()
        tokens.write_bytes(approved.replace(b"16px", b"17px"))
        assert any(
            issue["code"] == "tokens-stale"
            for issue in check(manifest, expected=1)["issues"]
        )
        tokens.write_bytes(approved)

        second = app / "second.html"
        second.write_text('<!doctype html><link rel="stylesheet" href="layout.css">')
        assert any(
            issue["path"] == "second.html" and issue["code"] == "tokens-unreferenced"
            for issue in check(manifest, expected=1)["issues"]
        )
        second.unlink()

        index = app / "index.html"
        index.write_text(
            index.read_text().replace(
                "</body>", '<p style="color:red">drift</p></body>'
            )
        )
        assert any(
            issue["code"] == "html-unverified-attribute"
            for issue in check(manifest, expected=1)["issues"]
        )
        (app / "app.tsx").write_text("export const App = () => null")
        assert any(
            issue["code"] == "adapter-required"
            for issue in check(manifest, expected=1)["issues"]
        )

        policy = json.loads(manifest.read_text())
        policy["exclusions"] = [{"path": "app.tsx", "reason": "review requested"}]
        manifest.write_text(json.dumps(policy))
        assert any(
            issue["code"] == "contract-mismatch"
            for issue in check(manifest, expected=1)["issues"]
        )
    print(
        "PASS project CSS workflow: full discovery, pinned contract, token parity, inline and unsupported coverage, and unchanged source"
    )


if __name__ == "__main__":
    main()
