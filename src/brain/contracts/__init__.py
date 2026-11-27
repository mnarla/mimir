"""Versioned, local-only contracts and the single preparation boundary for future writers."""

import copy
import json
import math
import re
from datetime import datetime
from functools import lru_cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker, ValidationError, validators

from brain.privacy import scrub_text

CONTRACTS = (
    "get_project_context",
    "search_memory",
    "record_memory",
    "resolve_conflict",
    "get_history",
    "ingest",
)
MAX_BODY_BYTES = 262144
_REGISTRY = json.loads(files(__package__).joinpath("v1.json").read_text(encoding="utf-8"))
_TIME_FIELDS = {
    "timestamp",
    "valid_from",
    "valid_to",
    "valid_at",
    "recorded_at",
    "effective_at",
    "effective_valid_at",
}
_FORMATS = FormatChecker()
_UTC_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
_TEXT_FIELDS = {
    "statement",
    "rationale",
    "anchor",
    "locator",
    "value",
    "technology",
    "version",
    "symptom",
    "cause",
    "fix",
    "verification",
    "goal",
    "query",
    "source_agent",
}


class ContractError(ValueError):
    """Public errors contain fixed messages, never submitted values or validator internals."""


@_FORMATS.checks("date-time")
def _utc_time(value: object) -> bool:
    if not isinstance(value, str):
        return True  # Schema type validation handles non-string values.
    if not _UTC_TIME.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


_STRICT = validators.extend(  # type: ignore[no-untyped-call]  # Upstream factory lacks stubs.
    Draft202012Validator,
    type_checker=(
        Draft202012Validator.TYPE_CHECKER.redefine(
            "integer", lambda checker, value: type(value) is int
        ).redefine(
            "number",
            lambda checker, value: (
                type(value) in (int, float) and (type(value) is int or math.isfinite(value))
            ),
        )
    ),
)


def _reachable_definitions(root: str, definitions: dict[str, Any]) -> dict[str, Any]:
    """Follow the bundled registry's local references, including refs in arrays."""
    reachable: dict[str, Any] = {}
    prefix = "#/$defs/"

    def visit(node: object) -> None:
        if isinstance(node, dict):
            if "$ref" in node:
                reference = node["$ref"]
                if not isinstance(reference, str) or not reference.startswith(prefix):
                    raise ContractError("Expected a local contract definition reference")
                target = reference.removeprefix(prefix)
                if target not in reachable:
                    reachable[target] = definitions[target]
                    visit(reachable[target])
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit({"$ref": root})
    return reachable


def schema_for(name: str, direction: str, *, compact: bool = False) -> dict[str, Any]:
    """Export a self-contained schema, optionally retaining only its referenced definitions."""
    if name not in CONTRACTS or direction not in ("Input", "Output"):
        raise ContractError("Unknown contract")
    root = "#/$defs/" + name + direction
    definitions = _REGISTRY["$defs"]
    if compact:
        definitions = _reachable_definitions(root, definitions)
    return {
        "$schema": _REGISTRY["$schema"],
        "type": "object",
        "$ref": root,
        "$defs": copy.deepcopy(definitions),
    }


@lru_cache(maxsize=12)
def _validator(name: str, direction: str) -> Any:
    return _STRICT(schema_for(name, direction), format_checker=_FORMATS)


def _validate(name: str, direction: str, value: dict[str, Any]) -> None:
    try:
        _validator(name, direction).validate(value)
    except ValidationError:
        raise ContractError("Invalid contract payload") from None


def _json_copy(value: object) -> Any:
    """Bounded plain-JSON tree only, also rejecting floats masquerading as integer fields later."""
    total = 0
    nodes = 0

    def visit(item: object, depth: int) -> Any:
        nonlocal total, nodes
        nodes += 1
        if depth > 32 or nodes > 32768:
            raise ContractError("Payload complexity exceeds limit")
        if type(item) is str:
            if len(item) > MAX_BODY_BYTES:
                raise ContractError("Payload exceeds limit")
            total += len(item.encode("utf-8"))
        elif type(item) is dict:
            if any(type(key) is not str for key in item):
                raise ContractError("Invalid JSON object")
            return {visit(key, depth + 1): visit(val, depth + 1) for key, val in item.items()}
        elif type(item) is list:
            return [visit(val, depth + 1) for val in item]
        elif item is None or type(item) in (bool, int):
            pass
        elif type(item) is float and math.isfinite(item):
            pass
        else:
            raise ContractError("Only finite JSON values are accepted")
        if total > MAX_BODY_BYTES:
            raise ContractError("Payload exceeds limit")
        return item

    try:
        result = visit(value, 0)
        if len(json.dumps(result, allow_nan=False).encode("utf-8")) > MAX_BODY_BYTES:
            raise ContractError("Payload exceeds limit")
    except (ValueError, UnicodeError, RecursionError):
        raise ContractError("Invalid or oversized JSON payload") from None
    return result


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("Duplicate JSON property")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ContractError("Nonfinite JSON value")


def _clean(value: Any, field: str = "", *, normalize_time: bool = True) -> Any:
    if isinstance(value, dict):
        return {key: _clean(val, key, normalize_time=normalize_time) for key, val in value.items()}
    if isinstance(value, list):
        return [_clean(val, field, normalize_time=normalize_time) for val in value]
    if isinstance(value, str):
        cleaned = scrub_text(value)
        if cleaned != value and field not in _TEXT_FIELDS:
            raise ContractError("Sensitive data in an identity or control field")
        if normalize_time and field in _TIME_FIELDS:
            return (
                datetime.fromisoformat(value)
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z")
            )
        return cleaned
    return value


def _interval(value: dict[str, Any]) -> None:
    start, end = value.get("valid_from"), value.get("valid_to")
    if end is not None and (start is None or end <= start):
        raise ContractError("Invalid applicability interval")


def _record(value: dict[str, Any]) -> None:
    _interval(value)
    kind, data = value["payload"]["type"], value["payload"]["data"]
    identity_field = {"TaskState": "task_id", "BugFix": "issue_id"}.get(kind)
    if identity_field and data[identity_field] != value["identity"]["entity"]:
        raise ContractError("Typed identity does not match memory identity")
    if kind == "TaskState" and value["cardinality"] != "single":
        raise ContractError("TaskState must have single cardinality")
    if value["supersedes_revision"] is not None and value["expected_revision"] is None:
        raise ContractError("Supersession requires an expected revision")


def _semantics(name: str, value: dict[str, Any], configured_tokenizer: bool) -> None:
    scope = value.get("scope", value)
    if scope.get("tokenizer") == "configured" and not configured_tokenizer:
        raise ContractError("Configured tokenizer is unavailable")
    if "additional_projects" in scope and scope["project_id"] in scope["additional_projects"]:
        raise ContractError("Primary project must not be repeated")
    if name == "record_memory":
        _record(value)
    elif name == "resolve_conflict":
        if value["action"] == "accept_candidate" and value["effective_at"] is None:
            raise ContractError("Accepting a candidate requires an effective time")
    elif name == "get_history":
        if value["through_seq"] is not None and value["through_seq"] < value["after_seq"]:
            raise ContractError("Invalid history sequence range")
    elif name == "ingest":
        sources: set[tuple[str, str]] = set()
        requests: set[str] = set()
        for entry in value["entries"]:
            identity = (entry["source_id"], entry["source_revision"])
            if identity in sources:
                raise ContractError("Duplicate source manifest")
            sources.add(identity)
            if (entry["extraction_state"] == "structured") != bool(entry["records"]):
                raise ContractError("Extraction state does not match records")
            for record in entry["records"]:
                if (
                    record["project_id"] != value["project_id"]
                    or record["source"] != entry["source"]
                ):
                    raise ContractError("Ingest source or project mismatch")
                if record["idempotency_key"] in requests:
                    raise ContractError("Duplicate record request key")
                requests.add(record["idempotency_key"])
                _record(record)


def prepare_input(
    name: str, payload: object, *, configured_tokenizer: bool = False
) -> dict[str, Any]:
    """Parse → validate → copy/scrub/normalize → revalidate, with no persistence or side effects."""
    try:
        if type(payload) is bytes:
            if len(payload) > MAX_BODY_BYTES:
                raise ContractError("Request body exceeds limit")
            payload = json.loads(
                payload.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant
            )
        value = _json_copy(payload)
    except (ValueError, UnicodeError, RecursionError):
        raise ContractError("Invalid JSON request") from None
    if not isinstance(value, dict):
        raise ContractError("Request must be an object")
    _validate(name, "Input", value)
    # Distinct raw provenance must not become equivalent just because secrets were masked.
    if name == "ingest" and any(
        record["source"] != entry["source"]
        for entry in value["entries"]
        for record in entry["records"]
    ):
        raise ContractError("Ingest source mismatch")
    cleaned: dict[str, Any] = _clean(value)
    _validate(name, "Input", cleaned)
    _semantics(name, cleaned, configured_tokenizer)
    return cleaned


def validate_output(name: str, payload: object) -> None:
    """Reject unsafe outputs, never silently alter their already-counted/token-packed content."""
    value = _json_copy(payload)
    if not isinstance(value, dict):
        raise ContractError("Response must be an object")
    _validate(name, "Output", value)
    if _clean(value, normalize_time=False) != value:
        raise ContractError("Response contains potential secret data")
    if value["ok"] and "budget" in value["data"]:
        budget = value["data"]["budget"]
        if budget["used"] > budget["limit"]:
            raise ContractError("Response exceeds its budget")
