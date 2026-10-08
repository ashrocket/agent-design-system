"""Advisory Tailwind color audit for TSX source and an authored @theme block.

This intentionally reports coverage gaps; it is not an application-wide design
system conformance result. TypeScript is an optional, exact-pinned sidecar.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import stat
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from .format import FormatError, load_json
from .tsx import ADAPTER_DIR, PARSER_VERSION, _node_binary, _verify_installed_parser

ADAPTER = ADAPTER_DIR / "tailwind.cjs"
MAX_FILES = 1_000
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_FINDINGS = 5_000
COLOR_UTILITIES = (
    "placeholder", "decoration", "outline", "accent", "border", "divide",
    "stroke", "shadow", "caret", "fill", "from", "ring", "text", "bg", "via", "to",
)
DEFAULT_PALETTES = frozenset(
    "slate gray zinc neutral stone red orange amber yellow lime green emerald "
    "teal cyan sky blue indigo violet purple fuchsia pink rose".split()
)
NONCOLOR_SUFFIXES = {
    "text": frozenset(
        "xs sm base lg xl 2xl 3xl 4xl 5xl 6xl 7xl 8xl 9xl left center right "
        "justify start end wrap nowrap ellipsis clip balance pretty".split()
    ),
    "bg": frozenset(
        "cover contain center top bottom left right fixed local scroll repeat no-repeat "
        "repeat-x repeat-y auto none clip-border clip-padding clip-content origin-border "
        "origin-padding origin-content".split()
    ),
}
HEX = re.compile(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?$")
HEX_WITH_ALPHA = re.compile(r"#[0-9a-fA-F]{4}(?:[0-9a-fA-F]{4})?$")
THEME_START = re.compile(r"@theme\s*\{")
THEME_TOKEN = re.compile(r"--color-([A-Za-z0-9_-]+)\s*:\s*([^;{}]+);")
THEME_NAME = re.compile(r"(?m)(?:^|;)[ \t\r\n]*(--color-[A-Za-z0-9_-]+)")


def _direct_file(path: Path, label: str) -> Path:
    try:
        mode = path.lstat().st_mode
    except OSError as error:
        raise FormatError(f"missing {label}: {path}") from error
    if not stat.S_ISREG(mode):
        raise FormatError(f"{label} must be a direct regular file: {path}")
    return path


def _hex(value: str) -> str:
    color = value.strip().lower()
    if not color.startswith("#"):
        color = "#" + color
    if not HEX.fullmatch(color):
        raise FormatError(f"invalid canonical hex color: {value}")
    if len(color) == 4:
        color = "#" + "".join(part * 2 for part in color[1:])
    return color


def _literal_rgb(value: str) -> str | None:
    if HEX.fullmatch(value):
        return _hex(value)
    if HEX_WITH_ALPHA.fullmatch(value):
        digits = value[1:]
        return _hex("#" + (digits[:3] if len(digits) == 4 else digits[:6]))
    return None


def _canon_colors(path: Path, dotted_path: str) -> set[str]:
    value: Any = load_json(_direct_file(path, "canon"))
    if not dotted_path or any(not part for part in dotted_path.split(".")):
        raise FormatError("canon path must name a JSON array")
    try:
        for part in dotted_path.split("."):
            value = value[part]
    except (KeyError, TypeError) as error:
        raise FormatError("canon path does not exist") from error
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) for item in value
    ):
        raise FormatError("canon path must contain a nonempty color array")
    return {_hex(item) for item in value}


def _theme_colors(path: Path) -> dict[str, tuple[str, int]]:
    source = _direct_file(path, "theme CSS").read_text(encoding="utf-8")
    # Comments cannot introduce declarations or braces. Preserve other source
    # byte positions; this scanner inspects only flat @theme declarations.
    masked = re.sub(r"/\*.*?\*/", lambda match: " " * len(match.group()), source, flags=re.S)
    blocks = list(THEME_START.finditer(masked))
    if len(blocks) != 1:
        raise FormatError("theme CSS must contain exactly one @theme block")
    start = blocks[0].end()
    depth = 1
    end = start
    while end < len(masked) and depth:
        if masked[end] == "{":
            depth += 1
        elif masked[end] == "}":
            depth -= 1
        end += 1
    if depth:
        raise FormatError("unclosed @theme block")
    body = masked[start : end - 1]
    if "{" in body or "}" in body:
        raise FormatError("nested @theme rules need a separate adapter")
    tokens: dict[str, tuple[str, int]] = {}
    declarations = list(THEME_TOKEN.finditer(body))
    if {match.start() for match in declarations} != {
        match.start(1) for match in THEME_NAME.finditer(body)
    }:
        raise FormatError("malformed @theme color declaration")
    for match in declarations:
        name, value = match.groups()
        if name in tokens:
            raise FormatError(f"duplicate @theme color token: {name}")
        tokens[name] = (value.strip(), source.count("\n", 0, start + match.start()) + 1)
    if not tokens:
        raise FormatError("@theme contains no --color-* declarations")
    return tokens


def _tsx_files(root: Path, excludes: tuple[str, ...]) -> list[Path]:
    if root.is_symlink() or not root.is_dir():
        raise FormatError("TSX root must be a direct directory")
    files: list[Path] = []
    for current, dirs, names in os.walk(root, followlinks=False):
        base = Path(current)
        for name in dirs + names:
            path = base / name
            if path.is_symlink():
                raise FormatError(f"symlink inside TSX root: {path}")
        for name in names:
            if not name.endswith(".tsx"):
                continue
            path = base / name
            relative = path.relative_to(root).as_posix()
            if any(fnmatch.fnmatch(relative, pattern) for pattern in excludes):
                continue
            _direct_file(path, "TSX source")
            files.append(path)
            if len(files) > MAX_FILES:
                raise FormatError("TSX inventory exceeds 1000 files")
    if not files:
        raise FormatError("TSX inventory is empty")
    return sorted(files)


def _inspect_tsx(path: Path, relative: str) -> dict[str, Any]:
    source = path.read_text(encoding="utf-8")
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise FormatError(f"TSX source exceeds 2 MiB: {relative}")
    if not ADAPTER.is_file():
        raise FormatError("Tailwind TSX adapter is missing")
    _verify_installed_parser()
    environment = os.environ.copy()
    environment.pop("NODE_OPTIONS", None)
    environment.pop("NODE_PATH", None)
    try:
        result = subprocess.run(
            [_node_binary(), "--max-old-space-size=256", str(ADAPTER)],
            input=json.dumps({"path": relative, "source": source}),
            capture_output=True, text=True, encoding="utf-8", timeout=15,
            cwd=ADAPTER_DIR, env=environment, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FormatError(f"Tailwind TSX parser unavailable: {relative}") from error
    if result.returncode or len(result.stdout.encode("utf-8")) > 4 * 1024 * 1024:
        raise FormatError(f"Tailwind TSX parser failed: {relative}")
    try:
        report = json.loads(result.stdout)
    except ValueError as error:
        raise FormatError(f"Tailwind TSX parser returned invalid JSON: {relative}") from error
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    if (
        not isinstance(report, dict)
        or set(report) != {
            "protocol", "parser_version", "source_sha256", "jsx_class_attributes",
            "dynamic_class_attributes", "classes", "issues",
        }
        or report["protocol"] != "adsf-tailwind-tsx-v1"
        or report["parser_version"] != PARSER_VERSION
        or report["source_sha256"] != digest
        or any(
            not isinstance(report[key], int) or isinstance(report[key], bool) or report[key] < 0
            for key in ("jsx_class_attributes", "dynamic_class_attributes")
        )
        or report["dynamic_class_attributes"] > report["jsx_class_attributes"]
        or not isinstance(report["classes"], list)
        or not isinstance(report["issues"], list)
        or len(report["classes"]) > 20_000
        or len(report["issues"]) > 1_000
    ):
        raise FormatError(f"invalid Tailwind TSX parser envelope: {relative}")
    for key, fields in (("classes", {"name", "line", "column"}),
                        ("issues", {"code", "line", "column", "message"})):
        for item in report[key]:
            if not isinstance(item, dict) or set(item) != fields or any(
                not isinstance(item[field], int) or isinstance(item[field], bool) or item[field] < 1
                for field in ("line", "column")
            ):
                raise FormatError(f"invalid Tailwind TSX {key}: {relative}")
            if not isinstance(item["name" if key == "classes" else "code"], str) or not item["name" if key == "classes" else "code"]:
                raise FormatError(f"invalid Tailwind TSX {key}: {relative}")
            if key == "issues" and not isinstance(item["message"], str):
                raise FormatError(f"invalid Tailwind TSX {key}: {relative}")
    return report


def _base_utility(token: str) -> str:
    """Strip Tailwind variants while respecting arbitrary selectors and values."""
    depth_square = depth_round = 0
    last = 0
    for index, char in enumerate(token):
        if char == "[":
            depth_square += 1
        elif char == "]":
            depth_square -= 1
        elif char == "(":
            depth_round += 1
        elif char == ")":
            depth_round -= 1
        elif char == ":" and not depth_square and not depth_round:
            last = index + 1
    return token[last:].lstrip("!- ")


def _color_utility(token: str) -> tuple[str, str] | None:
    base = _base_utility(token)
    for family in COLOR_UTILITIES:
        if base.startswith(family + "-"):
            suffix = base[len(family) + 1 :].split("/", 1)[0]
            if not suffix or suffix in NONCOLOR_SUFFIXES.get(family, ()):
                return None
            if family == "text" and (suffix.isdigit() or re.fullmatch(r"\d?xl", suffix)):
                return None
            if family == "bg" and suffix.startswith(("gradient-", "linear-", "radial-", "conic-")):
                return None
            if family in {"border", "ring", "outline", "stroke"} and (
                suffix.isdigit() or suffix in {"solid", "dashed", "dotted", "double", "none"}
            ):
                return None
            return family, suffix
    return None


def audit_tailwind(
    *, tsx_root: str | Path, theme_css: str | Path, canon: str | Path,
    canon_path: str, excludes: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return deterministic findings and explicit source-coverage counts."""
    root = Path(tsx_root).absolute()
    allowed = _canon_colors(Path(canon).absolute(), canon_path)
    theme = _theme_colors(Path(theme_css).absolute())
    files = _tsx_files(root, excludes)
    counts: Counter[str] = Counter()
    findings: list[dict[str, Any]] = []

    def finding(code: str, source: str, line: int, value: str) -> None:
        counts[code] += 1
        if len(findings) < MAX_FINDINGS:
            findings.append({"code": code, "source": source, "line": line, "value": value})

    for name, (value, line) in sorted(theme.items()):
        rgb = _literal_rgb(value)
        if rgb is not None:
            if rgb not in allowed:
                finding("theme-outside-canon", Path(theme_css).name, line, name)
        else:
            finding("theme-unresolved-color", Path(theme_css).name, line, name)

    class_attributes = dynamic_attributes = class_tokens = syntax_files = 0
    source_hashes: dict[str, str] = {}
    for path in files:
        relative = path.relative_to(root).as_posix()
        result = _inspect_tsx(path, relative)
        syntax_files += any(issue["code"] == "tsx-syntax" for issue in result["issues"])
        source_hashes[relative] = result["source_sha256"]
        class_attributes += result["jsx_class_attributes"]
        dynamic_attributes += result["dynamic_class_attributes"]
        class_tokens += len(result["classes"])
        for issue in result["issues"]:
            finding(issue["code"], relative, issue["line"], "")
        for item in result["classes"]:
            utility = _color_utility(item["name"])
            if utility is None:
                continue
            _family, suffix = utility
            if suffix in theme or suffix in {"transparent", "current", "inherit"}:
                continue
            if suffix in {"white", "black"}:
                if {"white": "#ffffff", "black": "#000000"}[suffix] not in allowed:
                    finding("builtin-color-outside-canon", relative, item["line"], item["name"])
                continue
            if suffix.startswith("[") and suffix.endswith("]"):
                value = suffix[1:-1]
                if value.startswith("color:"):
                    value = value[6:]
                rgb = _literal_rgb(value)
                if rgb is not None:
                    if rgb not in allowed:
                        finding("arbitrary-color-outside-canon", relative, item["line"], item["name"])
                elif value.startswith(("var(", "oklch(", "rgb(", "rgba(", "hsl(", "hsla(", "color-mix(", "color:")):
                    finding("arbitrary-color-unresolved", relative, item["line"], item["name"])
                continue
            parts = suffix.split("-")
            if len(parts) >= 2 and parts[0] in DEFAULT_PALETTES and parts[-1].isdigit():
                finding("default-palette-color", relative, item["line"], item["name"])
            elif suffix.startswith("color-") or _family in {"bg", "fill", "stroke", "from", "via", "to"}:
                finding("unknown-color-token", relative, item["line"], item["name"])
    return {
        "format": "adsf-tailwind-audit", "version": "0.1.0",
        "coverage": {
            "tsx_files": len(files), "jsx_class_attributes": class_attributes,
            "dynamic_class_attributes": dynamic_attributes,
            "static_class_tokens": class_tokens, "theme_color_tokens": len(theme),
            "tsx_files_with_syntax_errors": syntax_files,
            "uninspected": "non-TSX sources, runtime/generated class names, and non-color Tailwind utilities",
        },
        "finding_counts": dict(sorted(counts.items())),
        "findings": findings,
        "findings_truncated": sum(counts.values()) - len(findings),
        "source_sha256": source_hashes,
        "adapter_sha256": hashlib.sha256(ADAPTER.read_bytes()).hexdigest(),
    }
