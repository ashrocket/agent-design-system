from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .model import Token
from .selectors import SelectorError, parse_selector


TOKEN_REFERENCE = re.compile(r"^\{([a-zA-Z0-9_.-]+)\}$")
ALLOWED_ASSERTIONS = {
    "token-reference",
    "enum",
    "equals",
    "type",
    "pattern",
    "property-exists",
}
ALLOWED_CONDITIONS = {"always", "value-type", "matches", "coercible"}
ALLOWED_ACTIONS = {"report", "coerce", "reject", "ignore"}
ALLOWED_SEVERITIES = {"info", "warning", "error"}
ALLOWED_STRATEGIES = {"nearest-token", "replace"}
ALLOWED_TOKEN_TYPES = {
    "color",
    "dimension",
    "number",
    "fontFamily",
    "fontWeight",
    "duration",
    "cubicBezier",
    "string",
    "boolean",
}
ALLOWED_VALUE_TYPES = {
    "string",
    "number",
    "integer",
    "boolean",
    "object",
    "array",
    "null",
}


class FormatError(ValueError):
    pass


def load_json(path: str | Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as error:
        raise FormatError(f"cannot read {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise FormatError(f"invalid JSON in {path}: {error}") from error


def collect_tokens(tree: dict[str, Any]) -> dict[str, Token]:
    tokens: dict[str, Token] = {}

    def visit(node: Any, path: tuple[str, ...]) -> None:
        if isinstance(node, dict) and "$value" in node:
            token_path = ".".join(path)
            tokens[token_path] = Token(
                path=token_path,
                value=node["$value"],
                token_type=str(node.get("$type", "unknown")),
                description=str(node.get("$description", "")),
            )
            return
        if isinstance(node, dict):
            for key, child in node.items():
                if not key.startswith("$"):
                    visit(child, path + (key,))

    visit(tree, ())
    return tokens


def resolve_token_value(
    path: str, tokens: dict[str, Token], stack: tuple[str, ...] = ()
) -> Any:
    if path not in tokens:
        raise FormatError(f"unknown token reference: {path}")
    if path in stack:
        cycle = " -> ".join(stack + (path,))
        raise FormatError(f"token reference cycle: {cycle}")
    value = tokens[path].value
    if isinstance(value, str):
        match = TOKEN_REFERENCE.fullmatch(value)
        if match:
            return resolve_token_value(match.group(1), tokens, stack + (path,))
    return value


def validate_design_system(system: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(system, dict):
        return ["document root must be an object"]
    _validate_known_properties(
        system,
        {
            "$schema",
            "format",
            "version",
            "name",
            "description",
            "tokens",
            "rules",
            "metadata",
            "extensions",
        },
        "document root",
        errors,
    )
    for required in ("format", "version", "name", "tokens", "rules"):
        if required not in system:
            errors.append(f"missing required property: {required}")
    if system.get("format") != "adsf":
        errors.append("format must equal 'adsf'")
    if "$schema" in system and not isinstance(system["$schema"], str):
        errors.append("$schema must be a string")
    if (
        not isinstance(system.get("version"), str)
        or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", system.get("version", "")) is None
    ):
        errors.append("version must be a semantic version such as 0.1.0")
    for field in ("name", "description"):
        if not isinstance(system.get(field), str) or not system[field].strip():
            errors.append(f"{field} must be a non-empty string")
    if not isinstance(system.get("tokens"), dict):
        errors.append("tokens must be an object")
    else:
        _validate_token_node(system["tokens"], "tokens", errors, root=True)
    if not isinstance(system.get("rules"), list):
        errors.append("rules must be an array")
        return errors
    for field in ("metadata", "extensions"):
        if field in system and not isinstance(system[field], dict):
            errors.append(f"{field} must be an object")

    tokens = collect_tokens(system.get("tokens", {}))
    for token_path, token in tokens.items():
        try:
            resolve_token_value(token_path, tokens)
        except FormatError as error:
            errors.append(str(error))

    seen_ids: set[str] = set()
    for index, rule in enumerate(system.get("rules", [])):
        prefix = f"rules[{index}]"
        if not isinstance(rule, dict):
            errors.append(f"{prefix} must be an object")
            continue
        _validate_known_properties(
            rule,
            {
                "id",
                "description",
                "rationale",
                "selector",
                "assert",
                "severity",
                "decision",
            },
            prefix,
            errors,
        )
        for required in (
            "id",
            "description",
            "selector",
            "assert",
            "severity",
            "decision",
        ):
            if required not in rule:
                errors.append(f"{prefix} missing required property: {required}")
        rule_id = rule.get("id")
        if isinstance(rule_id, str):
            if re.fullmatch(r"[a-z][a-z0-9-]*", rule_id) is None:
                errors.append(
                    f"{prefix}.id must use lowercase letters, digits, and hyphens"
                )
            if rule_id in seen_ids:
                errors.append(f"duplicate rule id: {rule_id}")
            seen_ids.add(rule_id)
        else:
            errors.append(f"{prefix}.id must be a string")
        for field in ("description", "rationale"):
            if not isinstance(rule.get(field), str) or not rule.get(field, "").strip():
                errors.append(f"{prefix}.{field} must be a non-empty string")
        try:
            if "selector" in rule:
                parse_selector(rule["selector"])
        except SelectorError as error:
            errors.append(f"{prefix}: {error}")
        assertion = rule.get("assert", {})
        if (
            not isinstance(assertion, dict)
            or assertion.get("operator") not in ALLOWED_ASSERTIONS
        ):
            errors.append(
                f"{prefix}.assert.operator must be one of {sorted(ALLOWED_ASSERTIONS)}"
            )
        else:
            _validate_assertion(assertion, f"{prefix}.assert", errors)
        if rule.get("severity") not in ALLOWED_SEVERITIES:
            errors.append(
                f"{prefix}.severity must be one of {sorted(ALLOWED_SEVERITIES)}"
            )
        decision = rule.get("decision")
        if not isinstance(decision, list) or not decision:
            errors.append(f"{prefix}.decision must be a non-empty array")
            continue
        for branch_index, branch in enumerate(decision):
            branch_prefix = f"{prefix}.decision[{branch_index}]"
            if not isinstance(branch, dict):
                errors.append(f"{branch_prefix} must be an object")
                continue
            _validate_known_properties(
                branch, {"when", "action"}, branch_prefix, errors
            )
            condition = branch.get("when", {})
            action = branch.get("action", {})
            if (
                not isinstance(condition, dict)
                or condition.get("operator") not in ALLOWED_CONDITIONS
            ):
                errors.append(
                    f"{branch_prefix}.when.operator must be one of {sorted(ALLOWED_CONDITIONS)}"
                )
            else:
                _validate_condition(condition, f"{branch_prefix}.when", errors)
            if (
                not isinstance(action, dict)
                or action.get("type") not in ALLOWED_ACTIONS
            ):
                errors.append(
                    f"{branch_prefix}.action.type must be one of {sorted(ALLOWED_ACTIONS)}"
                )
            else:
                _validate_action(action, f"{branch_prefix}.action", errors)
                if rule.get("selector") == "$" and action.get("type") == "coerce":
                    errors.append(
                        f"{branch_prefix}.action cannot coerce the document root"
                    )
        if decision and isinstance(decision[-1], dict):
            final_when = decision[-1].get("when", {})
            if final_when.get("operator") != "always":
                errors.append(f"{prefix}.decision must end with an always fallback")
    return errors


def _validate_token_node(
    node: Any, path: str, errors: list[str], *, root: bool = False
) -> None:
    if not isinstance(node, dict):
        errors.append(f"{path} must be an object")
        return
    if "$value" in node:
        if root:
            errors.append("tokens root must be a group, not a token")
        missing = {"$type", "$value", "$description"} - set(node)
        for field in sorted(missing):
            errors.append(f"{path} missing required property: {field}")
        unknown = set(node) - {"$type", "$value", "$description", "$extensions"}
        for field in sorted(unknown):
            errors.append(f"{path} has unknown property: {field}")
        if node.get("$type") not in ALLOWED_TOKEN_TYPES:
            errors.append(f"{path}.$type must be one of {sorted(ALLOWED_TOKEN_TYPES)}")
        if (
            not isinstance(node.get("$description"), str)
            or not node.get("$description", "").strip()
        ):
            errors.append(f"{path}.$description must be a non-empty string")
        if "$extensions" in node and not isinstance(node["$extensions"], dict):
            errors.append(f"{path}.$extensions must be an object")
        return
    if not node:
        errors.append(f"{path} must not be empty")
    for key, child in node.items():
        if key.startswith("$"):
            errors.append(f"{path} contains reserved group property: {key}")
            continue
        _validate_token_node(child, f"{path}.{key}", errors)


def _validate_assertion(
    assertion: dict[str, Any], path: str, errors: list[str]
) -> None:
    _validate_known_properties(
        assertion, {"operator", "category", "values", "value"}, path, errors
    )
    operator = assertion["operator"]
    if "category" in assertion and not isinstance(assertion["category"], str):
        errors.append(f"{path}.category must be a string")
    if operator == "enum" and not isinstance(assertion.get("values"), list):
        errors.append(f"{path}.values must be an array for enum")
    if (
        operator in {"equals", "type", "pattern", "property-exists"}
        and "value" not in assertion
    ):
        errors.append(f"{path}.value is required for {operator}")
    if operator == "type" and assertion.get("value") not in ALLOWED_VALUE_TYPES:
        errors.append(f"{path}.value must be one of {sorted(ALLOWED_VALUE_TYPES)}")
    if operator == "pattern":
        _validate_regex(assertion.get("value"), f"{path}.value", errors)
    if operator == "property-exists" and (
        not isinstance(assertion.get("value"), str)
        or not assertion.get("value", "").strip()
    ):
        errors.append(f"{path}.value must be a non-empty relative property path")


def _validate_condition(
    condition: dict[str, Any], path: str, errors: list[str]
) -> None:
    _validate_known_properties(
        condition, {"operator", "value", "strategy", "category"}, path, errors
    )
    operator = condition["operator"]
    if "strategy" in condition and condition["strategy"] not in ALLOWED_STRATEGIES:
        errors.append(f"{path}.strategy must be one of {sorted(ALLOWED_STRATEGIES)}")
    if "category" in condition and not isinstance(condition["category"], str):
        errors.append(f"{path}.category must be a string")
    if operator == "value-type" and condition.get("value") not in ALLOWED_VALUE_TYPES:
        errors.append(f"{path}.value must be one of {sorted(ALLOWED_VALUE_TYPES)}")
    if operator == "matches":
        _validate_regex(condition.get("value"), f"{path}.value", errors)
    if operator == "coercible":
        if "strategy" not in condition:
            errors.append(f"{path}.strategy is required for coercible")
        if condition.get("strategy") == "nearest-token" and not isinstance(
            condition.get("category"), str
        ):
            errors.append(f"{path}.category is required for nearest-token")
        if condition.get("strategy") == "replace" and "value" not in condition:
            errors.append(f"{path}.value is required for replace")


def _validate_action(action: dict[str, Any], path: str, errors: list[str]) -> None:
    _validate_known_properties(
        action,
        {"type", "strategy", "category", "value", "message", "rationale"},
        path,
        errors,
    )
    if "strategy" in action and action["strategy"] not in ALLOWED_STRATEGIES:
        errors.append(f"{path}.strategy must be one of {sorted(ALLOWED_STRATEGIES)}")
    if "category" in action and not isinstance(action["category"], str):
        errors.append(f"{path}.category must be a string")
    for field in ("message", "rationale"):
        if field in action and not isinstance(action[field], str):
            errors.append(f"{path}.{field} must be a string")
    if action["type"] != "coerce":
        return
    if "strategy" not in action:
        errors.append(f"{path}.strategy is required for coerce")
    if action.get("strategy") == "nearest-token" and not isinstance(
        action.get("category"), str
    ):
        errors.append(f"{path}.category is required for nearest-token")
    if action.get("strategy") == "replace" and "value" not in action:
        errors.append(f"{path}.value is required for replace")


def _validate_regex(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, str):
        errors.append(f"{path} must be a regular-expression string")
        return
    try:
        re.compile(value)
    except re.error as error:
        errors.append(f"{path} is not a valid regular expression: {error}")


def _validate_known_properties(
    value: dict[str, Any], allowed: set[str], path: str, errors: list[str]
) -> None:
    for field in sorted(set(value) - allowed):
        errors.append(f"{path} has unknown property: {field}")
