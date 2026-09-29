"""Bounded, fail-closed enforcement of authored CSS, without a browser or model."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shlex
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .format import (
    FormatError,
    collect_tokens,
    resolve_token_value,
    validate_design_system,
)
from .model import EnforcementResult, Violation

PROPERTY = re.compile(r"[a-z][a-z0-9-]*")
VARIABLE = re.compile(r"--[a-zA-Z_][a-zA-Z0-9_-]*")
NUMBER = re.compile(
    r"([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)([a-zA-Z%]*)"
)
REFERENCE = re.compile(r"var\([ \t\r\n\f]*(--[a-zA-Z_][a-zA-Z0-9_-]*)[ \t\r\n\f]*\)")
GROUPS = {"media", "supports", "layer", "container", "scope", "keyframes"}
CSS_SPACE = " \t\r\n\f"
LENGTH_UNITS = {"px", "em", "rem", "ex", "ch", "vw", "vh", "vmin", "vmax", "%"}


def _has_priority(value: str) -> bool:
    quote = ""
    for char in value:
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "!":
            return True
    return False


@dataclass(frozen=True)
class Binding:
    name: str
    value: str
    description: str


@dataclass(frozen=True)
class CSSPolicy:
    bindings: dict[str, Binding]
    properties: dict[str, dict[str, Any]]
    unrestricted: tuple[str, ...]
    sha256: str


@dataclass(frozen=True)
class Declaration:
    property: str
    value: str
    start: int
    end: int
    line: int
    column: int


def _literal(value: Any, token_type: str) -> str:
    if isinstance(value, bool):
        raise FormatError("boolean tokens cannot be bound to CSS")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise FormatError("CSS tokens require finite numbers")
        rendered = str(value)
        if token_type == "dimension":
            return rendered + "px"
        if token_type == "duration":
            return rendered + "ms"
        if token_type in {"number", "fontWeight"}:
            return rendered
    if (
        isinstance(value, str)
        and value.strip()
        and not re.search(r"[;{}\\\r\n\f]|/\*|var\(", value, re.IGNORECASE)
        and not _has_priority(value)
    ):
        if token_type == "color" and not re.fullmatch(
            r"#[0-9a-fA-F]{3,4}|#[0-9a-fA-F]{6}|#[0-9a-fA-F]{8}", value
        ):
            raise FormatError("CSS color bindings require a hex color")
        if token_type in {"dimension", "duration", "number", "fontWeight"}:
            numeric = NUMBER.fullmatch(value)
            if not numeric:
                raise FormatError(
                    "numeric CSS token bindings require a numeric CSS value"
                )
            unit = numeric.group(2).lower()
            allowed_units = (
                LENGTH_UNITS
                if token_type == "dimension"
                else {"s", "ms"}
                if token_type == "duration"
                else {""}
            )
            if unit not in allowed_units:
                raise FormatError(
                    f"unsupported {token_type} unit: {unit or '(unitless)'}"
                )
        parse_css(f".token {{ value: {value}; }}")
        return value.strip()
    raise FormatError(f"unsupported CSS value for token type {token_type}")


def _normalized(value: str) -> str:
    value = value.strip(CSS_SPACE)
    if re.fullmatch(r"#[0-9a-fA-F]{3,4}|#[0-9a-fA-F]{6}|#[0-9a-fA-F]{8}", value):
        digits = value[1:].lower()
        if len(digits) in {3, 4}:
            digits = "".join(char * 2 for char in digits)
        return "#" + digits
    number = NUMBER.fullmatch(value)
    if number:
        return f"{Fraction(number.group(1))}:{number.group(2).lower()}"
    return value


def load_css_policy(system: dict[str, Any]) -> CSSPolicy:
    errors = validate_design_system(system, css=False)
    if errors:
        raise FormatError("invalid design system:\n- " + "\n- ".join(errors))
    policy = system.get("extensions", {}).get("css")
    if not isinstance(policy, dict) or set(policy) != {
        "version",
        "bindings",
        "properties",
        "unrestricted_properties",
    }:
        raise FormatError(
            "extensions.css must contain version, bindings, properties, unrestricted_properties"
        )
    if policy["version"] != "0.1.0":
        raise FormatError("unsupported extensions.css version (expected 0.1.0)")
    tokens = collect_tokens(system["tokens"])
    raw_bindings = policy["bindings"]
    if not isinstance(raw_bindings, dict) or not raw_bindings:
        raise FormatError(
            "CSS bindings must be a non-empty token-path to variable-name object"
        )
    bindings: dict[str, Binding] = {}
    names: set[str] = set()
    for path, name in sorted(raw_bindings.items()):
        if (
            path not in tokens
            or not isinstance(name, str)
            or not VARIABLE.fullmatch(name)
        ):
            raise FormatError(f"invalid CSS token binding: {path}")
        if name in names:
            raise FormatError(f"duplicate CSS variable binding: {name}")
        names.add(name)
        token = tokens[path]
        bindings[path] = Binding(
            name,
            _literal(resolve_token_value(path, tokens), token.token_type),
            token.description,
        )
    properties = policy["properties"]
    if not isinstance(properties, dict) or not properties:
        raise FormatError("CSS properties must be a non-empty object")
    for name, rule in properties.items():
        if (
            not PROPERTY.fullmatch(name)
            or name == "all"
            or not isinstance(rule, dict)
            or set(rule) - {"tokens", "literals", "max_values"}
            or "tokens" not in rule
        ):
            raise FormatError(f"invalid CSS property policy: {name}")
        allowed = rule["tokens"]
        if (
            not isinstance(allowed, list)
            or not allowed
            or any(
                not isinstance(path, str) or path not in bindings for path in allowed
            )
            or len(set(allowed)) != len(allowed)
        ):
            raise FormatError(f"CSS property {name} must list unique bound tokens")
        maximum = rule.get("max_values", 1)
        if (
            isinstance(maximum, bool)
            or not isinstance(maximum, (int, float))
            or (isinstance(maximum, float) and not maximum.is_integer())
            or not 1 <= maximum <= 4
        ):
            raise FormatError(
                f"CSS property {name} max_values must be an integer from 1 to 4"
            )
        literals = rule.get("literals", [])
        if not isinstance(literals, list) or any(
            not isinstance(value, str)
            or not value.strip()
            or re.search(r"[;{}\\\r\n\f]|/\*|var\(", value, re.IGNORECASE)
            or _has_priority(value)
            for value in literals
        ):
            raise FormatError(
                f"CSS property {name} literals must be safe non-empty CSS strings"
            )
        for literal in literals:
            parse_css(f".literal {{ value: {literal}; }}")
    unrestricted = policy["unrestricted_properties"]
    if (
        not isinstance(unrestricted, list)
        or any(
            not isinstance(name, str)
            or not PROPERTY.fullmatch(name)
            or name in properties
            or name == "all"
            for name in unrestricted
        )
        or len(set(unrestricted)) != len(unrestricted)
    ):
        raise FormatError(
            "CSS unrestricted_properties must be unique ordinary property names outside governed properties; 'all' is forbidden"
        )
    digest = hashlib.sha256(
        json.dumps(
            system,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()
    return CSSPolicy(bindings, properties, tuple(unrestricted), digest)


def _location(text: str, position: int) -> tuple[int, int]:
    return text.count("\n", 0, position) + 1, position - text.rfind("\n", 0, position)


def _error(text: str, position: int, message: str) -> FormatError:
    line, column = _location(text, position)
    return FormatError(f"CSS {line}:{column}: {message}")


def _mask_comments(text: str) -> str:
    chars = list(text)
    index = 0
    quote = ""
    while index < len(text):
        char = text[index]
        if quote:
            if char in "\r\n\f":
                raise _error(text, index, "unescaped newline in CSS string")
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                raise _error(text, index, "unterminated comment")
            for offset in range(index, end + 2):
                if chars[offset] not in "\r\n":
                    chars[offset] = " "
            index = end + 2
            continue
        index += 1
    if quote:
        raise _error(text, len(text), "unterminated string")
    return "".join(chars)


def parse_css(text: str) -> list[Declaration]:
    """Parse ordinary rules and grouping at-rules. Reject every unsupported form."""
    masked = _mask_comments(text)
    declarations: list[Declaration] = []

    def boundary(start: int, delimiters: str) -> int:
        stack: list[str] = []
        quote = ""
        index = start
        while index < len(masked):
            char = masked[index]
            if quote:
                if char == "\\":
                    raise _error(text, index, "CSS escapes are unsupported")
                if char == quote:
                    quote = ""
            elif char in "\"'":
                quote = char
            elif char == "\\":
                raise _error(text, index, "CSS escapes are unsupported")
            elif char in "([":
                stack.append(char)
            elif char in ")]":
                if not stack or stack.pop() != {")": "(", "]": "["}[char]:
                    raise _error(text, index, "unbalanced brackets or parentheses")
            elif not stack and char in delimiters:
                return index
            index += 1
        if quote or stack:
            raise _error(text, start, "unbalanced string, brackets or parentheses")
        return len(masked)

    def skip(position: int) -> int:
        while position < len(masked) and masked[position] in CSS_SPACE:
            position += 1
        return position

    def rules(position: int, *, nested: bool, depth: int = 0) -> int:
        if depth > 64:
            raise _error(text, position, "grouping nesting exceeds the limit of 64")
        while True:
            position = skip(position)
            if position == len(masked):
                if nested:
                    raise _error(text, position, "missing closing brace")
                return position
            if masked[position] == "}":
                if not nested:
                    raise _error(text, position, "unexpected closing brace")
                return position + 1
            end = boundary(position, "{};")
            if end == len(masked) or masked[end] != "{":
                raise _error(
                    text,
                    position,
                    "expected a rule block; imports and statement at-rules are unsupported",
                )
            prelude = masked[position:end].strip(CSS_SPACE)
            if not prelude:
                raise _error(text, position, "empty selector")
            if prelude.startswith("@"):
                match = re.match(r"@([a-zA-Z-]+)\b", prelude)
                if not match or match.group(1).lower() not in GROUPS:
                    raise _error(text, position, "unsupported at-rule")
                position = rules(end + 1, nested=True, depth=depth + 1)
            else:
                position = block(end + 1)

    def block(position: int) -> int:
        while True:
            position = skip(position)
            if position == len(masked):
                raise _error(text, position, "missing closing brace")
            if masked[position] == "}":
                return position + 1
            if masked[position] == ";":
                position += 1
                continue
            end = boundary(position, ";{}")
            if end == len(masked) or masked[end] == "{":
                raise _error(
                    text,
                    position,
                    "nested rules and malformed declarations are unsupported",
                )
            colon = boundary(position, ":;{}")
            if colon >= end or masked[colon] != ":":
                raise _error(
                    text, position, "declaration must contain a property and value"
                )
            name = masked[position:colon].strip(CSS_SPACE)
            if not re.fullmatch(
                r"(?:--[a-zA-Z_][a-zA-Z0-9_-]*|[a-zA-Z][a-zA-Z0-9-]*)", name
            ):
                raise _error(text, position, "unsupported property syntax")
            start = skip(colon + 1)
            value_end = end
            while value_end > start and masked[value_end - 1] in CSS_SPACE:
                value_end -= 1
            if start == value_end:
                raise _error(text, start, "empty declaration value")
            line, column = _location(text, start)
            declarations.append(
                Declaration(
                    name if name.startswith("--") else name.lower(),
                    masked[start:value_end],
                    start,
                    value_end,
                    line,
                    column,
                )
            )
            position = end + (masked[end] == ";")

    rules(0, nested=False)
    return declarations


def _value_parts(value: str) -> list[str] | None:
    parts: list[str] = []
    index = 0
    while index < len(value):
        while index < len(value) and value[index] in CSS_SPACE:
            index += 1
        if index == len(value):
            break
        reference = REFERENCE.match(value, index)
        if reference:
            end = reference.end()
        else:
            end = index
            while end < len(value) and value[end] not in CSS_SPACE:
                end += 1
        if end < len(value) and value[end] not in CSS_SPACE:
            return None
        parts.append(value[index:end])
        index = end
    return parts


def _check_value(policy: CSSPolicy, name: str, value: str) -> tuple[bool, str | None]:
    rule = policy.properties[name]
    literals = rule.get("literals", [])
    if any(_normalized(value) == _normalized(literal) for literal in literals):
        return True, None
    parts = _value_parts(value)
    if not parts or len(parts) > rule.get("max_values", 1):
        return False, None
    output: list[str] = []
    changed = False
    allowed = {policy.bindings[path].name: path for path in rule["tokens"]}
    for part in parts:
        reference = REFERENCE.fullmatch(part)
        if reference:
            if reference.group(1) not in allowed:
                return False, None
            output.append(part)
        elif any(_normalized(part) == _normalized(literal) for literal in literals):
            output.append(part)
        else:
            matches = [
                path
                for path in rule["tokens"]
                if _normalized(policy.bindings[path].value) == _normalized(part)
            ]
            if len(matches) != 1:
                return False, None
            output.append(f"var({policy.bindings[matches[0]].name})")
            changed = True
    return not changed, " ".join(output) if changed else None


def _preserve_value_source(original: str, masked: str, proposed: str) -> str:
    old_parts = _value_parts(masked) or []
    new_parts = _value_parts(proposed) or []
    edits: list[tuple[int, int, str]] = []
    position = 0
    for old, new in zip(old_parts, new_parts, strict=True):
        start = masked.index(old, position)
        position = start + len(old)
        if old != new:
            edits.append((start, position, new))
    output = original
    for start, end, value in reversed(edits):
        output = output[:start] + value + output[end:]
    return output


def enforce_css(
    system: dict[str, Any],
    text: str,
    *,
    source: str = "<memory>",
    apply_fixes: bool = False,
) -> EnforcementResult:
    policy = load_css_policy(system)
    declarations = parse_css(text)
    violations: list[Violation] = []
    fixes: list[tuple[int, int, str]] = []
    governed = 0
    unrestricted = 0
    for index, declaration in enumerate(declarations):
        name, value = declaration.property, declaration.value
        replacement: str | None = None
        message = ""
        if name.startswith("--"):
            message = "Component CSS cannot define custom properties; import the separately generated token stylesheet."
        elif _has_priority(value):
            message = "Priority overrides are outside this contract; remove !important."
        elif name in policy.properties:
            governed += 1
            passes, replacement = _check_value(policy, name, value)
            if passes:
                continue
            if replacement is not None:
                replacement = _preserve_value_source(
                    text[declaration.start : declaration.end], value, replacement
                )
            choices = ", ".join(
                f"var({policy.bindings[path].name})"
                for path in policy.properties[name]["tokens"]
            )
            message = f"{name} must use approved tokens: {choices}. Fallbacks and unapproved functions/literals are forbidden."
        elif name in policy.unrestricted:
            unrestricted += 1
            continue
        else:
            message = f"Property {name} is outside the approved CSS policy; choose a governed or explicitly unrestricted property."
        violation = Violation(
            rule_id=f"css-{name.lstrip('-')}",
            path=f"$.css[{index}].value",
            severity="error",
            message=message,
            original=value,
            disposition="coerce" if replacement is not None else "reject",
            replacement=replacement,
            rationale="Authored application styles must follow the approved design system.",
            line=declaration.line,
            column=declaration.column,
        )
        violations.append(violation)
        if apply_fixes and replacement is not None:
            fixes.append((declaration.start, declaration.end, replacement))
    if not declarations:
        violations.append(
            Violation(
                rule_id="css-empty-coverage",
                path="$.css",
                severity="error",
                message="No declarations were inspected; an empty stylesheet cannot establish compliance.",
                original=text,
                disposition="reject",
                line=1,
                column=1,
            )
        )
    output = text
    for start, end, replacement in reversed(fixes):
        output = output[:start] + replacement + output[end:]
    return EnforcementResult(
        source=source,
        violations=violations,
        document=output,
        changed=output != text,
        coverage={
            "adapter": "css",
            "declarations": len(declarations),
            "governed": governed,
            "unrestricted": unrestricted,
        },
        contract_sha256=policy.sha256,
    )


def render_tokens(system: dict[str, Any]) -> str:
    policy = load_css_policy(system)
    declarations = [
        f"  {binding.name}: {binding.value};" for binding in policy.bindings.values()
    ]
    return (
        f"/* ADSF contract SHA-256: {policy.sha256} */\n:root {{\n"
        + "\n".join(declarations)
        + "\n}\n"
    )


def agent_context(
    system: dict[str, Any], system_path: str, *, project_gate: str | None = None
) -> dict[str, Any]:
    policy = load_css_policy(system)
    name = sorted(policy.properties)[0]
    binding = policy.bindings[policy.properties[name]["tokens"][0]]
    unknown = "--adsf-unapproved"
    while unknown in {item.name for item in policy.bindings.values()}:
        unknown += "-x"
    examples = [
        {
            "source": f".example {{ {name}: var({binding.name}); }}",
            "expected": "pass",
            "reason": "Approved reference for this property.",
        },
        {
            "source": f".example {{ {name}: var({unknown}); }}",
            "expected": "reject",
            "reason": "Unknown references cannot satisfy the contract.",
        },
        {
            "source": f".example {{ {name}: var({binding.name}) !important; }}",
            "expected": "reject",
            "reason": "Priority overrides are forbidden even with an approved token.",
        },
    ]
    file_gate = f"adsys check {shlex.quote(system_path)} PATH.css --input-format css"
    limits = [
        "Standalone CSS files only; JSX, inline styles, Tailwind, CSS-in-JS and other files require additional adapters.",
        "No assurance of component reuse, layout quality, accessibility, browser-computed styles, or per-selector semantic intent.",
        "Grouping at-rules are supported; nested rules, imports, escapes and other at-rules are rejected.",
        "The generated token stylesheet must be installed and protected separately; component CSS cannot redefine variables.",
    ]
    if project_gate is None:
        limits.append(
            "The single-file gate does not prove whole-project coverage; use a reviewed project manifest for that claim."
        )
    else:
        limits[0] = (
            "The project gate discovers CSS and reports unsupported styling sources; those sources require adapters or reviewed exclusions."
        )
    return {
        "format": "adsf-agent-context",
        "version": "0.1.0",
        "system": system_path,
        "name": system["name"],
        "contract_sha256": policy.sha256,
        "gate": project_gate or file_gate,
        "fast_file_check": file_gate,
        "tokens": {
            path: {
                "variable": binding.name,
                "value": binding.value,
                "description": binding.description,
            }
            for path, binding in policy.bindings.items()
        },
        "properties": {
            name: policy.properties[name] for name in sorted(policy.properties)
        },
        "unrestricted_properties": sorted(policy.unrestricted),
        "examples": examples,
        "limits": limits,
    }


def render_context(
    system: dict[str, Any], system_path: str, *, project_gate: str | None = None
) -> str:
    context = agent_context(system, system_path, project_gate=project_gate)
    lines = [
        f"# Design contract: {system['name']}",
        "",
        f"Source: `{system_path}`",
        f"Contract SHA-256: `{context['contract_sha256']}`",
        "",
        "Read this before changing styles. Use the approved references below. Do not change the contract to silence a violation. Report conflicts with a requested design change.",
        "",
        "Run this gate after each edit and before handing off:",
        "",
        "```sh",
        context["gate"],
        "```",
        "",
        "For a quick single-file diagnostic, you can also run:",
        "",
        "```sh",
        context["fast_file_check"],
        "```",
        "",
        "## Approved tokens",
        "",
        "| Token | CSS reference | Current value | Purpose |",
        "| --- | --- | --- | --- |",
    ]
    for path, token in context["tokens"].items():
        purpose = token["description"].replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {path} | `var({token['variable']})` | `{token['value']}` | {purpose} |"
        )
    lines += ["", "## Property constraints", ""]
    for name, rule in context["properties"].items():
        choices = ", ".join(
            f"`var({context['tokens'][path]['variable']})`" for path in rule["tokens"]
        )
        lines.append(
            f"- `{name}`: {choices}; at most {rule.get('max_values', 1)} value(s); literal exceptions: {json.dumps(rule.get('literals', []))}."
        )
    lines += [
        "",
        "Explicitly unrestricted values: "
        + ", ".join(f"`{name}`" for name in context["unrestricted_properties"])
        + ". All other properties are rejected.",
        "",
        "Fallbacks, !important and component custom-property definitions are forbidden. Exact unambiguous literals can be repaired with `adsys fix SYSTEM PATH.css --input-format css --output FIXED.css`; the CLI rechecks the result. Approximate and ambiguous substitutions need a design decision.",
        "",
        "## Passing and failing examples",
        "",
    ]
    for example in context["examples"]:
        lines += [
            f"{example['expected'].upper()}: {example['reason']}",
            "",
            "```css",
            example["source"],
            "```",
            "",
        ]
    lines += [
        "## Coverage limits",
        "",
    ]
    lines += [f"- {limit}" for limit in context["limits"]]
    return "\n".join(lines) + "\n"
