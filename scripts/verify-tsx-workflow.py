"""Exercise the optional AST-backed static TSX project profile end to end."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "examples/assistant-app.project.json"
ADAPTER_SHA256 = "362745c4ebe8743031eb07121b81766aeb26064e40f797378a2830653abd6318"
LOCK_SHA256 = "27addf51ab3806b6e7cc4fa6506d80f782c93a71dba05ac3fcf5dcd78aad42a4"
CONTRACT_SHA256 = "ca054b52cff9c76d012c7654d0483af8164df8780b45afd52bd7cb4f254e5817"
APP_SOURCE = """import "./tokens.css";
import "./layout.css";
import "./components/button.css";
export default function App() {
  return <main className="stage"><section className="card"><button className="primary-button" type="button">Review</button></section></main>;
}
"""


def check(
    manifest: Path,
    *,
    expected: int,
    pin: str | None = None,
    no_node: bool = False,
    path_prefix: Path | None = None,
) -> dict:
    command = [
        sys.executable,
        "-m",
        "agent_design_system",
        "project-check",
        str(manifest),
        "--format",
        "json",
    ]
    if pin:
        command.extend(["--expected-sha256", pin])
    environment = os.environ.copy()
    if path_prefix is not None:
        environment["PATH"] = (
            str(path_prefix) + os.pathsep + environment.get("PATH", "")
        )
    if no_node:
        environment["ADSF_NODE_BIN"] = "/nonexistent/node"
    else:
        node = shutil.which("node")
        if node is None:
            raise AssertionError("Node is required for the static TSX verifier")
        environment["ADSF_NODE_BIN"] = str(Path(node).resolve())
    run = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    if run.returncode != expected:
        raise AssertionError(
            f"expected exit {expected}, got {run.returncode}: {run.stdout}\n{run.stderr}"
        )
    return json.loads(run.stdout)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="adsys-tsx-verification-") as temporary:
        directory = Path(temporary).resolve()
        manifest = directory / MANIFEST.name
        shutil.copy2(MANIFEST, manifest)
        shutil.copy2(
            ROOT / "examples/aurora.design-system.json",
            directory / "aurora.design-system.json",
        )
        app = directory / "assistant-app"
        shutil.copytree(ROOT / "examples/assistant-app", app)
        policy = json.loads(manifest.read_text())
        policy["tsx"] = {
            "profile": "static-jsx-v1",
            "adapter_sha256": ADAPTER_SHA256,
            "lock_sha256": LOCK_SHA256,
        }
        manifest.write_text(json.dumps(policy))
        component = app / "App.tsx"
        component.write_text(APP_SOURCE)
        before = {
            path.relative_to(directory): path.read_bytes()
            for path in directory.rglob("*")
            if path.is_file()
        }
        passing = check(manifest, expected=0)
        pin = CONTRACT_SHA256
        assert passing["status"] == "pass"
        assert passing["contract_sha256"] == pin
        assert passing["coverage"]["tsx_files"] == 1
        tsx = passing["coverage"]["tsx_sources"][0]
        assert tsx["parser_version"] == "6.0.3"
        assert tsx["adapter_sha256"] == ADAPTER_SHA256
        assert tsx["lock_sha256"] == LOCK_SHA256
        assert tsx["css_imports"] == [
            "tokens.css",
            "layout.css",
            "components/button.css",
        ]
        assert [item["name"] for item in tsx["classes"]] == [
            "stage",
            "card",
            "primary-button",
        ]
        assert (
            len(tsx["sha256"])
            == len(tsx["adapter_sha256"])
            == len(tsx["lock_sha256"])
            == 64
        )
        assert before == {
            path.relative_to(directory): path.read_bytes()
            for path in directory.rglob("*")
            if path.is_file()
        }
        assert check(manifest, expected=0, pin=pin)["status"] == "pass"
        spoof = directory / "fake-bin"
        spoof.mkdir()
        fake_node = spoof / "node"
        fake_node.write_text("#!/bin/sh\nexit 91\n")
        fake_node.chmod(0o755)
        assert (
            check(manifest, expected=0, pin=pin, path_prefix=spoof)["status"] == "pass"
        )

        mutations = [
            (
                APP_SOURCE.replace('className="stage"', 'className="unknown"'),
                "tsx-class-unverified",
            ),
            (
                APP_SOURCE.replace('className="primary-button"', ""),
                "tsx-class-missing",
            ),
            (APP_SOURCE.replace('import "./layout.css";', ""), "tsx-class-unverified"),
            (APP_SOURCE.replace('import "./tokens.css";', ""), "tsx-tokens-unimported"),
            (
                APP_SOURCE.replace(
                    '<main className="stage">',
                    '<main className="stage"><style>red</style>',
                ),
                "tsx-tag-unsupported",
            ),
            (
                APP_SOURCE.replace(
                    '<main className="stage">',
                    '<main className="stage"><link rel="stylesheet" href="https://example.com/x.css" />',
                ),
                "tsx-tag-unsupported",
            ),
            (
                APP_SOURCE.replace(
                    '<main className="stage">',
                    '<main className="stage"><svg fill="red" />',
                ),
                "tsx-tag-unsupported",
            ),
            (
                APP_SOURCE.replace(
                    'className="stage"', 'className="stage" style={{color:"red"}}'
                ),
                "tsx-attribute-unsupported",
            ),
            (
                APP_SOURCE.replace('className="stage"', 'className="stage" {...props}'),
                "tsx-attribute-unsupported",
            ),
            (
                APP_SOURCE.replace('className="stage"', 'className={"stage"}'),
                "tsx-attribute-dynamic",
            ),
            (
                APP_SOURCE.replace(">Review</button>", ">{makeStyle()}</button>"),
                "tsx-expression-unsupported",
            ),
            (
                APP_SOURCE.replace(
                    'className="stage"', 'className="stage" onClick="run"'
                ),
                "tsx-attribute-unsupported",
            ),
            (
                APP_SOURCE.replace(
                    '<main className="stage">',
                    '<main className="stage"><Custom className="stage" />',
                ),
                "tsx-tag-unsupported",
            ),
            (
                APP_SOURCE.replace(
                    "function App()",
                    'function App(x = document.body.style.color = "red")',
                ),
                "tsx-function-unsupported",
            ),
            (
                APP_SOURCE.replace(
                    "  return <main",
                    '  document.body.style.color = "red";\n  return <main',
                ),
                "tsx-function-unsupported",
            ),
            ('import "untrusted-package";\n' + APP_SOURCE, "tsx-import-unsupported"),
            ("export const unwanted = 1;\n" + APP_SOURCE, "tsx-statement-unsupported"),
            ('import "../outside.css";\n' + APP_SOURCE, "tsx-import-unverified"),
            (
                APP_SOURCE.replace('className="stage"', 'className="&#x73;tage"'),
                "tsx-attribute-encoding",
            ),
            (
                "/** @jsxImportSource untrusted */\n" + APP_SOURCE,
                "tsx-pragma-unsupported",
            ),
            ("/** @jsx untrustedFactory */\n" + APP_SOURCE, "tsx-pragma-unsupported"),
            (
                '/// <amd-dependency path="untrusted/style-mutator" />\n' + APP_SOURCE,
                "tsx-pragma-unsupported",
            ),
        ]
        for source, code in mutations:
            component.write_text(source)
            failed = check(manifest, expected=1, pin=pin)
            if not any(issue["code"] == code for issue in failed["issues"]):
                raise AssertionError(f"{code} missing from {failed['issues']}")

        component.write_text(APP_SOURCE)
        policy["tsx"]["adapter_sha256"] = "0" * 64
        manifest.write_text(json.dumps(policy))
        drifted = check(manifest, expected=1, pin=pin)
        assert any(issue["code"] == "tsx-adapter-drift" for issue in drifted["issues"])
        policy["tsx"]["adapter_sha256"] = ADAPTER_SHA256
        policy["tsx"]["lock_sha256"] = "0" * 64
        manifest.write_text(json.dumps(policy))
        drifted = check(manifest, expected=1, pin=pin)
        assert any(issue["code"] == "tsx-adapter-drift" for issue in drifted["issues"])
        policy["tsx"]["lock_sha256"] = LOCK_SHA256
        manifest.write_text(json.dumps(policy))
        unavailable = check(manifest, expected=1, pin=pin, no_node=True)
        assert any(
            issue["code"] == "tsx-adapter-error" for issue in unavailable["issues"]
        )
        print(
            "PASS static TSX workflow: pinned AST parser, CSS import and class linkage, adversarial syntax rejection, missing-parser failure, and read-only source"
        )


if __name__ == "__main__":
    main()
