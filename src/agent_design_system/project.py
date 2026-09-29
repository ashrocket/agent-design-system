"""Read-only project-wide CSS discovery and coverage accounting."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .css import enforce_css, render_tokens
from .format import FormatError, load_json

UNVERIFIED_SUFFIXES = {
    ".astro",
    ".cjs",
    ".cts",
    ".ejs",
    ".erb",
    ".hbs",
    ".js",
    ".jsx",
    ".less",
    ".liquid",
    ".mjs",
    ".mts",
    ".mdx",
    ".php",
    ".sass",
    ".scss",
    ".styl",
    ".svelte",
    ".svg",
    ".ts",
    ".tsx",
    ".twig",
    ".vue",
}
PRESENTATION_ATTRIBUTES = {
    "align",
    "alink",
    "background",
    "bgcolor",
    "border",
    "cellpadding",
    "cellspacing",
    "clear",
    "color",
    "face",
    "fill",
    "height",
    "hspace",
    "link",
    "marginheight",
    "marginwidth",
    "nowrap",
    "size",
    "stroke",
    "text",
    "valign",
    "vlink",
    "vspace",
    "width",
}


def _relative(value: Any, label: str, *, root: bool = False) -> tuple[str, ...]:
    if root and value == ".":
        return ()
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or "\\" in value
        or ":" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise FormatError(f"{label} must be a canonical relative POSIX path")
    return tuple(value.split("/"))


def _declared(
    base: Path, parts: tuple[str, ...], label: str, *, missing: bool = False
) -> tuple[Path, bool]:
    current = base
    for index, part in enumerate(parts):
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            if missing:
                return base.joinpath(*parts), False
            raise FormatError(f"missing {label}: {current}") from None
        if stat.S_ISLNK(mode):
            raise FormatError(f"symlink is forbidden in {label}: {current}")
        if index < len(parts) - 1 and not stat.S_ISDIR(mode):
            raise FormatError(f"non-directory in {label}: {current}")
    return current, True


def _overlap(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    return a[: len(b)] == b or b[: len(a)] == a


def _issue(issues: list[dict[str, str]], path: str, code: str, message: str) -> None:
    issues.append({"path": path, "code": code, "message": message})


class _HTMLCoverage(HTMLParser):
    def __init__(self, source: str, issues: list[dict[str, str]]) -> None:
        super().__init__(convert_charrefs=True)
        self.source = source
        self.issues = issues
        self.stylesheets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        seen: set[str] = set()
        for name, _ in attrs:
            if name in seen:
                _issue(
                    self.issues,
                    self.source,
                    "html-duplicate-attribute",
                    f"duplicate {name} attribute has ambiguous browser behavior",
                )
            seen.add(name)
        attributes = dict(attrs)
        if tag in {
            "base",
            "style",
            "script",
            "svg",
            "iframe",
            "object",
            "embed",
            "font",
        }:
            _issue(
                self.issues,
                self.source,
                "html-unverified-element",
                f"<{tag}> needs a separate style adapter",
            )
        for name in attributes:
            if (
                name == "style"
                or name.startswith("on")
                or name in PRESENTATION_ATTRIBUTES
                or name == "srcdoc"
            ):
                _issue(
                    self.issues,
                    self.source,
                    "html-unverified-attribute",
                    f"{name} is an uninspected styling or execution surface",
                )
        if (
            tag == "link"
            and "stylesheet" in (attributes.get("rel") or "").lower().split()
        ):
            self.stylesheets.append(attributes.get("href") or "")

    handle_startendtag = handle_starttag


def check_project(
    manifest_path: str | Path, *, expected_sha256: str | None = None
) -> dict[str, Any]:
    """Discover supported source; report drift and unverified surfaces without writes."""
    manifest_file = Path(manifest_path).absolute()
    if manifest_file.is_symlink() or not manifest_file.is_file():
        raise FormatError("project manifest must be a regular, direct file")
    manifest = load_json(manifest_file)
    if not isinstance(manifest, dict) or set(manifest) != {
        "format",
        "version",
        "root",
        "system",
        "tokens",
        "exclusions",
    }:
        raise FormatError(
            "project manifest requires format, version, root, system, tokens, exclusions only"
        )
    if manifest["format"] != "adsf-css-project" or manifest["version"] != "0.1.0":
        raise FormatError("unsupported CSS project manifest format or version")
    base = manifest_file.parent.resolve(strict=True)
    root_parts = _relative(manifest["root"], "root", root=True)
    system_parts = _relative(manifest["system"], "system")
    token_parts = _relative(manifest["tokens"], "tokens")
    if not token_parts[-1].lower().endswith(".css"):
        raise FormatError("tokens must identify a CSS file")
    root, _ = _declared(base, root_parts, "root")
    if not root.is_dir():
        raise FormatError("root must be a directory")
    system_file, _ = _declared(base, system_parts, "system")
    if not system_file.is_file():
        raise FormatError("system must be a regular file")
    token_file, token_exists = _declared(root, token_parts, "tokens", missing=True)
    if token_exists and not token_file.is_file():
        raise FormatError("tokens must be a regular file")
    raw_exclusions = manifest["exclusions"]
    if not isinstance(raw_exclusions, list):
        raise FormatError("exclusions must be a list")
    exclusions: list[tuple[tuple[str, ...], str, bool]] = []
    reserved: list[tuple[str, ...]] = [token_parts]
    for protected in (manifest_file, system_file):
        try:
            reserved.append(protected.relative_to(root).parts)
        except ValueError:
            pass
    for entry in raw_exclusions:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"path", "reason"}
            or not isinstance(entry["reason"], str)
            or not entry["reason"].strip()
        ):
            raise FormatError("each exclusion needs only a path and non-empty reason")
        parts = _relative(entry["path"], "exclusion")
        if (
            parts[0] == ".git"
            or any(_overlap(parts, other[0]) for other in exclusions)
            or any(_overlap(parts, item) for item in reserved)
        ):
            raise FormatError(f"overlapping or reserved exclusion: {entry['path']}")
        target, exists = _declared(root, parts, "exclusion", missing=True)
        if exists and not (target.is_file() or target.is_dir()):
            raise FormatError(f"exclusion must target a file or directory: {target}")
        exclusions.append((parts, entry["reason"].strip(), exists))
    if expected_sha256 is not None and not re.fullmatch(
        r"[0-9a-f]{64}", expected_sha256
    ):
        raise FormatError("expected contract SHA-256 must be 64 lowercase hex digits")
    system = load_json(system_file)
    token_bytes = render_tokens(system).encode("utf-8")
    fingerprint = hashlib.sha256(
        json.dumps(
            {"manifest": manifest, "system": system},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    issues: list[dict[str, str]] = []
    if expected_sha256 is not None and expected_sha256 != fingerprint:
        _issue(
            issues,
            manifest_file.name,
            "contract-mismatch",
            "system or manifest differs from the reviewed fingerprint",
        )
    if not token_exists:
        _issue(
            issues,
            manifest["tokens"],
            "tokens-missing",
            "generated token stylesheet is missing",
        )
    elif token_file.read_bytes() != token_bytes:
        _issue(
            issues,
            manifest["tokens"],
            "tokens-stale",
            "generated token stylesheet differs byte for byte",
        )

    files: list[dict[str, Any]] = []
    html: list[tuple[str, str, str]] = []
    unverified: list[str] = []
    built_in: list[str] = []
    excluded_seen: set[tuple[str, ...]] = set()

    def scan(directory: Path) -> None:
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            path = Path(entry.path)
            relative = path.relative_to(root).parts
            name = "/".join(relative)
            mode = entry.stat(follow_symlinks=False).st_mode
            if stat.S_ISLNK(mode):
                raise FormatError(f"symlink in project root: {name}")
            if entry.name == ".git" and stat.S_ISDIR(mode):
                built_in.append(name)
                continue
            omitted = next(
                (
                    parts
                    for parts, _, _ in exclusions
                    if relative[: len(parts)] == parts
                ),
                None,
            )
            if omitted is not None:
                excluded_seen.add(omitted)
                continue
            if stat.S_ISDIR(mode):
                scan(path)
                continue
            if not stat.S_ISREG(mode):
                raise FormatError(f"special file in project root: {name}")
            if path == token_file:
                continue
            suffix = path.suffix.lower()
            if suffix not in {".css", ".html", ".htm"} | UNVERIFIED_SUFFIXES:
                continue
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            try:
                source = content.decode("utf-8")
                if "\x00" in source:
                    raise UnicodeError("NUL byte")
            except UnicodeError:
                _issue(
                    issues,
                    name,
                    "source-undecodable",
                    "source must be UTF-8 text without NUL bytes",
                )
                continue
            if suffix == ".css":
                try:
                    result = enforce_css(system, source, source=name)
                except FormatError as error:
                    _issue(issues, name, "css-unsupported", str(error))
                    continue
                files.append(
                    {
                        "path": name,
                        "sha256": digest,
                        "coverage": result.coverage,
                        "violations": [item.to_dict() for item in result.violations],
                    }
                )
                for violation in result.violations:
                    _issue(issues, name, violation.rule_id, violation.message)
            elif suffix in {".html", ".htm"}:
                html.append((name, source, digest))
            else:
                unverified.append(name)
                _issue(
                    issues,
                    name,
                    "adapter-required",
                    f"{suffix} source needs a styling adapter",
                )

    scan(root)
    inspected = {item["path"] for item in files}
    referenced_tokens = False
    html_sources: list[dict[str, Any]] = []
    for name, source, digest in html:
        parser = _HTMLCoverage(name, issues)
        parser.feed(source)
        parser.close()
        html_sources.append(
            {"path": name, "sha256": digest, "stylesheets": list(parser.stylesheets)}
        )
        for href in parser.stylesheets:
            parsed = urlsplit(href)
            if (
                not href
                or parsed.scheme
                or parsed.netloc
                or parsed.query
                or parsed.fragment
                or href.startswith("/")
                or "%" in href
            ):
                _issue(
                    issues,
                    name,
                    "stylesheet-unverified",
                    f"uninspected stylesheet reference: {href or '(empty)'}",
                )
                continue
            href_parts = href.split("/")
            if any(part in {"", ".."} for part in href_parts):
                _issue(
                    issues,
                    name,
                    "stylesheet-unverified",
                    f"unsafe stylesheet reference: {href}",
                )
                continue
            target_name = (
                Path(name).parent / Path(*[part for part in href_parts if part != "."])
            ).as_posix()
            if target_name == manifest["tokens"]:
                referenced_tokens = True
            if target_name not in inspected and target_name != manifest["tokens"]:
                _issue(
                    issues,
                    name,
                    "stylesheet-unverified",
                    f"stylesheet was not inspected: {href}",
                )
    if html and not referenced_tokens:
        _issue(
            issues,
            manifest["tokens"],
            "tokens-unreferenced",
            "no static HTML file references the generated token stylesheet",
        )
    coverage = {
        "css_files": len(files),
        "css_declarations": sum(item["coverage"]["declarations"] for item in files),
        "governed_declarations": sum(item["coverage"]["governed"] for item in files),
        "html_files": len(html),
        "html_sources": html_sources,
        "unverified_files": unverified,
        "exclusions": [
            {
                "path": "/".join(parts),
                "reason": reason,
                "present": exists,
                "matched": parts in excluded_seen,
            }
            for parts, reason, exists in exclusions
        ],
        "built_in_exclusions": [
            {"path": path, "reason": "Git metadata"} for path in built_in
        ],
        "scope": "authored CSS and static HTML inventory; not computed browser styles or component semantics",
    }
    if not files or not coverage["governed_declarations"]:
        _issue(
            issues,
            manifest["root"],
            "css-empty-coverage",
            "no governed CSS declarations were inspected",
        )
    return {
        "format": "adsf-project-report",
        "version": "0.1.0",
        "status": "fail" if issues else "pass",
        "contract_sha256": fingerprint,
        "expected_sha256": expected_sha256,
        "tokens_sha256": hashlib.sha256(token_bytes).hexdigest(),
        "files": files,
        "coverage": coverage,
        "issues": issues,
    }
