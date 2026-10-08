"""Read-only project-wide CSS discovery and coverage accounting."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from .css import enforce_css, render_tokens
from .format import FormatError, load_json
from .tsx import MAX_TSX_BYTES, PROFILE, AdapterDriftError, inspect_static_tsx

MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_SCAN_DEPTH = 64
MAX_SCAN_ENTRIES = 20_000
MAX_UNVERIFIED_HASH_BYTES = 64 * 1024 * 1024
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


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_unverified(path: Path, size: int) -> str | None:
    if size > MAX_UNVERIFIED_HASH_BYTES:
        return None
    digest = hashlib.sha256()
    remaining = MAX_UNVERIFIED_HASH_BYTES
    with path.open("rb") as handle:
        while chunk := handle.read(min(1024 * 1024, remaining + 1)):
            if len(chunk) > remaining:
                return None
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def _tsx_import_path(source: str, specifier: str) -> str:
    if (
        not specifier.startswith(("./", "../"))
        or not specifier.endswith(".css")
        or any(character in specifier for character in "\\%?#:")
    ):
        raise FormatError(f"unsafe TSX CSS import: {specifier}")
    parts = list(PurePosixPath(source).parent.parts)
    for part in specifier.split("/"):
        if part == ".":
            continue
        if part == "..":
            if not parts:
                raise FormatError(f"TSX CSS import escapes project root: {specifier}")
            parts.pop()
        elif not part:
            raise FormatError(f"noncanonical TSX CSS import: {specifier}")
        else:
            parts.append(part)
    return "/".join(parts)


class _HTMLCoverage(HTMLParser):
    def __init__(self, source: str, issues: list[dict[str, str]]) -> None:
        super().__init__(convert_charrefs=True)
        self.source = source
        self.issues = issues
        self.stylesheets: list[str] = []
        self.usable_stylesheets: list[str] = []
        self.classes: list[tuple[str, str, int]] = []

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
        for token in re.split(r"[ \t\r\n\f]+", attributes.get("class") or ""):
            if token:
                self.classes.append((token, tag, self.getpos()[0]))
        if tag in {
            "base",
            "style",
            "script",
            "svg",
            "iframe",
            "object",
            "embed",
            "font",
            "template",
            "noscript",
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
        if tag == "link":
            rel = (attributes.get("rel") or "").lower().split()
            if "stylesheet" in rel:
                href = attributes.get("href") or ""
                self.stylesheets.append(href)
                if rel != ["stylesheet"] or any(
                    name in attributes
                    for name in ("media", "disabled", "title", "type")
                ):
                    _issue(
                        self.issues,
                        self.source,
                        "stylesheet-conditional",
                        "alternate, conditional, disabled, titled, or typed stylesheet link cannot prove loading",
                    )
                else:
                    self.usable_stylesheets.append(href)

    handle_startendtag = handle_starttag


def check_project(
    manifest_path: str | Path, *, expected_sha256: str | None = None
) -> dict[str, Any]:
    """Discover supported source; report drift and unverified surfaces without writes."""
    manifest_file = Path(manifest_path).absolute()
    if manifest_file.is_symlink() or not manifest_file.is_file():
        raise FormatError("project manifest must be a regular, direct file")
    manifest = load_json(manifest_file)
    required = {
        "format",
        "version",
        "root",
        "system",
        "tokens",
        "exclusions",
    }
    if (
        not isinstance(manifest, dict)
        or not required.issubset(manifest)
        or set(manifest) - required - {"tsx", "app"}
    ):
        raise FormatError(
            "project manifest requires format, version, root, system, tokens, exclusions, and optional tsx/app only"
        )
    if manifest["format"] != "adsf-css-project" or manifest["version"] != "0.1.0":
        raise FormatError("unsupported CSS project manifest format or version")
    tsx_enabled = "tsx" in manifest
    if tsx_enabled:
        tsx_policy = manifest["tsx"]
        if (
            not isinstance(tsx_policy, dict)
            or set(tsx_policy) != {"profile", "adapter_sha256", "lock_sha256"}
            or tsx_policy["profile"] != PROFILE
            or not all(
                isinstance(tsx_policy[key], str)
                and re.fullmatch(r"[0-9a-f]{64}", tsx_policy[key])
                for key in ("adapter_sha256", "lock_sha256")
            )
        ):
            raise FormatError(
                "tsx requires the static-jsx-v1 profile and reviewed adapter and lock SHA-256 pins"
            )
    app_enabled = "app" in manifest
    if app_enabled:
        if expected_sha256 is None:
            raise FormatError(
                "app profile requires an external expected contract SHA-256"
            )
        app_policy = manifest["app"]
        if (
            not tsx_enabled
            or not isinstance(app_policy, dict)
            or set(app_policy)
            != {"profile", "host", "host_sha256", "entry", "entry_sha256"}
            or app_policy["profile"] != "pinned-react-entry-v1"
            or not all(
                isinstance(app_policy[key], str)
                and re.fullmatch(r"[0-9a-f]{64}", app_policy[key])
                for key in ("host_sha256", "entry_sha256")
            )
        ):
            raise FormatError(
                "app requires pinned-react-entry-v1, tsx, and reviewed host/entry SHA-256 pins"
            )
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
    app_sources: dict[str, tuple[str, str]] = {}
    if app_enabled:
        host_parts = _relative(manifest["app"]["host"], "app host")
        entry_parts = _relative(manifest["app"]["entry"], "app entry")
        if (
            not host_parts[-1].lower().endswith((".html", ".htm"))
            or not entry_parts[-1].lower().endswith(".tsx")
            or _overlap(host_parts, entry_parts)
            or _overlap(host_parts, token_parts)
            or _overlap(entry_parts, token_parts)
        ):
            raise FormatError(
                "app host and entry must be distinct HTML/TSX files outside the token path"
            )
        for role, parts, pin in (
            ("host", host_parts, manifest["app"]["host_sha256"]),
            ("entry", entry_parts, manifest["app"]["entry_sha256"]),
        ):
            target, exists = _declared(root, parts, f"app {role}", missing=True)
            if exists and not target.is_file():
                raise FormatError(f"app {role} must be a regular file")
            if target in {manifest_file, system_file}:
                raise FormatError(f"app {role} cannot be the manifest or system")
            app_sources["/".join(parts)] = (role, pin)
    raw_exclusions = manifest["exclusions"]
    if not isinstance(raw_exclusions, list):
        raise FormatError("exclusions must be a list")
    exclusions: list[tuple[tuple[str, ...], str, bool]] = []
    reserved: list[tuple[str, ...]] = [token_parts]
    if app_enabled:
        reserved.extend([host_parts, entry_parts])
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
    for app_path, (role, _) in app_sources.items():
        if not (root / app_path).is_file():
            _issue(
                issues,
                app_path,
                f"app-{role}-missing",
                f"declared app {role} is missing",
            )

    files: list[dict[str, Any]] = []
    html: list[tuple[str, str, str]] = []
    tsx_sources: list[tuple[str, str, str]] = []
    unverified: list[str] = []
    unverified_sources: list[dict[str, Any]] = []
    trusted_sources: list[dict[str, str]] = []
    app_coverage: list[dict[str, str]] = []
    built_in: list[str] = []
    excluded_seen: set[tuple[str, ...]] = set()
    scan_entries = 0

    def scan(directory: Path, depth: int = 0) -> None:
        nonlocal scan_entries
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            scan_entries += 1
            if scan_entries > MAX_SCAN_ENTRIES:
                raise FormatError(f"project scan exceeds {MAX_SCAN_ENTRIES} entries")
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
                if depth >= MAX_SCAN_DEPTH:
                    raise FormatError(
                        f"project scan depth exceeds {MAX_SCAN_DEPTH}: {name}"
                    )
                scan(path, depth + 1)
                continue
            if not stat.S_ISREG(mode):
                raise FormatError(f"special file in project root: {name}")
            if path in {token_file, manifest_file, system_file}:
                role = (
                    "tokens"
                    if path == token_file
                    else "manifest"
                    if path == manifest_file
                    else "system"
                )
                trusted_sources.append(
                    {"path": name, "role": role, "sha256": _hash_file(path)}
                )
                continue
            suffix = path.suffix.lower()
            if suffix not in {".css", ".html", ".htm"} and not (
                suffix == ".tsx" and tsx_enabled
            ):
                size = entry.stat(follow_symlinks=False).st_size
                digest = _hash_unverified(path, size)
                unverified.append(name)
                unverified_sources.append(
                    {
                        "path": name,
                        "suffix": suffix,
                        "bytes": size,
                        "sha256": digest,
                        "hash_status": "sha256" if digest else "size-limit",
                    }
                )
                _issue(
                    issues,
                    name,
                    "adapter-required",
                    f"{suffix or 'extensionless'} source or asset needs an adapter or reviewed exclusion",
                )
                continue
            source_limit = MAX_TSX_BYTES if suffix == ".tsx" else MAX_SOURCE_BYTES
            with path.open("rb") as handle:
                content = handle.read(source_limit + 1)
            if len(content) > source_limit:
                _issue(
                    issues,
                    name,
                    "source-too-large",
                    f"source exceeds {source_limit} byte inspection limit",
                )
                continue
            digest = hashlib.sha256(content).hexdigest()
            if name in app_sources:
                role, expected = app_sources[name]
                app_coverage.append({"path": name, "role": role, "sha256": digest})
                if digest != expected:
                    _issue(
                        issues,
                        name,
                        f"app-{role}-drift",
                        f"app {role} differs from its reviewed SHA-256 pin",
                    )
                continue
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
            if suffix == ".tsx":
                tsx_sources.append((name, source, digest))
            elif suffix == ".css":
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
            else:
                html.append((name, source, digest))

    scan(root)
    inspected = {item["path"] for item in files}
    classes_by_path = {
        item["path"]: set(item["coverage"]["selector_classes"]) for item in files
    }
    tsx_coverage: list[dict[str, Any]] = []
    for name, source, digest in tsx_sources:
        try:
            tsx_report = inspect_static_tsx(
                name,
                source,
                digest,
                expected_adapter_sha256=manifest["tsx"]["adapter_sha256"],
                expected_lock_sha256=manifest["tsx"]["lock_sha256"],
            )
        except AdapterDriftError as error:
            _issue(issues, name, "tsx-adapter-drift", str(error))
            tsx_coverage.append({"path": name, "sha256": digest, "adapter": "drifted"})
            continue
        except FormatError as error:
            _issue(issues, name, "tsx-adapter-error", str(error))
            tsx_coverage.append(
                {"path": name, "sha256": digest, "adapter": "unavailable"}
            )
            continue
        imported: list[str] = []
        tsx_linked_classes: set[str] = set()
        token_imported = False
        for specifier in tsx_report["imports"]:
            try:
                target_name = _tsx_import_path(name, specifier)
            except FormatError as error:
                _issue(issues, name, "tsx-import-unverified", str(error))
                continue
            imported.append(target_name)
            if target_name == manifest["tokens"]:
                token_imported = True
            elif target_name not in inspected:
                _issue(
                    issues,
                    name,
                    "tsx-import-unverified",
                    f"CSS import {specifier} was not inspected",
                )
            tsx_linked_classes.update(classes_by_path.get(target_name, set()))
        for item in tsx_report["issues"]:
            _issue(
                issues,
                name,
                item["code"],
                f"line {item['line']}:{item['column']}: {item['message']}",
            )
        for item in tsx_report["classes"]:
            if item["name"] not in tsx_linked_classes:
                _issue(
                    issues,
                    name,
                    "tsx-class-unverified",
                    f"class {item['name']!r} at line {item['line']}:{item['column']} has no simple base rule in imported, inspected CSS",
                )
        if not token_imported:
            _issue(
                issues,
                name,
                "tsx-tokens-unimported",
                "static TSX source does not import the generated token stylesheet",
            )
        tsx_coverage.append(
            {
                "path": name,
                "sha256": digest,
                "profile": PROFILE,
                "parser_version": tsx_report["parser_version"],
                "adapter_sha256": tsx_report["adapter_sha256"],
                "lock_sha256": tsx_report["lock_sha256"],
                "css_imports": imported,
                "classes": tsx_report["classes"],
            }
        )
    if tsx_enabled and not tsx_sources:
        _issue(
            issues,
            manifest_file.name,
            "tsx-empty-coverage",
            "the enabled static TSX profile found no TSX source",
        )
    html_sources: list[dict[str, Any]] = []
    for name, source, digest in html:
        parser = _HTMLCoverage(name, issues)
        parser.feed(source)
        parser.close()
        referenced_tokens = False
        linked_classes: set[str] = set()
        html_sources.append(
            {
                "path": name,
                "sha256": digest,
                "stylesheets": list(parser.stylesheets),
                "classes": sorted({token for token, _, _ in parser.classes}),
            }
        )
        for href in parser.usable_stylesheets:
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
            linked_classes.update(classes_by_path.get(target_name, set()))
        for token, tag, line in parser.classes:
            if token not in linked_classes:
                _issue(
                    issues,
                    name,
                    "html-class-unverified",
                    f"class {token!r} on <{tag}> at line {line} has no simple base rule in linked, inspected CSS",
                )
        if not referenced_tokens:
            _issue(
                issues,
                name,
                "tokens-unreferenced",
                "static HTML page does not reference the generated token stylesheet",
            )
    coverage = {
        "css_files": len(files),
        "css_declarations": sum(item["coverage"]["declarations"] for item in files),
        "governed_declarations": sum(item["coverage"]["governed"] for item in files),
        "html_files": len(html),
        "tsx_files": len(tsx_sources),
        "tsx_sources": tsx_coverage,
        "html_sources": html_sources,
        "unverified_files": unverified,
        "unverified_sources": unverified_sources,
        "trusted_sources": sorted(trusted_sources, key=lambda item: item["path"]),
        "app_sources": sorted(app_coverage, key=lambda item: item["path"]),
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
        "scope": "authored CSS, static HTML, opt-in static TSX, and exact pinned app host/entry bytes; not app runtime, computed browser styles, or component semantics",
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
