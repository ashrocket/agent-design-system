from __future__ import annotations

import copy
import math
import re
from typing import Any

from .format import (
    TOKEN_REFERENCE,
    FormatError,
    collect_tokens,
    resolve_token_value,
    validate_design_system,
)
from .model import EnforcementResult, Match, Token, Violation
from .selectors import assign, select


def enforce(
    system: dict[str, Any],
    document: Any,
    *,
    source: str = "<memory>",
    apply_fixes: bool = False,
) -> EnforcementResult:
    errors = validate_design_system(system)
    if errors:
        raise FormatError("invalid design system:\n- " + "\n- ".join(errors))

    tokens = collect_tokens(system["tokens"])
    output = copy.deepcopy(document)
    violations: list[Violation] = []
    pending_fixes: list[tuple[tuple[str | int, ...], Any]] = []

    for rule in system["rules"]:
        for match in select(document, rule["selector"]):
            if _assertion_passes(rule["assert"], match.value, tokens):
                continue
            violation = _decide(rule, match, tokens)
            violations.append(violation)
            if (
                apply_fixes
                and violation.disposition == "coerce"
                and violation.replacement is not None
            ):
                pending_fixes.append((match.segments, violation.replacement))

    for segments, replacement in pending_fixes:
        assign(output, segments, replacement)

    return EnforcementResult(
        source=source,
        violations=violations,
        document=output,
        changed=bool(pending_fixes),
    )


def _assertion_passes(
    assertion: dict[str, Any], value: Any, tokens: dict[str, Token]
) -> bool:
    operator = assertion["operator"]
    if operator == "token-reference":
        if not isinstance(value, str):
            return False
        match = TOKEN_REFERENCE.fullmatch(value)
        if not match or match.group(1) not in tokens:
            return False
        category = assertion.get("category")
        return category is None or match.group(1).startswith(f"{category}.")
    if operator == "enum":
        return any(
            _json_equal(value, candidate) for candidate in assertion.get("values", [])
        )
    if operator == "equals":
        return _json_equal(value, assertion.get("value"))
    if operator == "type":
        return _is_type(value, assertion.get("value"))
    if operator == "pattern":
        return (
            isinstance(value, str)
            and re.search(assertion.get("value", ""), value) is not None
        )
    if operator == "property-exists":
        return _property_exists(value, assertion.get("value", ""))
    return False


def _decide(rule: dict[str, Any], match: Match, tokens: dict[str, Token]) -> Violation:
    for branch in rule["decision"]:
        if not _condition_matches(branch["when"], match.value, tokens):
            continue
        action = branch["action"]
        replacement = (
            _replacement(action, match.value, tokens)
            if action["type"] == "coerce"
            else None
        )
        disposition = action["type"]
        if disposition == "coerce" and replacement is None:
            disposition = "report"
        message = action.get("message") or f"{rule['description']} at {match.path}"
        return Violation(
            rule_id=rule["id"],
            path=match.path,
            severity=rule["severity"],
            message=message,
            original=match.value,
            disposition=disposition,
            replacement=replacement,
            rationale=action.get("rationale", rule.get("rationale", "")),
        )
    raise FormatError(f"rule {rule['id']} has no matching decision branch")


def _condition_matches(
    condition: dict[str, Any], value: Any, tokens: dict[str, Token]
) -> bool:
    operator = condition["operator"]
    if operator == "always":
        return True
    if operator == "value-type":
        return _is_type(value, condition.get("value"))
    if operator == "matches":
        return (
            isinstance(value, str)
            and re.search(condition.get("value", ""), value) is not None
        )
    if operator == "coercible":
        strategy = condition.get("strategy")
        if strategy == "replace":
            return "value" in condition
        if strategy == "nearest-token":
            return (
                _nearest_token_reference(value, tokens, condition.get("category"))
                is not None
            )
    return False


def _replacement(action: dict[str, Any], value: Any, tokens: dict[str, Token]) -> Any:
    strategy = action.get("strategy")
    if strategy == "replace":
        return action.get("value")
    if strategy == "nearest-token":
        return _nearest_token_reference(value, tokens, action.get("category"))
    return None


def _nearest_token_reference(
    value: Any, tokens: dict[str, Token], category: str | None
) -> str | None:
    candidates: list[tuple[float, str]] = []
    for token_path in sorted(tokens):
        if category and not token_path.startswith(f"{category}."):
            continue
        try:
            token_value = resolve_token_value(token_path, tokens)
        except FormatError:
            continue
        distance = _distance(value, token_value)
        if distance is not None:
            candidates.append((distance, token_path))
    if not candidates:
        return None
    _, best_path = min(candidates, key=lambda candidate: (candidate[0], candidate[1]))
    return "{" + best_path + "}"


def _distance(left: Any, right: Any) -> float | None:
    if isinstance(left, bool) or isinstance(right, bool):
        return None
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right))
    left_color = _parse_color(left)
    right_color = _parse_color(right)
    if left_color is not None and right_color is not None:
        return math.sqrt(
            sum((a - b) ** 2 for a, b in zip(left_color, right_color, strict=True))
        )
    if isinstance(left, str) and isinstance(right, str):
        return 0.0 if left == right else None
    return None


def _parse_color(value: Any) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})", value)
    if not match:
        return None
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(character * 2 for character in digits)
    return tuple(int(digits[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]


def _is_type(value: Any, expected: str | None) -> bool:
    return {
        "string": isinstance(value, str),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "null": value is None,
    }.get(expected or "", False)


def _property_exists(value: Any, relative_path: str) -> bool:
    current = value
    for segment in relative_path.split("."):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit():
            index = int(segment)
            if not 0 <= index < len(current):
                return False
            current = current[index]
        else:
            return False
    return True


def _json_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) or isinstance(right, (int, float)):
        return (
            isinstance(left, (int, float))
            and not isinstance(left, bool)
            and isinstance(right, (int, float))
            and not isinstance(right, bool)
            and float(left) == float(right)
        )
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    if isinstance(left, dict):
        return set(left) == set(right) and all(
            _json_equal(left[key], right[key]) for key in left
        )
    return left == right
