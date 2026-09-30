"""Build an app-authored React entry from source-gated, pinned source files."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "examples/rendered-react.project.json"
SYSTEM = ROOT / "examples/aurora.design-system.json"
SOURCE = ROOT / "examples/rendered-react-source"
BUILDER = ROOT / "scripts/react-fixture"
CONTRACT_SHA256 = "dc462ea8b33834870ede719fc01c74ee5693da3f0c141355cb0cadbc620b9f2a"
BUILDER_SHA256 = "7b1ba44ded6fb4eb87958da3df1245a44b68d6c1ddc455911fc3c0ca0c19e5ad"
LOCK_SHA256 = "db2c3264f715b9358b6b2b5bd5d63c1f42ea3239fe36ddc3b892f7f39ce906e6"
PACKAGE_SHA256 = "4ae3d06da8a18c2df3f5bf1b4bbff9bd7c3c02c85bc785301d75399446109e3c"
BROWSER_SHA256 = "422b03507885a7072949576d83919f9f93470b1d87cf514a44fd98b1036551b3"
SOURCE_FILES = {
    "index.html",
    "main.tsx",
    "App.tsx",
    "tokens.css",
    "layout.css",
    "components/button.css",
}
OUTPUT_NAMES = {"index.html", "app.js", "app.css"}
MAX_RESPONSE = 8 * 1024 * 1024


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def trusted_tool(name: str) -> str:
    value = os.environ.get(name, "")
    require(
        value and Path(value).is_absolute(), f"{name} must be an explicit absolute path"
    )
    path = Path(value).resolve(strict=True)
    require(path.is_file(), f"{name} is not a regular file")
    return str(path)


def verified_source_hashes(report: dict[str, Any]) -> dict[str, str]:
    coverage = report["coverage"]
    entries = [
        *report["files"],
        *coverage["tsx_sources"],
        *coverage["trusted_sources"],
        *coverage["app_sources"],
    ]
    hashes = {entry["path"]: entry["sha256"] for entry in entries}
    require(
        len(hashes) == len(entries) and set(hashes) == SOURCE_FILES,
        "source gate inventory is incomplete",
    )
    return hashes


def verify_source_bytes(source: Path, expected: dict[str, str]) -> None:
    actual = {
        path.relative_to(source).as_posix(): sha256(path.read_bytes())
        for path in source.rglob("*")
        if path.is_file() and path.relative_to(source).parts[0] != ".git"
    }
    require(actual == expected, "source bytes differ from the inspected gate report")


def source_check(manifest: Path, node: str) -> dict[str, Any]:
    env = os.environ.copy()
    env["ADSF_NODE_BIN"] = node
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_design_system",
            "project-check",
            str(manifest),
            "--expected-sha256",
            CONTRACT_SHA256,
            "--format",
            "json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    if run.returncode != 0 or not run.stdout.strip():
        try:
            failed = json.loads(run.stdout)
            detail = ", ".join(
                f"{issue['path']}:{issue['code']}" for issue in failed["issues"]
            )
        except (ValueError, KeyError, TypeError):
            detail = (run.stderr or run.stdout).strip()[:1000]
        raise AssertionError(f"source gate failed: {detail}")
    try:
        return json.loads(run.stdout)
    except ValueError as error:
        raise AssertionError("source gate returned invalid JSON") from error


def install_builder(destination: Path, node: str, npm_cli: str) -> None:
    require(
        sha256((BUILDER / "build.cjs").read_bytes()) == BUILDER_SHA256,
        "builder script drift",
    )
    require(
        sha256((BUILDER / "package-lock.json").read_bytes()) == LOCK_SHA256,
        "build lock drift",
    )
    require(
        sha256((BUILDER / "package.json").read_bytes()) == PACKAGE_SHA256,
        "build package drift",
    )
    destination.mkdir()
    for name in ("build.cjs", "package.json", "package-lock.json"):
        shutil.copy2(BUILDER / name, destination / name)
    env = os.environ.copy()
    env.pop("NODE_OPTIONS", None)
    env.pop("NODE_PATH", None)
    run = subprocess.run(
        [node, npm_cli, "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=destination,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    require(run.returncode == 0, f"fresh locked build install failed: {run.stderr}")
    platforms = list((destination / "node_modules/@esbuild").glob("*/bin/esbuild"))
    require(len(platforms) == 1, "exactly one esbuild platform binary is required")


def run_builder(source: Path, builder: Path, node: str) -> dict[str, Any]:
    env = os.environ.copy()
    env.pop("NODE_OPTIONS", None)
    env.pop("NODE_PATH", None)
    run = subprocess.run(
        [node, str(builder / "build.cjs")],
        input=json.dumps({"root": str(source)}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=builder,
        env=env,
        timeout=30,
        check=False,
    )
    require(run.returncode == 0, f"locked build failed: {run.stderr}")
    require(len(run.stdout.encode()) <= MAX_RESPONSE, "build response exceeds limit")
    return json.loads(run.stdout)


def validate_build(
    response: dict[str, Any], source: Path, builder: Path
) -> dict[str, bytes]:
    require(
        set(response)
        == {
            "protocol",
            "builder_version",
            "react_version",
            "react_dom_version",
            "entry_sha256",
            "host_sha256",
            "metafile",
            "files",
        },
        "unexpected build envelope",
    )
    require(
        response["protocol"] == "adsf-react-fixture-build-v1", "wrong build protocol"
    )
    require(response["builder_version"] == "0.28.2", "wrong esbuild version")
    require(
        response["react_version"] == response["react_dom_version"] == "19.3.0",
        "wrong React version",
    )
    require(
        response["entry_sha256"] == sha256((source / "main.tsx").read_bytes()),
        "builder used the wrong app entry",
    )
    require(
        response["host_sha256"] == sha256((source / "index.html").read_bytes()),
        "builder used the wrong app host",
    )
    files: dict[str, bytes] = {}
    for item in response["files"]:
        require(set(item) == {"name", "sha256", "base64"}, "unexpected output field")
        name = item["name"]
        require(
            name in OUTPUT_NAMES and name not in files, "unexpected or duplicate output"
        )
        data = base64.b64decode(item["base64"], validate=True)
        require(
            base64.b64encode(data).decode() == item["base64"],
            "noncanonical build encoding",
        )
        require(sha256(data) == item["sha256"], "build output hash mismatch")
        files[name] = data
    require(set(files) == OUTPUT_NAMES, "build output inventory is incomplete")
    require(
        files["index.html"] == (source / "index.html").read_bytes(),
        "build output differs from the checked app host",
    )
    html = files["index.html"].decode("utf-8")
    require(
        html.count('<link rel="stylesheet" href="./app.css">') == 1,
        "built CSS is not linked",
    )
    require(
        html.count('<script type="module" src="./app.js"></script>') == 1,
        "built JS is not linked",
    )
    require("Content-Security-Policy" in html, "built host lacks CSP")
    meta = response["metafile"]
    require(
        isinstance(meta, dict) and set(meta) == {"inputs", "outputs"},
        "invalid build metafile",
    )
    source_inputs: set[str] = set()
    runtime_inputs: dict[str, str] = {}
    runtime_root = (builder / "node_modules").resolve(strict=True)
    for name in meta["inputs"]:
        resolved = (source / name).resolve(strict=True)
        if resolved.is_relative_to(source):
            source_inputs.add(resolved.relative_to(source).as_posix())
        else:
            require(
                resolved.is_relative_to(runtime_root), f"untrusted build input: {name}"
            )
            runtime_inputs[resolved.relative_to(runtime_root).as_posix()] = sha256(
                resolved.read_bytes()
            )
    require(
        source_inputs == SOURCE_FILES - {"index.html"},
        "source build graph is not closed",
    )
    require(
        runtime_inputs
        and all(
            path.startswith(("react/", "react-dom/", "scheduler/"))
            for path in runtime_inputs
        ),
        "unexpected runtime dependency",
    )
    require(len(meta["outputs"]) == 2, "unexpected emitted asset graph")
    emitted = {Path(name).name for name in meta["outputs"]}
    require(emitted == {"app.js", "app.css"}, "wrong emitted asset set")
    js_output = next(
        value for name, value in meta["outputs"].items() if name.endswith("app.js")
    )
    require(
        js_output.get("entryPoint") == "main.tsx",
        "built JavaScript did not use the checked app entry",
    )
    css_inputs = next(
        value["inputs"]
        for name, value in meta["outputs"].items()
        if name.endswith("app.css")
    )
    require(
        set(css_inputs) == {"tokens.css", "layout.css", "components/button.css"},
        "build dropped inspected CSS",
    )
    require(
        b"--color-action-primary: #2563eb" in files["app.css"],
        "generated token missing from CSS",
    )
    require(
        b"background-color:var(--color-action-primary)" in files["app.css"],
        "button token binding missing from CSS",
    )
    return files


def verify_artifacts(output: Path, expected: dict[str, bytes]) -> None:
    actual = {path.name for path in output.iterdir()}
    require(
        actual == set(expected), f"output inventory drift: {actual ^ set(expected)}"
    )
    for name, data in expected.items():
        path = output / name
        require(
            path.is_file() and not path.is_symlink(), f"not a regular artifact: {name}"
        )
        require(path.read_bytes() == data, f"artifact bytes drifted: {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=SOURCE,
        help="app source directory to inspect and build (default: checked-in fixture)",
    )
    parser.add_argument(
        "--output", type=Path, help="write verified app assets to a new directory"
    )
    parser.add_argument(
        "--attestation",
        type=Path,
        help="write a browser attestation to a new JSON file",
    )
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    require(source.is_dir(), "source must be a directory")
    require(
        all(not path.is_symlink() for path in source.rglob("*")),
        "source tree must not contain symlinks",
    )
    require(
        args.attestation is None or args.output is not None,
        "attestation requires --output",
    )
    node = trusted_tool("ADSF_NODE_BIN")
    npm_cli = trusted_tool("ADSF_NPM_CLI")
    require(
        sha256((BUILDER / "browser-verify.cjs").read_bytes()) == BROWSER_SHA256,
        "browser verifier drift",
    )
    with tempfile.TemporaryDirectory(prefix="adsf-rendered-react-") as temporary:
        area = Path(temporary).resolve()
        project = area / "project"
        project.mkdir()
        shutil.copy2(MANIFEST, project / MANIFEST.name)
        shutil.copy2(SYSTEM, project / SYSTEM.name)
        shutil.copytree(source, project / SOURCE.name)
        before = {
            p.relative_to(project): p.read_bytes()
            for p in project.rglob("*")
            if p.is_file()
        }
        report = source_check(project / MANIFEST.name, node)
        require(
            report["status"] == "pass" and report["contract_sha256"] == CONTRACT_SHA256,
            "source gate did not pass reviewed contract",
        )
        coverage = report["coverage"]
        require(
            coverage["tsx_files"] == 1
            and coverage["css_files"] == 2
            and coverage["html_files"] == 0
            and {(item["path"], item["role"]) for item in coverage["app_sources"]}
            == {("index.html", "host"), ("main.tsx", "entry")},
            "wrong source coverage",
        )
        require(
            not coverage["unverified_files"] and not coverage["exclusions"],
            "uninspected fixture source",
        )
        source_hashes = verified_source_hashes(report)
        verify_source_bytes(project / SOURCE.name, source_hashes)
        snapshot = area / "source-snapshot"
        shutil.copytree(project / SOURCE.name, snapshot)
        verify_source_bytes(snapshot, source_hashes)
        changed = snapshot / "components/button.css"
        original = changed.read_bytes()
        changed.write_bytes(original + b"\n.primary-button{background:red!important}\n")
        try:
            verify_source_bytes(snapshot, source_hashes)
        except AssertionError as error:
            require(
                "source bytes differ" in str(error),
                "source mutation failed for an unrelated reason",
            )
        else:
            raise AssertionError("uninspected source mutation unexpectedly passed")
        changed.write_bytes(original)
        verify_source_bytes(snapshot, source_hashes)
        builder = area / "trusted-builder"
        install_builder(builder, node, npm_cli)
        first = run_builder(snapshot, builder, node)
        artifacts = validate_build(first, snapshot, builder)
        second_snapshot = area / "independent-source-location"
        shutil.copytree(project / SOURCE.name, second_snapshot)
        verify_source_bytes(second_snapshot, source_hashes)
        second = run_builder(second_snapshot, builder, node)
        require(
            artifacts == validate_build(second, second_snapshot, builder),
            "rebuild differs across source paths",
        )
        verify_source_bytes(snapshot, source_hashes)
        verify_source_bytes(second_snapshot, source_hashes)
        output = area / "attested-output"
        output.mkdir()
        for name, data in artifacts.items():
            (output / name).write_bytes(data)
        verify_artifacts(output, artifacts)
        (output / "app.css").write_bytes(
            artifacts["app.css"] + b"\n.primary-button{background:red!important}\n"
        )
        try:
            verify_artifacts(output, artifacts)
        except AssertionError as error:
            require(
                "artifact bytes drifted" in str(error),
                "CSS mutation failed for an unrelated reason",
            )
        else:
            raise AssertionError("mutated built CSS unexpectedly passed")
        (output / "app.css").write_bytes(artifacts["app.css"])
        (output / "extra.js").write_text("// unreviewed")
        try:
            verify_artifacts(output, artifacts)
        except AssertionError as error:
            require(
                "inventory drift" in str(error),
                "extra asset failed for an unrelated reason",
            )
        else:
            raise AssertionError("extra emitted asset unexpectedly passed")
        (output / "extra.js").unlink()
        verify_artifacts(output, artifacts)
        require(
            before
            == {
                p.relative_to(project): p.read_bytes()
                for p in project.rglob("*")
                if p.is_file()
            },
            "source check/build changed the project",
        )
        if args.output is not None:
            destination = args.output.absolute()
            require(not destination.exists(), "output path must not already exist")
            destination.mkdir(parents=True)
            for name, data in artifacts.items():
                (destination / name).write_bytes(data)
            verify_artifacts(destination, artifacts)
        attestation = {
            "format": "adsf-rendered-app-attestation-v1",
            "contract_sha256": CONTRACT_SHA256,
            "host_sha256": source_hashes["index.html"],
            "entry_sha256": source_hashes["main.tsx"],
            "source_sha256": dict(sorted(source_hashes.items())),
            "artifact_sha256": {
                name: sha256(data) for name, data in sorted(artifacts.items())
            },
            "builder_sha256": BUILDER_SHA256,
            "browser_verifier_sha256": BROWSER_SHA256,
        }
        if args.attestation is not None:
            attestation_path = args.attestation.absolute()
            require(
                not attestation_path.exists(), "attestation path must not already exist"
            )
            require(
                not attestation_path.is_relative_to(destination),
                "attestation must be outside output assets",
            )
            attestation_path.write_text(
                json.dumps(attestation, indent=2, sort_keys=True) + "\n"
            )
        print(
            json.dumps(
                {
                    "status": "pass",
                    "scope": "one static React app with exact pinned authored host/entry and policy-checked App/CSS; browser result separate",
                    "contract_sha256": CONTRACT_SHA256,
                    "builder_sha256": BUILDER_SHA256,
                    "lock_sha256": LOCK_SHA256,
                    "package_sha256": PACKAGE_SHA256,
                    "browser_verifier_sha256": BROWSER_SHA256,
                    "source_sha256": dict(sorted(source_hashes.items())),
                    "artifacts": {
                        name: sha256(data) for name, data in sorted(artifacts.items())
                    },
                    "esbuild_version": first["builder_version"],
                    "react_version": first["react_version"],
                    "negative_checks": [
                        "source-byte-drift",
                        "built-css-byte-drift",
                        "extra-built-asset",
                    ],
                    "output": str(args.output.absolute())
                    if args.output is not None
                    else None,
                    "attestation": str(args.attestation.absolute())
                    if args.attestation is not None
                    else None,
                },
                indent=2,
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
