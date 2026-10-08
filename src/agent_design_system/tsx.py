"""Optional, bounded TypeScript parser for the static TSX source profile."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .format import FormatError

PROFILE = "static-jsx-v1"
PARSER_VERSION = "6.0.3"
PARSER_INTEGRITY = "sha512-y2TvuxSZPDyQakkFRPZHKFm+KKVqIisdg9/CZwm9ftvKXLP8NRWj38/ODjNbr43SsoXqNuAisEf1GdCxqWcdBw=="
PARSER_PACKAGE_SHA256 = (
    "9332e97c30d3e53ed54910b89207ed657fb444066484df6e5b6965bf130865e9"
)
PARSER_SCRIPT_SHA256 = (
    "569177652966bd528c319171c7dd22860dbf72bde116cbc4f644f1d02bb12e39"
)
MAX_TSX_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
ADAPTER_DIR = Path(__file__).parent / "adapters" / "tsx"
ADAPTER_SCRIPT = ADAPTER_DIR / "check.cjs"
ADAPTER_LOCK = ADAPTER_DIR / "package-lock.json"
PARSER_PACKAGE = ADAPTER_DIR / "node_modules" / "typescript" / "package.json"
PARSER_SCRIPT = ADAPTER_DIR / "node_modules" / "typescript" / "lib" / "typescript.js"


class AdapterDriftError(FormatError):
    """The installed adapter differs from the reviewed manifest pin."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _location(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _node_binary() -> str:
    """Use a runner-selected executable, never a project-controlled PATH entry."""
    configured = os.environ.get("ADSF_NODE_BIN", "")
    path = Path(configured)
    try:
        if path.is_absolute() and path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve(strict=True))
    except OSError:
        pass
    raise FormatError("set ADSF_NODE_BIN to a trusted absolute Node executable")


def _verify_installed_parser() -> None:
    try:
        if (
            PARSER_PACKAGE.is_file()
            and PARSER_SCRIPT.is_file()
            and _sha256(PARSER_PACKAGE) == PARSER_PACKAGE_SHA256
            and _sha256(PARSER_SCRIPT) == PARSER_SCRIPT_SHA256
        ):
            return
    except OSError:
        pass
    raise AdapterDriftError(
        "installed TypeScript parser differs from the reviewed package"
    )


def inspect_static_tsx(
    path: str,
    source: str,
    digest: str,
    *,
    expected_adapter_sha256: str | None = None,
    expected_lock_sha256: str | None = None,
) -> dict[str, Any]:
    """Parse source as data; return a validated sidecar result or fail closed."""
    if len(source.encode("utf-8")) > MAX_TSX_BYTES:
        raise FormatError("TSX source exceeds the 1 MiB parser limit")
    if not ADAPTER_SCRIPT.is_file() or not ADAPTER_LOCK.is_file():
        raise FormatError("static TSX adapter package is missing")
    try:
        adapter_sha256 = _sha256(ADAPTER_SCRIPT)
        lock_sha256 = _sha256(ADAPTER_LOCK)
    except OSError as error:
        raise FormatError("static TSX adapter package is unreadable") from error
    if (
        expected_adapter_sha256 is not None
        and adapter_sha256 != expected_adapter_sha256
    ) or (expected_lock_sha256 is not None and lock_sha256 != expected_lock_sha256):
        raise AdapterDriftError(
            "static TSX adapter or lock differs from the reviewed manifest pin"
        )
    try:
        lock = json.loads(ADAPTER_LOCK.read_text())
        package = lock["packages"]["node_modules/typescript"]
        if (
            package["version"] != PARSER_VERSION
            or package["integrity"] != PARSER_INTEGRITY
        ):
            raise FormatError("static TSX parser lock does not pin TypeScript 6.0.3")
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise FormatError("invalid static TSX parser lock") from error
    _verify_installed_parser()
    node_binary = _node_binary()
    environment = os.environ.copy()
    environment.pop("NODE_OPTIONS", None)
    environment.pop("NODE_PATH", None)
    try:
        completed = subprocess.run(
            [node_binary, "--max-old-space-size=128", str(ADAPTER_SCRIPT)],
            input=json.dumps({"path": path, "source": source}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
            cwd=ADAPTER_DIR,
            env=environment,
        )
    except (
        FileNotFoundError,
        subprocess.TimeoutExpired,
        OSError,
        UnicodeError,
    ) as error:
        raise FormatError("static TSX parser is unavailable or timed out") from error
    if completed.returncode != 0:
        raise FormatError(
            "static TSX parser failed; run npm ci in the adapter directory"
        )
    if len(completed.stdout.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise FormatError("static TSX parser output exceeds the limit")
    try:
        report = json.loads(completed.stdout)
    except ValueError as error:
        raise FormatError("static TSX parser returned invalid JSON") from error
    if (
        not isinstance(report, dict)
        or set(report)
        != {
            "protocol",
            "parser_version",
            "source_sha256",
            "imports",
            "classes",
            "issues",
        }
        or report["protocol"] != "adsf-static-tsx-v1"
        or report["parser_version"] != PARSER_VERSION
        or report["source_sha256"] != digest
        or not isinstance(report["imports"], list)
        or not isinstance(report["classes"], list)
        or not isinstance(report["issues"], list)
        or len(report["imports"]) > 5_000
        or len(report["classes"]) > 5_000
        or len(report["issues"]) > 100
    ):
        raise FormatError("static TSX parser returned an invalid envelope")
    if not all(isinstance(item, str) for item in report["imports"]):
        raise FormatError("static TSX parser returned invalid imports")
    if not all(
        isinstance(item, dict)
        and set(item) == {"name", "line", "column"}
        and isinstance(item["name"], str)
        and _location(item["line"])
        and _location(item["column"])
        for item in report["classes"]
    ):
        raise FormatError("static TSX parser returned invalid class references")
    if not all(
        isinstance(item, dict)
        and set(item) == {"code", "message", "line", "column"}
        and isinstance(item["code"], str)
        and isinstance(item["message"], str)
        and _location(item["line"])
        and _location(item["column"])
        for item in report["issues"]
    ):
        raise FormatError("static TSX parser returned invalid issues")
    report["adapter_sha256"] = adapter_sha256
    report["lock_sha256"] = lock_sha256
    return report
